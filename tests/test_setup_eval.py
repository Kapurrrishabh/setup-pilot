from setuppilot.evaluation.setup_eval import (
    SetupCase,
    SetupScore,
    auto_answer,
    load_setup_cases,
    run_setup_case,
    setup_table,
)
from tests.test_agent import PIP_REPO, make_agent

CASE = SetupCase(
    "https://github.com/someone/demo", "python", "pip",
    ("uv venv .venv", "uv pip install --python .venv -r requirements.txt"), "verified",
)  # fmt: skip


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
    score = run_setup_case(CASE, agent, deps.runs, _NoUsage(), tmp_path / "dest", clock=lambda: 0.0)
    assert (score.detected, score.is_stack_right, score.is_install_right, score.is_verified) == (
        "python/pip",
        True,
        True,
        True,
    )
    assert score.approvals == 2


def test_a_wrong_expected_install_is_scored_wrong(tmp_path):
    agent, deps = make_agent(tmp_path, PIP_REPO)
    wrong = SetupCase(CASE.url, "python", "pip", ("uv sync",), "verified")
    assert not run_setup_case(
        wrong, agent, deps.runs, _NoUsage(), tmp_path / "dest"
    ).is_install_right


def test_table_has_a_total_row():
    score = SetupScore(CASE.url, "python/pip", True, True, False, "VerifyFailed", 12.0, 0, 2)
    table = setup_table([score])
    assert "| **Total (1)** |  | 1/1 (100%) | 1/1 (100%) | 0/1 (0%) |" in table


def test_cases_load_from_yaml(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        "repos:\n  - url: https://github.com/a/b\n    expected_stack: go\n"
        "    expected_manager: go\n    expected_install: [go mod download]\n"
        "    expected_result: verified\n"
    )
    assert load_setup_cases(path)[0].expected_install == ("go mod download",)


class _NoUsage:
    def tokens_for_run(self, run_id):
        return 0
