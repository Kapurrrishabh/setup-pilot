from setuppilot.config import load_settings
from setuppilot.db.postgres import PostgresRunStore
from setuppilot.db.runs import InMemoryRunStore
from setuppilot.db.stores import open_stores
from setuppilot.llm.transport import Reply
from tests.conftest import TEST_DB_CONFIG
from tests.test_config import REAL_CONFIG


def settings_with_fast_timeout():
    return load_settings(REAL_CONFIG).model_copy(update={"database": TEST_DB_CONFIG})


def test_missing_db_url_falls_back_to_memory_and_says_so(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    stores = open_stores(settings_with_fast_timeout())
    assert isinstance(stores.runs, InMemoryRunStore)
    assert "SUPABASE_DB_URL is not set" in stores.database_warning


def test_unreachable_db_falls_back_to_memory_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_DB_URL", f"postgresql://postgres@/nothing_test?host={tmp_path}")
    stores = open_stores(settings_with_fast_timeout())
    assert isinstance(stores.runs, InMemoryRunStore)
    assert "Nothing is saved to the database" in stores.database_warning


def test_reachable_db_gives_postgres_stores_and_no_warning(monkeypatch, empty_pg):
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("SUPABASE_DB_URL", TEST_DATABASE_URL)
    stores = open_stores(settings_with_fast_timeout())
    assert isinstance(stores.runs, PostgresRunStore) and stores.database_warning is None


def test_cache_written_through_one_open_is_read_by_the_next(monkeypatch, empty_pg):
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("SUPABASE_DB_URL", TEST_DATABASE_URL)
    open_stores(settings_with_fast_timeout()).cache.put("k", Reply("saved", 1, 1, False))
    assert open_stores(settings_with_fast_timeout()).cache.get("k").text == "saved"
