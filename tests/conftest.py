import logging
import os

import pytest

from setuppilot.config import DatabaseConfig

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_DB_CONFIG = DatabaseConfig(pool_max_size=2, connect_timeout_seconds=5)
DATA_TABLES = ("runs", "steps", "token_usage", "llm_cache", "metrics", "chat_repos", "chat_chunks")


@pytest.fixture(scope="session")
def pg_pool():
    """A pool on the test database, or skip when there is none (but never skip in CI)."""
    if not TEST_DATABASE_URL:
        if os.environ.get("SETUPPILOT_SKIP_DB_TESTS") == "1":
            pytest.skip("SETUPPILOT_SKIP_DB_TESTS=1: the Postgres tests run in the Linux CI job.")
        if os.environ.get("CI"):
            pytest.fail("TEST_DATABASE_URL must be set in CI, or the Postgres tests never run.")
        pytest.skip("TEST_DATABASE_URL is not set, so Postgres tests are skipped.")
    from setuppilot.db.postgres import migrate, open_pool

    pool = open_pool(TEST_DATABASE_URL, TEST_DB_CONFIG)
    with pool.connection() as conn:
        name = conn.execute("select current_database() as name").fetchone()["name"]
    # The tests empty every table, so never let them touch a real database.
    if not name.endswith("_test"):
        pool.close()
        pytest.fail(f"TEST_DATABASE_URL must point at a database ending in _test, not {name!r}.")
    migrate(pool)
    yield pool
    pool.close()


@pytest.fixture
def empty_pg(pg_pool):
    """The test database with every table emptied."""
    with pg_pool.connection() as conn:
        conn.execute(f"truncate {', '.join(DATA_TABLES)} restart identity cascade")
    return pg_pool


@pytest.fixture
def can_symlink(tmp_path):
    """Skips the test where this account cannot make symlinks."""
    try:
        (tmp_path / ".symlink-check").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Making symlinks needs admin rights or Developer Mode on Windows.")


@pytest.fixture(autouse=True)
def keep_path(monkeypatch):
    """On Windows the agent adds saved registry PATH entries; tests must not leak them."""
    monkeypatch.setenv("PATH", os.environ["PATH"])


@pytest.fixture(autouse=True)
def restore_logging():
    """Tests that call setup_logging (directly or through the CLI) must not leak handlers."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)
