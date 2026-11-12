"""scripts/acceptance.py: the parts that decide PASS and FAIL, on every system. The parts that
start programs are checked by running the script itself."""

import importlib.util
import sys
from pathlib import Path

import pytest

from setuppilot.chat.bot import index_repo
from setuppilot.chat.index import InMemoryChunkIndex
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.stores import Stores
from setuppilot.errors import LLMUnavailable
from setuppilot.evaluation.chat_eval import results_table, score_answer
from setuppilot.evaluation.run_chat import run_chat_eval
from setuppilot.evaluation.run_setup import run_setup_eval
from setuppilot.evaluation.setup_eval import SetupScore, setup_table
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.usage import InMemoryUsageStore
from tests.conftest import TEST_DATABASE_URL, TEST_DB_CONFIG
from tests.fakes import FakeEmbedder, FakeTransport, make_client, make_repo, reply_citing
from tests.test_agent import PIP_REPO, FakeRunner, make_agent
from tests.test_chat_eval import (
    LICENSE_CASE,
    REPO,
    SETTINGS,
    UNKNOWN_CASE,
    answer,
    case,
    stores_with,
)
from tests.test_setup_eval import CASE


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "acceptance.py"
    spec = importlib.util.spec_from_file_location("acceptance", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["acceptance"] = module
    spec.loader.exec_module(module)
    return module


acceptance = _load_script()
ALL_PARTS = ["basics", "groq", "supabase", "setup", "docker", "chat", "browser"]


def test_no_part_named_runs_every_part_in_order():
    assert acceptance.chosen_parts([]) == ALL_PARTS


def test_named_parts_run_in_the_fixed_order():
    assert acceptance.chosen_parts(["browser", "basics"]) == ["basics", "browser"]


def test_an_unknown_part_is_refused_with_the_list_of_parts():
    with pytest.raises(ValueError, match="Unknown part: chats. Pick from: basics groq supabase"):
        acceptance.chosen_parts(["basics", "chats"])


@pytest.mark.parametrize(
    ("hostname", "folder"),
    [
        ("Rishabs-MacBook-Pro.local", "20261112-201500-Rishabs-MacBook-Pro"),
        ("DESKTOP_7Q2 PC", "20261112-201500-DESKTOP-7Q2-PC"),
    ],
)
def test_results_folder_is_the_time_and_the_short_machine_name(hostname, folder):
    assert acceptance.results_folder_name("20261112-201500", hostname) == folder


def test_a_setting_in_dotenv_counts_when_the_variable_is_not_set():
    assert acceptance.app_setting("GROQ_API_KEY", {}, {"GROQ_API_KEY": "gsk_x"}) == "gsk_x"


def test_a_variable_set_to_nothing_hides_dotenv_as_load_dotenv_does():
    environ = {"GROQ_API_KEY": ""}
    assert acceptance.app_setting("GROQ_API_KEY", environ, {"GROQ_API_KEY": "gsk_x"}) == ""


def test_a_name_in_dotenv_without_a_value_is_not_a_setting():
    assert acceptance.app_setting("GROQ_API_KEY", {}, {"GROQ_API_KEY": None}) == ""


@pytest.mark.parametrize("host", ["posix", "windows"])
def test_basics_check_types_for_every_system_wherever_they_run(host):
    checks = acceptance.basics_checks(host)
    assert [c.argv[-1] for c in checks if "mypy" in c.argv] == ["linux", "win32", "darwin"]


def test_basics_on_windows_run_the_tests_as_the_windows_ci_job_does():
    (tests,) = [c for c in acceptance.basics_checks("windows") if c.name == "unit tests"]
    assert (tests.argv, tests.env) == (("uv", "run", "pytest"), {"PYTHONWARNDEFAULTENCODING": "1"})


def test_basics_on_windows_leave_the_coverage_gate_to_macos_and_linux():
    names = [c.name for c in acceptance.basics_checks("windows")]
    assert acceptance.GUARDRAIL_COVERAGE not in names


def test_basics_on_macos_and_linux_gate_guardrail_coverage_at_90():
    checks = {c.name: c for c in acceptance.basics_checks("posix")}
    gate = checks[acceptance.GUARDRAIL_COVERAGE].argv[-2:]
    assert gate == ("--include=src/setuppilot/guardrails/*", "--fail-under=90")


def test_newest_table_is_the_latest_one_of_its_kind():
    names = ["setup-2026-11-02-1428.md", "setup-2026-11-02-0902.md", "setup.jsonl"]
    assert acceptance.newest_table(names, "setup") == "setup-2026-11-02-1428.md"


def test_newest_table_of_a_kind_ignores_a_kind_whose_name_starts_with_it():
    assert acceptance.newest_table(["setup-docker-2026-11-03-0101.md"], "setup") is None


def _setup_result_file(tmp_path, runner):
    """The table run_setup_eval writes for one pip repo."""
    agent, deps = make_agent(tmp_path, PIP_REPO, runner=runner)
    stores = Stores(
        deps.runs, InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore(),
        InMemoryChunkIndex(), None, None,
    )  # fmt: skip
    run_setup_eval(stores, agent, [CASE], tmp_path / "ws", tmp_path / "out", use_docker=False)
    (table,) = (tmp_path / "out").glob("setup-*.md")
    return table.read_text(encoding="utf-8")


def test_setup_table_of_a_verified_evaluation_passes(tmp_path):
    verdict = acceptance.judge_all_verified(_setup_result_file(tmp_path, FakeRunner()))
    assert verdict == acceptance.Verdict(True, "1 of 1 verified")


def test_setup_table_with_a_repo_not_verified_fails(tmp_path):
    runner = FakeRunner(failing={"uv pip install --python .venv -r requirements.txt"})
    verdict = acceptance.judge_all_verified(_setup_result_file(tmp_path, runner))
    assert verdict == acceptance.Verdict(False, "0 of 1 verified")


def test_setup_table_that_stopped_early_fails_even_if_every_repo_done_was_verified():
    score = SetupScore(CASE.url, "python/pip", True, True, True, None, 1.0, 0, 2)
    table = f"1 of 2 repos done.\n\n{setup_table([score])}\n"
    verdict = acceptance.judge_all_verified(table)
    assert verdict == acceptance.Verdict(False, "only 1 of 2 done, so its scores do not count")


def test_setup_table_with_no_repos_fails_instead_of_passing_0_of_0_verified():
    table = f"0 of 0 repos done.\n\n{setup_table([])}\n"
    verdict = acceptance.judge_all_verified(table)
    assert verdict == acceptance.Verdict(False, "only 0 of 0 done, so its scores do not count")


def _chat_stores(tmp_path):
    index = InMemoryChunkIndex()
    folder = make_repo(tmp_path / "demo", {"LICENSE": "MIT License text", "README.md": "# Demo"})
    index_repo(REPO, folder, SETTINGS.chat, 2000, FakeEmbedder(), index)
    return stores_with(index)


def _chat_result_file(folder, mode):
    (table,) = folder.glob(f"chat-{mode}-*.md")
    return table.read_text(encoding="utf-8")


def test_chat_table_of_a_complete_evaluation_passes(tmp_path):
    out, stores = tmp_path / "out", _chat_stores(tmp_path)
    run_chat_eval(SETTINGS, stores, [LICENSE_CASE], "retrieval", out, FakeEmbedder(), None)
    verdict = acceptance.judge_grounded(_chat_result_file(out, "retrieval"))
    assert verdict == acceptance.Verdict(True, "1 of 1 grounded, target 80%")


def test_chat_table_of_a_stopped_evaluation_fails_even_at_100_percent(tmp_path):
    limit = LLMUnavailable("Daily limit reached.", "x")
    # The BIG model and then the SMALL one both hit the limit on the second question.
    llm = make_client(FakeTransport(reply_citing("LICENSE", "MIT"), limit, limit))
    cases, out = [LICENSE_CASE, case("Is it MIT License text?")], tmp_path / "out"
    run_chat_eval(SETTINGS, _chat_stores(tmp_path), cases, "full", out, FakeEmbedder(), llm)
    verdict = acceptance.judge_grounded(_chat_result_file(out, "full"))
    assert verdict == acceptance.Verdict(False, "only 1 of 2 done, so its scores do not count")


def _chat_table(grounded, answerable):
    """A complete chat table where `grounded` of `answerable` answers cite the right file."""
    scores = [
        score_answer(LICENSE_CASE, answer("MIT", ["LICENSE" if n < grounded else "README.md"]))
        for n in range(answerable)
    ]
    return f"{answerable} of {answerable} questions done.\n\n{results_table(scores)}\n"


def test_chat_table_exactly_at_the_target_passes():
    verdict = acceptance.judge_grounded(_chat_table(20, 25))
    assert verdict == acceptance.Verdict(True, "20 of 25 grounded, target 80%")


def test_chat_table_just_under_the_target_fails():
    verdict = acceptance.judge_grounded(_chat_table(19, 24))
    assert verdict == acceptance.Verdict(False, "19 of 24 grounded, target 80%")


def test_chat_grounding_is_read_from_the_grounded_column_not_correct():
    # Every answer has the right words (Correct 25/25), but none came from the right file.
    assert not acceptance.judge_grounded(_chat_table(0, 25)).is_pass


def test_chat_table_with_no_answerable_question_fails():
    scores = [score_answer(UNKNOWN_CASE, answer("I could not find this.", status="not_found"))]
    table = f"1 of 1 questions done.\n\n{results_table(scores)}\n"
    verdict = acceptance.judge_grounded(table)
    assert verdict == acceptance.Verdict(False, "no answerable question was scored")


@pytest.mark.parametrize(("done", "is_pass"), [(74, True), (73, False)])
def test_replay_passes_only_when_every_question_came_back(done, is_pass):
    assert acceptance.judge_complete(f"{done} of 74 questions done.\n").is_pass is is_pass


def test_a_table_without_its_done_line_fails():
    verdict = acceptance.judge_complete(_chat_table(1, 1).split("\n", 1)[1])
    assert verdict == acceptance.Verdict(False, "the table has no 'N of N ... done' line")


def test_a_table_without_a_total_row_is_a_clear_error():
    table = "1 of 1 repos done.\n\n| Repo | Verified |\n|---|---|\n| a | yes |\n"
    with pytest.raises(ValueError, match="no Total row"):
        acceptance.judge_all_verified(table)


def test_docker_mode_keeps_repos_with_an_image_and_compose_repos():
    managers = ["pip", "poetry", "compose", "pnpm", "cargo"]
    repos = [{"url": f"https://github.com/a/{m}", "expected_manager": m} for m in managers]
    kept = [r["expected_manager"] for r in acceptance.docker_mode_repos(repos)]
    assert kept == ["pip", "compose", "cargo"]


def test_the_paused_run_id_is_read_from_the_resume_hint():
    output = "Approve? q\n\nPaused. Continue later with: setuppilot resume 3f2c9a1e-77b0-4d0c\n"
    assert acceptance.paused_run_id(output) == "3f2c9a1e-77b0-4d0c"


def test_no_resume_hint_gives_no_run_id():
    output = "Stopped. Without a database this run cannot be resumed; start a new run."
    assert acceptance.paused_run_id(output) is None


def test_chrome_on_windows_is_looked_for_where_playwright_looks():
    environ = {
        "LOCALAPPDATA": r"C:\Users\me\AppData\Local", "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMFILES(X86)": r"C:\Program Files (x86)", "HOMEDRIVE": "D:",
    }  # fmt: skip
    assert acceptance.chrome_paths("win32", environ) == [
        r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"D:\Program Files\Google\Chrome\Application\chrome.exe",
        r"D:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]


def test_chrome_on_windows_skips_folders_whose_variable_is_not_set():
    paths = acceptance.chrome_paths("win32", {"PROGRAMFILES": r"C:\Program Files"})
    assert paths == [r"C:\Program Files\Google\Chrome\Application\chrome.exe"]


@pytest.mark.parametrize(
    ("system", "path"),
    [
        ("darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("linux", "/opt/google/chrome/chrome"),
        ("freebsd14", None),
    ],
)
def test_chrome_on_macos_and_linux_is_in_one_fixed_place(system, path):
    assert acceptance.chrome_paths(system, {}) == ([path] if path else [])


def test_compose_files_are_found_at_the_top_of_each_cloned_repo(tmp_path):
    eval_repo = make_repo(tmp_path / "eval/a__b/b", {"compose.yaml": "", "sub/compose.yaml": ""})
    docker_repo = make_repo(tmp_path / "docker/c__d/d", {"docker-compose.yml": ""})
    assert acceptance.compose_files(tmp_path) == [
        docker_repo / "docker-compose.yml",
        eval_repo / "compose.yaml",
    ]


def test_row_security_check_finds_nothing_wrong_in_the_app_tables(pg_pool):
    assert acceptance.row_security_problems(TEST_DATABASE_URL, TEST_DB_CONFIG) == []


def test_row_security_check_names_a_table_without_it(pg_pool):
    with pg_pool.connection() as conn:
        conn.execute("create table acceptance_open (id int)")
    try:
        problems = acceptance.row_security_problems(TEST_DATABASE_URL, TEST_DB_CONFIG)
    finally:
        with pg_pool.connection() as conn:
            conn.execute("drop table acceptance_open")
    assert problems == ["no row level security on acceptance_open"]
