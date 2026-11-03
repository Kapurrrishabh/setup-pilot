import json
import time

from setuppilot.redact import MASK, redact


def test_env_secret_is_hidden(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "my-real-groq-key-123")
    assert redact("key=my-real-groq-key-123 end") == f"key={MASK} end"


def test_json_escaped_secret_is_hidden_and_line_stays_valid_json(monkeypatch):
    fake_key = 'abcd"efgh\\'
    monkeypatch.setenv("GROQ_API_KEY", fake_key)
    assert json.loads(redact(json.dumps({"k": fake_key}))) == {"k": MASK}


def test_groq_key_pattern_is_hidden_without_env():
    assert "gsk_" not in redact("leaked gsk_abcdefghijklmnopqrstuvwxyz0123")


def test_langsmith_key_pattern_is_hidden_without_env():
    assert "lsv2_" not in redact("leaked lsv2_pt_0123456789abcdef0123456789abcdef_01")


def test_password_in_url_is_hidden():
    out = redact("postgresql://postgres:hunter2secret@db.abc.supabase.co:5432/postgres")
    assert "hunter2secret" not in out
    assert "db.abc.supabase.co" in out


def test_password_with_at_sign_in_url_is_hidden():
    out = redact("https://user:p@ssw0rdTail99@git.example.com/x.git")
    assert "ssw0rdTail99" not in out
    assert "git.example.com" in out


def test_long_line_without_spaces_is_fast():
    # The old pattern took over 5 seconds on this input.
    start = time.perf_counter()
    redact("http://h:1," * 20_000)
    assert time.perf_counter() - start < 1


def test_short_placeholder_value_is_left_alone(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "x")
    assert redact("x marks the spot") == "x marks the spot"
