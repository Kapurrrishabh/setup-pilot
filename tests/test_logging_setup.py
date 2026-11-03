import json
import logging

import pytest

from setuppilot.config import LoggingConfig
from setuppilot.logging_setup import run_id_var, setup_logging


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "logs" / "app.jsonl"
    setup_logging(LoggingConfig(level="INFO", file=path))
    return path


def last_entry(path):
    return json.loads(path.read_text().splitlines()[-1])


def test_log_line_has_message_run_id_and_extras(log_file):
    token = run_id_var.set("run-42")
    try:
        logging.getLogger("setuppilot.test").info("calling groq", extra={"tool": "llm"})
    finally:
        run_id_var.reset(token)
    entry = last_entry(log_file)
    assert (entry["msg"], entry["run_id"], entry["tool"]) == ("calling groq", "run-42", "llm")


def test_secret_in_log_line_is_hidden(log_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-secret-value")
    logging.getLogger("setuppilot.test").info("key is gsk-test-secret-value")
    assert "gsk-test-secret-value" not in log_file.read_text()


def test_extras_cannot_overwrite_core_fields(log_file):
    logging.getLogger("setuppilot.test").error("failed", extra={"level": "INFO", "logger": "x"})
    entry = last_entry(log_file)
    assert (entry["level"], entry["logger"]) == ("ERROR", "setuppilot.test")


def test_library_warning_reaches_the_log_file(log_file):
    logging.getLogger("psycopg.pool").warning("database unreachable")
    assert last_entry(log_file)["logger"] == "psycopg.pool"
