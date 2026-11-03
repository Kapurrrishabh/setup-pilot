from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import PoolTimeout

from setuppilot.config import DatabaseConfig
from setuppilot.db.postgres import PostgresRunStore, migrate, open_checkpointer, open_pool
from setuppilot.errors import DatabaseUnavailable
from tests.conftest import DATA_TABLES, TEST_DATABASE_URL, TEST_DB_CONFIG

FAST = DatabaseConfig(pool_max_size=1, connect_timeout_seconds=1)
SOME_RUN = "00000000-0000-0000-0000-000000000000"


def migrate_from_scratch(pool):
    with pool.connection() as conn:
        conn.execute(f"drop table {', '.join(DATA_TABLES)}, schema_migrations cascade")
    return migrate(pool)


def test_migrate_twice_runs_nothing_the_second_time(pg_pool):
    assert migrate(pg_pool) == []


def test_migrate_on_an_empty_database_creates_every_table(pg_pool):
    assert migrate_from_scratch(pg_pool) == ["001_init.sql", "002_chat.sql"]


def test_row_level_security_is_on_for_every_table(pg_pool):
    migrate_from_scratch(pg_pool)
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "select tablename, rowsecurity from pg_tables where schemaname = 'public'"
        ).fetchall()
    assert rows and all(r["rowsecurity"] for r in rows), rows


def test_supabase_public_roles_get_no_access_to_any_table(pg_pool):
    with pg_pool.connection() as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role} nologin")
        # Supabase gives these roles every new table by default; our migration must undo it.
        conn.execute("alter default privileges in schema public grant all on tables to anon")
    migrate_from_scratch(pg_pool)
    with pg_pool.connection() as conn:
        open_tables = conn.execute(
            "select tablename from pg_tables where schemaname = 'public' and "
            "has_table_privilege('anon', schemaname || '.' || tablename, 'select,insert,delete')"
        ).fetchall()
    assert open_tables == []


def test_unreachable_database_is_reported(tmp_path):
    url = f"postgresql://postgres@/nothing_test?host={tmp_path}"
    with pytest.raises(DatabaseUnavailable, match="Could not connect") as error:
        open_pool(url, FAST)
    assert "SUPABASE_DB_URL" in error.value.details


def test_missing_database_names_the_real_cause(pg_pool):
    url = make_conninfo(TEST_DATABASE_URL, dbname="no_such_db_test")
    with pytest.raises(DatabaseUnavailable) as error:
        open_pool(url, FAST)
    assert "does not exist" in error.value.details


@pytest.mark.parametrize(
    "url", ["postgresql://postgres:pa%ss@db.example/postgres", "postgresql://u:p@ss@db.example/x"]
)
def test_url_with_unescaped_password_characters_is_refused_without_showing_it(url):
    with pytest.raises(DatabaseUnavailable, match="not a valid connection string") as error:
        open_pool(url, FAST)
    assert "pa%ss" not in error.value.details and "ss@db" not in error.value.details


class BrokenPool:
    def __init__(self, error):
        self._error = error

    def connection(self):
        raise self._error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (psycopg.OperationalError("server closed the connection"), "Lost the connection"),
        (PoolTimeout("no connection in 10s"), "busy"),
        (psycopg.errors.ReadOnlySqlTransaction("read-only"), "read-only"),
    ],
)
def test_database_errors_become_database_unavailable(error, message):
    with pytest.raises(DatabaseUnavailable, match=message):
        PostgresRunStore(BrokenPool(error)).get_run(SOME_RUN)


def test_checkpointer_opens_with_a_pool_of_one_connection(pg_pool):
    open_checkpointer(TEST_DATABASE_URL, FAST).conn.close()


def test_two_first_starts_at_once_both_set_up_saved_run_state(pg_pool):
    with pg_pool.connection() as conn:
        conn.execute(
            "drop table if exists checkpoint_migrations, checkpoints, checkpoint_blobs, "
            "checkpoint_writes"
        )
    # A lock wait that never ends then fails the test instead of hanging it.
    url = make_conninfo(TEST_DATABASE_URL, options="-c lock_timeout=20s")
    with ThreadPoolExecutor(4) as starts:
        savers = list(starts.map(lambda _: open_checkpointer(url, TEST_DB_CONFIG), range(4)))
    for saver in savers:
        saver.conn.close()


def test_checkpoint_lock_held_too_long_is_reported_instead_of_waited_on(pg_pool):
    from setuppilot.db.postgres import _CHECKPOINT_LOCK_ID

    url = make_conninfo(TEST_DATABASE_URL, options="-c lock_timeout=5s")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as other_start:
        other_start.execute("select pg_advisory_lock(%s)", (_CHECKPOINT_LOCK_ID,))
        with pytest.raises(DatabaseUnavailable) as error:
            open_checkpointer(url, FAST)
    assert f"lock {_CHECKPOINT_LOCK_ID}" in error.value.details


def test_migrate_does_not_wait_while_the_saved_run_state_is_being_set_up(pg_pool):
    from setuppilot.db.postgres import _CHECKPOINT_LOCK_ID

    # Sharing one lock lets migrate block LangGraph's concurrent index build and leave it invalid.
    url = make_conninfo(TEST_DATABASE_URL, options="-c lock_timeout=2s")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as other_start:
        other_start.execute("select pg_advisory_lock(%s)", (_CHECKPOINT_LOCK_ID,))
        pool = open_pool(url, FAST)
        try:
            assert migrate(pool) == []
        finally:
            pool.close()
