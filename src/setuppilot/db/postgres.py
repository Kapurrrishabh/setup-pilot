"""Supabase (Postgres) versions of the stores, and the schema migrations."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from time import monotonic, sleep
from urllib.parse import urlsplit

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from setuppilot.chat.chunks import Chunk
from setuppilot.chat.index import ChatRepo, Hit, IndexedRepo, SearchResults, unknown_repo
from setuppilot.chat.search import tokens
from setuppilot.config import DatabaseConfig
from setuppilot.db.metrics import MetricName
from setuppilot.db.runs import FINISHED_STATUSES, Run, RunStatus, Step, no_such_run
from setuppilot.errors import DatabaseUnavailable
from setuppilot.llm.transport import Reply
from setuppilot.llm.usage import Usage
from setuppilot.redact import redact

# Any fixed numbers work; each only has to be the same in every process that migrates.
_MIGRATION_LOCK_ID = 20261101
# Its own lock: a migration waiting on it would block LangGraph's concurrent index build.
_CHECKPOINT_LOCK_ID = 20261102
# Every pool here sets row_factory=dict_row, so its rows are dicts, not tuples.
Pool = ConnectionPool[psycopg.Connection[DictRow]]


def open_pool(url: str, config: DatabaseConfig) -> Pool:
    timeout = int(config.connect_timeout_seconds)
    try:
        conninfo_to_dict(url)
        # A raw @ in the password moves part of it into the host name, and errors print the host.
        is_valid = urlsplit(url).netloc.count("@") <= 1
    except psycopg.ProgrammingError:
        is_valid = False
    if not is_valid:
        # psycopg's message repeats the URL, password included, so it is not passed on.
        raise DatabaseUnavailable(
            "SUPABASE_DB_URL is not a valid connection string.",
            details="Copy it again from Supabase. If your password has % or @ in it, "
            "write them as %25 and %40.",
        )
    try:
        psycopg.connect(url, connect_timeout=timeout).close()
    except psycopg.OperationalError as error:
        raise DatabaseUnavailable(
            "Could not connect to the database.",
            details=f"{redact(str(error)).strip()} Check SUPABASE_DB_URL in .env: the password, "
            "and on an IPv4 network the Session pooler string.",
        ) from error
    pool: Pool = ConnectionPool(
        url,
        min_size=1,
        max_size=config.pool_max_size,
        # Supabase's transaction pooler cannot use prepared statements.
        kwargs={"prepare_threshold": None, "row_factory": dict_row, "connect_timeout": timeout},
        check=ConnectionPool.check_connection,
        timeout=config.connect_timeout_seconds,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=config.connect_timeout_seconds)
    except PoolTimeout as error:
        raise DatabaseUnavailable(
            "Could not reach the database.",
            details=f"No connection after {timeout} seconds. Check SUPABASE_DB_URL in .env.",
        ) from error
    return pool


def open_checkpointer(url: str, config: DatabaseConfig) -> PostgresSaver:
    """LangGraph's saved run state, so a waiting approval survives an app restart."""
    timeout = int(config.connect_timeout_seconds)
    # LangGraph's own setup needs autocommit (it creates indexes concurrently).
    pool: Pool = ConnectionPool(
        url,
        min_size=1,
        max_size=config.pool_max_size,
        kwargs={
            "autocommit": True,
            "prepare_threshold": None,
            "row_factory": dict_row,
            "connect_timeout": timeout,
        },
        check=ConnectionPool.check_connection,
        timeout=config.connect_timeout_seconds,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=config.connect_timeout_seconds)
        saver = PostgresSaver(pool)
        with pool.connection() as conn:
            # Two app starts at once would both create LangGraph's tables; let one go first.
            # Poll, not wait in one query: a waiting query stalls the other's CREATE INDEX
            # CONCURRENTLY forever. Setup runs on this connection, so a pool of one is enough.
            deadline = monotonic() + config.connect_timeout_seconds
            try_lock = "select pg_try_advisory_lock(%s) as is_locked"
            while True:
                row = conn.execute(try_lock, (_CHECKPOINT_LOCK_ID,)).fetchone()
                if row is not None and row["is_locked"]:
                    break
                if monotonic() > deadline:
                    raise TimeoutError(
                        f"Another SetupPilot start held database lock {_CHECKPOINT_LOCK_ID} "
                        f"for over {timeout} seconds. Wait for it to finish, then try again."
                    )
                sleep(0.1)
            try:
                PostgresSaver(conn).setup()
            finally:
                conn.execute("select pg_advisory_unlock(%s)", (_CHECKPOINT_LOCK_ID,))
            tables = conn.execute(
                "select tablename from pg_tables "
                "where schemaname = 'public' and tablename like 'checkpoint%'"
            ).fetchall()
            for row in tables:
                name = sql.Identifier(row["tablename"])
                conn.execute(sql.SQL("alter table {} enable row level security").format(name))
    except (PoolTimeout, psycopg.Error, TimeoutError) as error:
        pool.close()
        raise DatabaseUnavailable(
            "Could not set up saved run state in the database.", details=str(error)
        ) from error
    return saver


@contextmanager
def _connection(pool: Pool) -> Iterator[psycopg.Connection[DictRow]]:
    try:
        with pool.connection() as conn:
            yield conn
    except PoolTimeout as error:
        raise DatabaseUnavailable(
            "The database is busy: every connection is in use.", details=str(error)
        ) from error
    except psycopg.errors.ReadOnlySqlTransaction as error:
        raise DatabaseUnavailable(
            "The database is read-only, often because the free plan's disk is full.",
            details=f"Delete old runs or cache rows in Supabase. {error}",
        ) from error
    except psycopg.OperationalError as error:
        raise DatabaseUnavailable(
            "Lost the connection to the database.", details=str(error)
        ) from error


def _run_uuid(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id)
    except ValueError:
        raise no_such_run(run_id) from None


@contextmanager
def _unknown_run_is_lookup_error(run_id: str | None) -> Iterator[None]:
    try:
        yield
    except psycopg.errors.ForeignKeyViolation:
        raise no_such_run(str(run_id)) from None


def migrate(pool: Pool) -> list[str]:
    """Run each migrations/*.sql file once, in name order. Returns the files it ran."""
    files = sorted(
        resources.files("setuppilot.db").joinpath("migrations").iterdir(), key=lambda f: f.name
    )
    ran = []
    with _connection(pool) as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        conn.execute(
            "create table if not exists schema_migrations "
            "(version text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("alter table schema_migrations enable row level security")
        done = {row["version"] for row in conn.execute("select version from schema_migrations")}
        for file in files:
            if file.name.endswith(".sql") and file.name not in done:
                conn.execute(file.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (version) values (%s)", (file.name,))
                ran.append(file.name)
    return ran


def _run_from_row(row: dict) -> Run:
    # psycopg gives times in the session's time zone; the app shows them in UTC.
    finished = row["finished_at"].astimezone(UTC) if row["finished_at"] else None
    return Run(
        str(row["id"]),
        row["repo_url"],
        row["dest_dir"],
        row["status"],
        row["error_type"],
        row["message"],
        row["created_at"].astimezone(UTC),
        finished,
    )


class PostgresRunStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def create_run(self, repo_url: str, dest_dir: str) -> Run:
        with _connection(self._pool) as conn:
            (row,) = conn.execute(
                "insert into runs (repo_url, dest_dir, status) values (%s, %s, 'running') "
                "returning *",
                (repo_url, dest_dir),
            ).fetchall()
        return _run_from_row(row)

    def get_run(self, run_id: str) -> Run:
        with _connection(self._pool) as conn:
            row = conn.execute("select * from runs where id = %s", (_run_uuid(run_id),)).fetchone()
        if row is None:
            raise no_such_run(run_id)
        return _run_from_row(row)

    def list_runs(self, limit: int) -> list[Run]:
        with _connection(self._pool) as conn:
            rows = conn.execute(
                "select * from runs order by created_at desc limit %s", (limit,)
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def set_status(
        self, run_id: str, status: RunStatus, error_type: str | None, message: str | None
    ) -> None:
        # One statement, so a late worker update cannot undo a Cancel that lands at the same time.
        with _connection(self._pool) as conn:
            updated = conn.execute(
                "update runs set status = %s, error_type = %s, message = %s, "
                "finished_at = case when %s then now() end "
                "where id = %s and status <> all(%s)",
                (
                    status,
                    error_type,
                    message,
                    status in FINISHED_STATUSES,
                    _run_uuid(run_id),
                    list(FINISHED_STATUSES),
                ),
            ).rowcount
        if updated == 0:
            self.get_run(run_id)  # raises when there is no such run; otherwise it was finished

    def delete_run(self, run_id: str) -> None:
        with _connection(self._pool) as conn:
            conn.execute("delete from runs where id = %s", (_run_uuid(run_id),))

    def add_step(self, step: Step) -> None:
        with _connection(self._pool) as conn, _unknown_run_is_lookup_error(step.run_id):
            conn.execute(
                "insert into steps (run_id, name, tool, arguments, result_summary, is_ok, "
                "error_type, exit_code, duration_ms, tokens, model) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _run_uuid(step.run_id),
                    step.name,
                    step.tool,
                    Jsonb(step.arguments),
                    step.result_summary,
                    step.is_ok,
                    step.error_type,
                    step.exit_code,
                    step.duration_ms,
                    step.tokens,
                    step.model,
                ),
            )

    def list_steps(self, run_id: str) -> list[Step]:
        with _connection(self._pool) as conn:
            rows = conn.execute(
                "select * from steps where run_id = %s order by id", (_run_uuid(run_id),)
            ).fetchall()
        return [
            Step(
                str(r["run_id"]),
                r["name"],
                r["tool"],
                r["arguments"],
                r["result_summary"],
                r["is_ok"],
                r["error_type"],
                r["exit_code"],
                r["duration_ms"],
                r["tokens"],
                r["model"],
            )
            for r in rows
        ]


class PostgresUsageStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def add(self, usage: Usage) -> None:
        run_id = _run_uuid(usage.run_id) if usage.run_id else None
        with _connection(self._pool) as conn, _unknown_run_is_lookup_error(usage.run_id):
            conn.execute(
                "insert into token_usage "
                "(run_id, model, input_tokens, output_tokens, is_cached, created_at) "
                "values (%s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.is_cached,
                    usage.at,
                ),
            )

    def tokens_for_run(self, run_id: str) -> int:
        with _connection(self._pool) as conn:
            (row,) = conn.execute(
                "select coalesce(sum(input_tokens + output_tokens), 0) as total "
                "from token_usage where run_id = %s",
                (_run_uuid(run_id),),
            ).fetchall()
        return row["total"]

    def tokens_for_runs(self, run_ids: list[str]) -> dict[str, int]:
        """All runs in one query, so a page of 200 runs is not 200 round trips."""
        ids = [_run_uuid(run_id) for run_id in run_ids]
        with _connection(self._pool) as conn:
            rows = conn.execute(
                "select run_id, sum(input_tokens + output_tokens) as total from token_usage "
                "where run_id = any(%s) group by run_id",
                (ids,),
            ).fetchall()
        totals = {str(r["run_id"]): r["total"] for r in rows}
        return {run_id: totals.get(run_id, 0) for run_id in run_ids}

    def tokens_on_day(self, day: date) -> int:
        with _connection(self._pool) as conn:
            (row,) = conn.execute(
                "select coalesce(sum(input_tokens + output_tokens), 0) as total "
                "from token_usage where created_at >= %s and created_at < %s",
                _day_range(day),
            ).fetchall()
        return row["total"]

    def calls_on_day_by_model(self, day: date) -> dict[str, int]:
        with _connection(self._pool) as conn:
            rows = conn.execute(
                "select model, count(*) as calls from token_usage "
                "where created_at >= %s and created_at < %s and not is_cached group by model",
                _day_range(day),
            ).fetchall()
        return {r["model"]: r["calls"] for r in rows}


def _day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), UTC)
    return start, start + timedelta(days=1)


class PostgresCache:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def get(self, key: str) -> Reply | None:
        with _connection(self._pool) as conn:
            row = conn.execute(
                "select text, input_tokens, output_tokens from llm_cache where key = %s", (key,)
            ).fetchone()
        if row is None:
            return None
        return Reply(row["text"], row["input_tokens"], row["output_tokens"], is_truncated=False)

    def put(self, key: str, reply: Reply) -> None:
        with _connection(self._pool) as conn:
            conn.execute(
                "insert into llm_cache (key, text, input_tokens, output_tokens) "
                "values (%s, %s, %s, %s) on conflict (key) do nothing",
                (key, reply.text, reply.input_tokens, reply.output_tokens),
            )


class PostgresMetricsStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def add(self, name: MetricName, run_id: str | None) -> None:
        run_uuid = _run_uuid(run_id) if run_id else None
        with _connection(self._pool) as conn, _unknown_run_is_lookup_error(run_id):
            conn.execute(
                "insert into metrics (run_id, name, value) values (%s, %s, 1)", (run_uuid, name)
            )

    def count(self, name: MetricName) -> int:
        with _connection(self._pool) as conn:
            (row,) = conn.execute(
                "select count(*) as n from metrics where name = %s", (name,)
            ).fetchall()
        return row["n"]


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


class PostgresChunkIndex:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def replace_repo(
        self, indexed: IndexedRepo, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        repo = indexed.repo
        rows = [
            (repo.key, c.path, c.section, c.text.replace("\x00", ""), c.start_line, c.end_line,
             c.kind, " ".join(tokens(c.search_text)), _vector_text(v))
            for c, v in zip(chunks, vectors, strict=True)
        ]  # fmt: skip
        card = Jsonb([asdict(c) for c in indexed.card])
        # One transaction: a reader sees the old index or the new one, never half of each.
        with _connection(self._pool) as conn:
            # Two indexing jobs for the same repo take turns instead of clashing.
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (repo.key,))
            conn.execute("delete from chat_repos where key = %s", (repo.key,))
            conn.execute(
                "insert into chat_repos (key, url, chunk_count, commit_sha, embedding_model, card, "
                "owner_github_id) values (%s, %s, %s, %s, %s, %s, %s)",
                (repo.key, repo.url, len(rows), repo.commit_sha, indexed.embedding_model, card,
                 repo.owner_github_id),
            )  # fmt: skip
            with conn.cursor() as cursor:
                cursor.executemany(
                    "insert into chat_chunks (repo_key, path, section, text, start_line, end_line, "
                    "kind, words, embedding) values (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    rows,
                )

    def repo(self, repo_key: str, viewer_github_id: int | None) -> IndexedRepo:
        with _connection(self._pool) as conn:
            row = conn.execute(
                "select * from chat_repos where key = %s "
                "and (owner_github_id is null or owner_github_id = %s)",
                (repo_key, viewer_github_id),
            ).fetchone()
        if row is None:
            raise unknown_repo(repo_key)
        repo = ChatRepo(row["key"], row["url"], row["commit_sha"], row["owner_github_id"])
        card = tuple(Chunk(**c) for c in row["card"])
        return IndexedRepo(repo, row["embedding_model"], card)

    def repos(self, viewer_github_id: int | None) -> list[str]:
        with _connection(self._pool) as conn:
            rows = conn.execute(
                "select key from chat_repos "
                "where owner_github_id is null or owner_github_id = %s order by key",
                (viewer_github_id,),
            ).fetchall()
        return [r["key"] for r in rows]

    def search(
        self,
        repo_key: str,
        viewer_github_id: int | None,
        vector: list[float],
        terms: list[str],
        limit: int,
    ) -> SearchResults:
        # Terms hold only letters, digits and dots (see tokens), so quoting each one is safe.
        query = " | ".join(f"'{term}'" for term in terms)
        with _connection(self._pool) as conn:
            is_visible = conn.execute(
                "select 1 from chat_repos where key = %s "
                "and (owner_github_id is null or owner_github_id = %s)",
                (repo_key, viewer_github_id),
            ).fetchone()
            if not is_visible:
                raise unknown_repo(repo_key)
            by_vector = conn.execute(
                "select path, section, text, start_line, end_line, kind, "
                "1 - (embedding <=> %s::vector) as score from chat_chunks "
                "where repo_key = %s order by embedding <=> %s::vector limit %s",
                (_vector_text(vector), repo_key, _vector_text(vector), limit),
            ).fetchall()
            by_words = []
            if terms:
                by_words = conn.execute(
                    "select path, section, text, start_line, end_line, kind, "
                    "ts_rank_cd(fts, query) as score from chat_chunks, "
                    "to_tsquery('simple', %s) as query "
                    "where repo_key = %s and fts @@ query order by score desc limit %s",
                    (query, repo_key, limit),
                ).fetchall()
        return SearchResults(_hits(by_vector), _hits(by_words))


def _hits(rows: list[DictRow]) -> list[Hit]:
    return [
        Hit(Chunk(r["path"], r["section"], r["text"], r["start_line"], r["end_line"], r["kind"]),
            r["score"])
        for r in rows
    ]  # fmt: skip
