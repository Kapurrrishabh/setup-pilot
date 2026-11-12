"""Score the setup agent on real repos: right stack, right install command, verified result."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from setuppilot.agent.driver import Finished, SetupAgent, Waiting
from setuppilot.db.runs import RunStore
from setuppilot.guardrails.commands import ExecTarget
from setuppilot.guardrails.docker_mode import exec_target
from setuppilot.llm.usage import UsageStore
from setuppilot.tools.clone import CloneRequest

_COLUMNS = ("Repo", "Detected", "Stack right", "Install right", "Verified", "Error",
            "Time (s)", "Tokens", "Approvals")  # fmt: skip


@dataclass(frozen=True)
class SetupCase:
    url: str
    expected_stack: str
    expected_manager: str
    # Where the layout differs, so do the commands: ./mvnw on Linux, ./mvnw.cmd on Windows.
    expected_install: Mapping[ExecTarget, tuple[str, ...]]


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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The table counts every result but "verified" as a miss, so it cannot score other guesses.
    if others := [r["url"] for r in data["repos"] if r["expected_result"] != "verified"]:
        raise ValueError(
            f"expected_result must be 'verified' for {others}, because the table counts any other "
            f"result as a miss. Set it to verified or remove the repo from {path}."
        )
    return [
        SetupCase(
            r["url"],
            r["expected_stack"],
            r["expected_manager"],
            _expected_install(r["url"], r["expected_install"], path),
        )  # fmt: skip
        for r in data["repos"]
    ]


def _expected_install(url: str, value: Any, path: Path) -> dict[ExecTarget, tuple[str, ...]]:
    """One list for every system, or {posix: [...], windows: [...]} where they differ."""
    if isinstance(value, list):
        return {"posix": tuple(value), "windows": tuple(value)}
    if not isinstance(value, dict) or set(value) != {"posix", "windows"}:
        raise ValueError(
            f"expected_install for {url} must be a list, or posix and windows lists, not "
            f"{value!r}. Fix it in {path}."
        )
    return {"posix": tuple(value["posix"]), "windows": tuple(value["windows"])}


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
    use_docker: bool, host: ExecTarget, clock: Callable[[], float] = time.monotonic,
) -> SetupScore:  # fmt: skip
    """host: the system the agent runs on, which picks the expected install commands."""
    target = exec_target(host, use_docker, case.expected_manager)
    started = clock()
    outcome: Waiting | Finished = agent.start(
        case.url, str(dest), use_docker=use_docker, request=CloneRequest("public", None)
    )
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
        is_install_right=tuple(installs) == case.expected_install[target],
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
    lines = path.read_text(encoding="utf-8").splitlines()
    return [SetupScore(**json.loads(line)) for line in lines if line]


def save_setup_score(path: Path, score: SetupScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(score)) + "\n")
