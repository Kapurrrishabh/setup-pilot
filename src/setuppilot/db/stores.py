"""Open the stores: Supabase when it is reachable, otherwise memory plus a clear warning."""

import atexit
import logging
import os
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from setuppilot.chat.index import ChunkIndex, InMemoryChunkIndex
from setuppilot.config import Settings
from setuppilot.db.metrics import InMemoryMetricsStore, MetricsStore
from setuppilot.db.postgres import (
    PostgresCache,
    PostgresChunkIndex,
    PostgresMetricsStore,
    PostgresRunStore,
    PostgresUsageStore,
    migrate,
    open_checkpointer,
    open_pool,
)
from setuppilot.db.runs import InMemoryRunStore, RunStore
from setuppilot.errors import DatabaseUnavailable
from setuppilot.llm.cache import InMemoryCache, LLMCache
from setuppilot.llm.usage import InMemoryUsageStore, UsageStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stores:
    runs: RunStore
    usage: UsageStore
    cache: LLMCache
    metrics: MetricsStore
    chat_index: ChunkIndex
    checkpointer: BaseCheckpointSaver
    # None when everything is saved to the database; otherwise the text to show the user.
    database_warning: str | None


def open_stores(settings: Settings) -> Stores:
    url = os.environ.get("SUPABASE_DB_URL")
    try:
        if not url:
            raise DatabaseUnavailable(
                "No database is set up.", details="SUPABASE_DB_URL is not set in .env."
            )
        pool = open_pool(url, settings.database)
        try:
            migrate(pool)
            checkpointer = open_checkpointer(url, settings.database)
        except DatabaseUnavailable:
            pool.close()
            raise
    except DatabaseUnavailable as error:
        warning = (
            f"{error.message} Nothing is saved to the database this time, and a paused run "
            f"cannot be resumed after a restart. The app's logs are in {settings.logging.file}. "
            f"Details: {error.details}"
        )
        log.warning("database unavailable", extra={"warning": warning})
        memory = (
            InMemoryRunStore(),
            InMemoryUsageStore(),
            InMemoryCache(),
            InMemoryMetricsStore(),
            InMemoryChunkIndex(),
            InMemorySaver(),
        )
        return Stores(*memory, warning)
    atexit.register(pool.close)
    atexit.register(checkpointer.conn.close)
    postgres = (
        PostgresRunStore(pool),
        PostgresUsageStore(pool),
        PostgresCache(pool),
        PostgresMetricsStore(pool),
        PostgresChunkIndex(pool),
    )
    return Stores(*postgres, checkpointer, None)
