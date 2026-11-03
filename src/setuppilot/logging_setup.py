"""Structured JSON logs to stderr and a local file, with secrets hidden."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

from setuppilot.config import LoggingConfig
from setuppilot.redact import redact

APP_LOGGER = "setuppilot"
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_RECORD_FIELDS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in vars(record).items() if k not in _RECORD_FIELDS}
        entry = extras | {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": run_id_var.get(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return redact(json.dumps(entry, default=str))


def setup_logging(config: LoggingConfig) -> None:
    config.file.parent.mkdir(parents=True, exist_ok=True)
    # Handlers sit on the root logger so library logs (httpx, psycopg) are hidden and saved too.
    root = logging.getLogger()
    for old in [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]:
        root.removeHandler(old)
        old.close()
    formatter = JsonFormatter()
    for handler in (logging.StreamHandler(), logging.FileHandler(config.file, encoding="utf-8")):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger(APP_LOGGER).setLevel(config.level)
