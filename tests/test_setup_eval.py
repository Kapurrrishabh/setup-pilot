import shutil

import pytest

from setuppilot import __main__ as cli
from setuppilot.__main__ import main
from setuppilot.agent.driver import Finished
from setuppilot.chat.index import InMemoryChunkIndex
from setuppilot.evaluation.run_setup import run_setup_eval
from setuppilot.evaluation.setup_eval import (
    SetupCase,
    SetupScore,
    auto_answer,
    load_setup_cases,
    run_setup_case,
    setup_table,
)
from tests.fakes import make_repo
from tests.test_agent import PIP_REPO, make_agent
from tests.test_chat_eval import stores_with
from tests.test_config import REAL_CONFIG

CASE = SetupCase(
    "https://github.com/someone/demo", "python", "pip",
    ("uv venv .venv", "uv pip install --python .venv -r requirements.txt"),
)  # fmt: skip
REPOS_YAML = (
    "repos:\n  - url: https://github.com/a/b\n    expected_stack: go\n"
    "    expected_manager: go\n    expected_install: [go mod download]\n"
    "    expected_result: verified\n"
)


def test_evaluator_picks_the_expected_stack():
    question = {"kind": "choose_stack", "options": [{"label": "node/npm"}, {"label": "python/pip"}]}
    assert auto_answer(CASE, question) == {"choice": 1}


def test_evaluator_skips_readme_suggestions_but_runs_its_own_commands():
    readme = {"kind": "approve_command", "reason": "From the README: needed"}
    own = {"kind": "approve_command", "reason": "Install the packages listed."}
    assert (auto_answer(CASE, readme), auto_answer(CASE, own)) == (
        {"action": "reject"},
        {"action": "approve"},
    )


def test_a_pip_repo_scores_right_stack_right_install_and_verified(tmp_path):
    agent, deps = make_agent(tmp_path, PIP_REPO)
    score = run_setup_case(
        CASE, agent, deps.runs, _NoUsage(), tmp_path / "dest", False, clock=lambda: 0.0
    )
    assert (score.detected, score.is_stack_right, score.is_install_right, score.is_verified) == (
        "python/pip",
        True,
        True,
        True,
    )
    assert score.approvals == 2


def test_a_wrong_expected_install_is_scored_wrong(tmp_path):
    agent, deps = make_agent(tmp_path, PIP_REPO)
    wrong = SetupCase(CASE.url, "python", "pip", ("uv sync",))
    assert not run_setup_case(
        wrong, agent, deps.runs, _NoUsage(), tmp_path / "dest", False
    ).is_install_right


def test_table_has_a_total_row():
    score = SetupScore(CASE.url, "python/pip", True, True, False, "VerifyFailed", 12.0, 0, 2)
    table = setup_table([score])
    assert "| **Total (1)** |  | 1/1 (100%) | 1/1 (100%) | 0/1 (0%) |" in table


def test_cases_load_from_yaml(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(REPOS_YAML)
    assert load_setup_cases(path)[0].expected_install == ("go mod download",)


def test_repo_expected_not_to_verify_is_refused_because_the_table_cannot_score_it(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(REPOS_YAML.replace("expected_result: verified", "expected_result: failed"))
    with pytest.raises(ValueError, match="expected_result must be 'verified'"):
        load_setup_cases(path)


def test_eval_setup_docker_flag_reaches_the_agent(tmp_path, monkeypatch):
    shutil.copy(REAL_CONFIG, tmp_path / "config.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    (tmp_path / "repos.yaml").write_text(REPOS_YAML)
    made = []

    def make_spy(settings, stores):
        made.append(_DockerSpy(stores.runs))
        return made[0]

    monkeypatch.setattr(cli, "make_agent", make_spy)
    main(
        ["eval-setup", "--docker", "--repos", "repos.yaml", "--workspace", "ws", "--results", "out"]
    )
    assert made[0].use_docker == [True]


def test_docker_eval_does_not_carry_on_from_native_scores(tmp_path):
    stores = stores_with(InMemoryChunkIndex())
    agent = _DockerSpy(stores.runs)
    for use_docker in (False, True):
        run_setup_eval(stores, agent, [CASE], tmp_path / "ws", tmp_path / "out", use_docker)
    assert agent.use_docker == [False, True]


def test_folder_the_evaluator_cannot_clear_shows_the_real_error(tmp_path):
    stores = stores_with(InMemoryChunkIndex())
    locked = make_repo(tmp_path / "ws/someone__demo/locked", {"file": "x"})
    locked.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            run_setup_eval(
                stores, _DockerSpy(stores.runs), [CASE], tmp_path / "ws", tmp_path / "out", False
            )
    finally:
        locked.chmod(0o700)


class _NoUsage:
    def tokens_for_run(self, run_id):
        return 0


class _DockerSpy:
    """Notes the Docker setting each run starts with, and finishes the run at once."""

    def __init__(self, runs):
        self._runs = runs
        self.use_docker = []

    def start(self, repo_url, dest_dir, use_docker):
        self.use_docker.append(use_docker)
        return Finished(self._runs.create_run(repo_url, dest_dir).id, "", False, None, None)
