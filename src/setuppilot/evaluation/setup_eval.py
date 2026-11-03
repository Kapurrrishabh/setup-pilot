"""Score the setup agent on real repos: right stack, right install command, verified result."""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from setuppilot.agent.driver import Finished, SetupAgent, Waiting
from setuppilot.db.runs import RunStore
from setuppilot.llm.usage import UsageStore

_COLUMNS = ("Repo", "Detected", "Stack right", "Install right", "Verified", "Error",
            "Time (s)", "Tokens", "Approvals")  # fmt: skip


@dataclass(frozen=True)
class SetupCase:
    url: str
    expected_stack: str
    expected_manager: str
    expected_install: tuple[str, ...]
    expected_result: str


@dataclass(frozen=True)
class SetupScore:
    url: str
    detected: str | None
    is_stack_right: bool
    is_install_right: bool
    is_verified: bool
    error_type: str | None
    seconds: float
    tokens: int
    approvals: int


def load_setup_cases(path: Path) -> list[SetupCase]:
    data = yaml.safe_load(path.read_text())
    return [
        SetupCase(
            r["url"],
            r["expected_stack"],
            r["expected_manager"],
            tuple(r["expected_install"]),
            r["expected_result"],
        )  # fmt: skip
        for r in data["repos"]
    ]


def auto_answer(case: SetupCase, question: dict) -> dict:
    """The evaluator's answers: pick the expected stack, run SetupPilot's own commands and
    checks, and skip README suggestions so every run is the same."""
    if question["kind"] == "choose_stack":
        labels = [o["label"] for o in question["options"]]
        wanted = f"{case.expected_stack}/{case.expected_manager}"
        return {"choice": labels.index(wanted) if wanted in labels else 0}
    is_readme_tip = question["reason"].startswith("From the README")
    return {"action": "reject" if is_readme_tip else "approve"}


def run_setup_case(
    case: SetupCase, agent: SetupAgent, runs: RunStore, usage: UsageStore, dest: Path,
    clock: Callable[[], float] = time.monotonic,
) -> SetupScore:  # fmt: skip
    started = clock()
    outcome: Waiting | Finished = agent.start(case.url, str(dest), use_docker=False)
    approvals, installs = 0, []
    while isinstance(outcome, Waiting):
        reply = auto_answer(case, outcome.question)
        if reply.get("action") == "approve":
            approvals += 1
            if not outcome.question["is_check"]:
                installs.append(outcome.question["command"])
        outcome = agent.answer(outcome.run_id, outcome.question_id, reply)
    run = runs.get_run(outcome.run_id)
    return SetupScore(
        url=case.url,
        detected=outcome.stack,
        is_stack_right=outcome.stack == f"{case.expected_stack}/{case.expected_manager}",
        is_install_right=tuple(installs) == case.expected_install,
        is_verified=outcome.is_verified,
        error_type=run.error_type,
        seconds=round(clock() - started, 1),
        tokens=usage.tokens_for_run(run.id),
        approvals=approvals,
    )


def setup_table(scores: list[SetupScore]) -> str:
    rows = [
        [s.url, s.detected or "-", _yes(s.is_stack_right), _yes(s.is_install_right),
         _yes(s.is_verified), s.error_type or "", f"{s.seconds:.0f}", str(s.tokens),
         str(s.approvals)]
        for s in scores
    ]  # fmt: skip
    n = max(len(scores), 1)
    rows.append(
        [f"**Total ({len(scores)})**", "", _rate(scores, "is_stack_right"),
         _rate(scores, "is_install_right"), _rate(scores, "is_verified"), "",
         f"{sum(s.seconds for s in scores) / n:.0f} avg",
         f"{sum(s.tokens for s in scores) / n:.0f} avg",
         f"{sum(s.approvals for s in scores) / n:.1f} avg"]
    )  # fmt: skip
    lines = ["| " + " | ".join(_COLUMNS) + " |", "|" + "---|" * len(_COLUMNS)]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows])


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _rate(scores: list[SetupScore], field: str) -> str:
    hits = sum(getattr(s, field) for s in scores)
    return f"{hits}/{len(scores)} ({hits / max(len(scores), 1):.0%})"


def load_setup_done(path: Path) -> list[SetupScore]:
    if not path.exists():
        return []
    return [SetupScore(**json.loads(line)) for line in path.read_text().splitlines() if line]


def save_setup_score(path: Path, score: SetupScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(asdict(score)) + "\n")
