import shutil

import pytest

from setuppilot import __main__ as cli
from setuppilot.__main__ import main
from tests.fakes import FakeTransport, make_client, make_repo, reply
from tests.test_config import REAL_CONFIG


@pytest.fixture
def in_empty_project(tmp_path, monkeypatch):
    """Run the CLI in a folder with only config.yaml: no .env, no database."""
    shutil.copy(REAL_CONFIG, tmp_path / "config.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    return tmp_path


def test_ask_without_api_key_exits_with_a_friendly_message(in_empty_project, monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        main(["ask", "hello"])
    assert exit_info.value.code == 1
    assert "GROQ_API_KEY is not set" in capsys.readouterr().err


def test_ask_prints_the_answer_and_tokens_used(in_empty_project, monkeypatch, capsys):
    fake = make_client(FakeTransport(reply("It sets up repos.", 12, 3)))
    monkeypatch.setattr(cli, "build_llm_client", lambda settings, usage, cache, metrics: fake)
    main(["ask", "What is this?"])
    out = capsys.readouterr().out
    assert "It sets up repos." in out and "tokens in: 12" in out and "tokens out: 3" in out


def test_ask_without_database_warns_that_nothing_is_saved(in_empty_project, monkeypatch, capsys):
    fake = make_client(FakeTransport(reply("hi")))
    monkeypatch.setattr(cli, "build_llm_client", lambda settings, usage, cache, metrics: fake)
    main(["ask", "q"])
    err_lines = capsys.readouterr().err.splitlines()
    assert any(line.startswith("No database is set up.") for line in err_lines)


def test_database_warning_is_printed_once_not_also_as_a_json_log_line(in_empty_project, capsys):
    main(["runs"])
    assert capsys.readouterr().err.count("No database is set up.") == 1


def test_db_check_without_database_prints_the_plain_warning(in_empty_project, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["db-check"])
    err_lines = capsys.readouterr().err.splitlines()
    assert exit_info.value.code == 1
    assert any(line.startswith("No database is set up.") for line in err_lines)


def test_db_check_writes_and_reads_a_run(in_empty_project, monkeypatch, capsys, empty_pg):
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("SUPABASE_DB_URL", TEST_DATABASE_URL)
    main(["db-check"])
    assert "Database OK" in capsys.readouterr().out
    with empty_pg.connection() as conn:
        assert conn.execute("select count(*) as n from runs").fetchone()["n"] == 0


def use_fake_agent(monkeypatch, source):
    """The real CLI, with a fake clone, fake tools and a fake runner behind it."""
    from setuppilot.agent.driver import SetupAgent
    from setuppilot.agent.graph import AgentDeps
    from tests.test_agent import AllInstalled, FakeRunner, fake_clone

    def make_fake_agent(settings, stores):
        deps = AgentDeps(
            settings, stores.runs, None, fake_clone(source), AllInstalled(), FakeRunner()
        )
        return SetupAgent(deps, stores.checkpointer)

    monkeypatch.setattr(cli, "make_agent", make_fake_agent)


def run_cli_with_answers(monkeypatch, answers, argv):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    return exit_info.value.code


def test_run_asks_before_each_command_and_reports(in_empty_project, monkeypatch, capsys):
    use_fake_agent(monkeypatch, make_repo(in_empty_project / "source", {"requirements.txt": ""}))
    (in_empty_project / "dest").mkdir()
    code = run_cli_with_answers(
        monkeypatch, ["a", "a"], ["run", "https://github.com/someone/demo", "dest"]
    )
    out = capsys.readouterr().out
    assert code == 0 and "Command: uv venv .venv" in out and "Setup verified" in out


def test_quitting_at_a_question_leaves_the_run_paused(in_empty_project, monkeypatch, capsys):
    use_fake_agent(monkeypatch, make_repo(in_empty_project / "source", {"requirements.txt": ""}))
    (in_empty_project / "dest").mkdir()
    code = run_cli_with_answers(
        monkeypatch, ["q"], ["run", "https://github.com/someone/demo", "dest"]
    )
    assert code == 1 and "Continue later with: setuppilot resume" in capsys.readouterr().out


def test_cli_asks_which_stack_and_skips_bad_answers(in_empty_project, monkeypatch, capsys):
    files = {"requirements.txt": "", "package.json": "{}"}
    use_fake_agent(monkeypatch, make_repo(in_empty_project / "source", files))
    (in_empty_project / "dest").mkdir()
    answers = ["9", "two", "2", "a"]
    code = run_cli_with_answers(
        monkeypatch, answers, ["run", "https://github.com/someone/demo", "dest"]
    )
    out = capsys.readouterr().out
    assert code == 0 and "1. python/pip" in out and "Command: npm install" in out


def test_resume_with_an_unknown_run_id_says_so(in_empty_project, monkeypatch, capsys):
    use_fake_agent(monkeypatch, make_repo(in_empty_project / "source", {"requirements.txt": ""}))
    code = run_cli_with_answers(monkeypatch, [], ["resume", "00000000-0000-0000-0000-000000000000"])
    assert code == 1 and "No run with id" in capsys.readouterr().err


def test_show_prints_a_runs_timeline_tokens_and_error(
    in_empty_project, monkeypatch, capsys, empty_pg
):
    from datetime import UTC, datetime

    from setuppilot.db.postgres import PostgresRunStore, PostgresUsageStore
    from setuppilot.db.runs import Step
    from setuppilot.llm.usage import Usage
    from tests.conftest import TEST_DATABASE_URL

    runs = PostgresRunStore(empty_pg)
    run = runs.create_run("https://github.com/psf/requests", "/home/dev/work")
    runs.add_step(Step(run.id, "clone", "git", {}, "Cloned.", True, None, 0, 900, 0, None))
    runs.add_step(
        Step(run.id, "execute", "uv", {}, "exit 1", False, "InstallFailed", 1, 50, 0, None)
    )
    runs.set_status(run.id, "failed", "InstallFailed", "uv pip install failed")
    PostgresUsageStore(empty_pg).add(Usage(run.id, "big", 120, 30, False, datetime.now(UTC)))
    monkeypatch.setenv("SUPABASE_DB_URL", TEST_DATABASE_URL)
    main(["show", run.id])
    out = capsys.readouterr().out
    assert "tokens: 150" in out and "FAILED (InstallFailed)" in out and "clone" in out
    main(["metrics"])
    assert "success rate: 0%" in capsys.readouterr().out


def test_index_then_chat_answers_with_sources(in_empty_project, monkeypatch, capsys):
    import json

    from setuppilot.chat import cli as chat_cli
    from tests.fakes import FakeEmbedder

    folder = make_repo(
        in_empty_project / "workspace/chat-repos/someone/demo",
        {"LICENSE": "MIT License", ".git/setuppilot-clone-done": ""},
    )
    assert folder.is_dir()
    monkeypatch.setattr(chat_cli, "LocalEmbedder", lambda name, size: FakeEmbedder())
    shared = {}

    def keep_stores(settings):
        if "stores" not in shared:
            shared["stores"] = real_open_stores(settings)
        return shared["stores"]

    real_open_stores = cli.open_stores
    monkeypatch.setattr(cli, "open_stores", keep_stores)
    main(["index", "https://github.com/someone/demo"])
    assert "Indexed someone/demo" in capsys.readouterr().out

    answer = json.dumps({"answer": "MIT.", "found": True, "sources": ["LICENSE"]})
    fake = make_client(FakeTransport(reply(answer)))
    monkeypatch.setattr(chat_cli, "build_llm_client", lambda settings, usage, cache, metrics: fake)
    questions = iter(["What license is it under? MIT License?", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(questions))
    main(["chat", "someone/demo"])
    out = capsys.readouterr().out
    assert "MIT." in out and "Sources: LICENSE" in out


def test_chat_about_an_unindexed_repo_says_how_to_index_it(in_empty_project, capsys):
    with pytest.raises(SystemExit):
        main(["chat", "nobody/nothing"])
    assert "setuppilot index" in capsys.readouterr().err


def test_resume_refuses_a_cancelled_run(in_empty_project, monkeypatch, capsys, empty_pg):
    from setuppilot.db.postgres import PostgresRunStore
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("SUPABASE_DB_URL", TEST_DATABASE_URL)
    runs = PostgresRunStore(empty_pg)
    run = runs.create_run("https://github.com/someone/demo", "/home/dev/work")
    runs.set_status(run.id, "cancelled", "RunCancelled", "You cancelled this run.")
    use_fake_agent(monkeypatch, make_repo(in_empty_project / "source", {"requirements.txt": ""}))
    code = run_cli_with_answers(monkeypatch, [], ["resume", run.id])
    assert code == 1 and "was cancelled" in capsys.readouterr().err
