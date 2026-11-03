"""The fixed setup flow. Plain Python does every predictable step. The LLM only reads the README
for extra steps and writes the final report; it can propose commands but never run them."""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from setuppilot.agent.prompts import readme_tips_messages, report_messages
from setuppilot.config import Settings
from setuppilot.db.runs import RunStatus, RunStore, Step
from setuppilot.errors import (
    CommandBlocked,
    InstallFailed,
    PathBlocked,
    PrereqMissing,
    RunCancelled,
    SetupPilotError,
    StackNotFound,
    TooManySteps,
    VerifyFailed,
)
from setuppilot.guardrails.commands import INSTALL_SCRIPT_WARNING, validate_command
from setuppilot.guardrails.docker_mode import docker_image_for, in_docker
from setuppilot.guardrails.paths import PathGuard
from setuppilot.guardrails.runner import CommandOutput
from setuppilot.llm.client import LLMClient
from setuppilot.redact import redact
from setuppilot.setup.plans import (
    SUPPORTED_MANAGERS,
    code_checks,
    install_plan,
    is_installable_project,
    quiet_checks,
)
from setuppilot.tools.compose import find_compose_files
from setuppilot.tools.detect import StackCandidate, detect_stacks
from setuppilot.tools.github_url import RepoRef, parse_github_url
from setuppilot.tools.prereqs import ToolProbe, check_prerequisites
from setuppilot.tools.scan import scan_repo

log = logging.getLogger(__name__)

# Only the end of command output is kept in the saved run state, to keep the database small.
_STATE_OUTPUT_CHARS = 2000
_README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")
_MAX_TIPS = 3
_MAX_MANUAL_STEPS = 5


class RunState(TypedDict, total=False):
    run_id: str
    repo_url: str
    dest_dir: str
    use_docker: bool
    repo_dir: str
    candidates: list[dict]
    conflicts: list[str]
    notes: list[str]
    prereqs: dict[str, list[dict]]
    chosen: dict
    plan: list[dict]
    manual_steps: list[str]
    next_index: int
    decision: str
    edit_error: str | None
    checks: list[dict]
    is_verified: bool
    error: dict | None
    report: str
    steps: int


@dataclass(frozen=True)
class AgentDeps:
    settings: Settings
    runs: RunStore
    llm: LLMClient | None
    clone: Callable[[RepoRef, Path], Path]
    probe: ToolProbe
    run: Callable[[Any], CommandOutput]


@dataclass(frozen=True)
class StepNote:
    """What one node did, for the run's timeline."""

    summary: str
    tool: str | None = None
    arguments: dict | None = None
    exit_code: int | None = None
    tokens: int = 0
    model: str | None = None


NodeFn = Callable[[RunState], tuple[dict, StepNote]]


def build_graph(deps: AgentDeps) -> StateGraph:
    graph = StateGraph(RunState)
    nodes: dict[str, NodeFn] = {
        "clone": lambda s: _clone(deps, s),
        "scan": _scan,
        "detect": _detect,
        "prerequisites": lambda s: _prerequisites(deps, s),
        "choose_stack": lambda s: _choose_stack(deps, s),
        "plan": lambda s: _plan(deps, s),
        "approve": _approve,
        "execute": lambda s: _execute(deps, s),
        "verify": lambda s: _verify(deps, s),
    }
    for name, fn in nodes.items():
        graph.add_node(name, _recorded(name, deps, fn))
    graph.add_node("report", lambda s: _report(deps, s))

    def then(next_node: str) -> Callable[[RunState], str]:
        return lambda s: "report" if s.get("error") else next_node

    graph.add_edge(START, "clone")
    for name, nxt in [
        ("clone", "scan"),
        ("scan", "detect"),
        ("detect", "prerequisites"),
        ("prerequisites", "choose_stack"),
        ("choose_stack", "plan"),
    ]:
        graph.add_conditional_edges(name, then(nxt))
    graph.add_conditional_edges("plan", lambda s: "report" if s.get("error") else _next_pending(s))
    graph.add_conditional_edges("approve", _after_approve)
    graph.add_conditional_edges(
        "execute", lambda s: "report" if s.get("error") else _next_pending(s)
    )
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph


# Not Callable[[RunState], dict]: LangGraph's node type needs the argument to be named `state`.
def _recorded(name: str, deps: AgentDeps, fn: NodeFn) -> Callable[..., dict]:
    """Run a node, count it, turn known errors into state, and save the step to the timeline."""

    def node(state: RunState) -> dict:
        steps = state.get("steps", 0)
        started = time.monotonic()
        try:
            if deps.runs.get_run(state["run_id"]).status == "cancelled":
                raise _cancelled()
            if steps >= deps.settings.agent.max_steps:
                raise TooManySteps(
                    "The run took too many steps and was stopped.",
                    details=f"Limit: agent.max_steps = {deps.settings.agent.max_steps}.",
                )
            update, note = fn(state)
        except SetupPilotError as error:
            update, note = {"error": _error(error)}, StepNote(error.message)
        _save_step(deps, state["run_id"], name, note, update.get("error"), started)
        return update | {"steps": steps + 1}

    return node


def _save_step(
    deps: AgentDeps, run_id: str, name: str, note: StepNote, error: dict | None, started: float
) -> None:
    """Save one step to the timeline and the log, with secrets hidden."""
    step = Step(
        run_id,
        name,
        note.tool,
        json.loads(redact(json.dumps(note.arguments or {}))),
        redact(note.summary),
        error is None,
        error["error_type"] if error else None,
        note.exit_code,
        int((time.monotonic() - started) * 1000),
        note.tokens,
        note.model,
    )
    deps.runs.add_step(step)
    fields = asdict(step)
    log.info("step", extra={"step": fields.pop("name")} | fields)


def _clone(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    ref = parse_github_url(state["repo_url"])
    target = deps.clone(ref, Path(state["dest_dir"]))
    return {"repo_dir": str(target)}, StepNote(
        f"Cloned {ref.url} to {target}.", "git", {"url": ref.url}
    )


def _scan(state: RunState) -> tuple[dict, StepNote]:
    repo = Path(state["repo_dir"])
    scan = scan_repo(repo)
    escaping = PathGuard(repo).links_that_escape(scan.skipped_symlinks)
    if escaping:
        raise PathBlocked(
            "This repo has links that point outside its folder, so it was not set up.",
            details="Links: " + ", ".join(escaping),
        )
    if scan.skipped_dirs:
        raise CommandBlocked(
            "This repo ships its own "
            + " and ".join(scan.skipped_dirs)
            + ", so it was not set up.",
            details="Tools would run programs from those folders. Delete them from the cloned "
            "folder yourself if you trust the repo, then start a new run.",
        )
    links = len(scan.skipped_symlinks)
    summary = _count(len(scan.files), "file") + (
        f"; {_count(links, 'link')} not followed" * bool(links)
    )
    summary += "."
    return {}, StepNote(summary)


def _detect(state: RunState) -> tuple[dict, StepNote]:
    detection = detect_stacks(scan_repo(Path(state["repo_dir"])))
    candidates = [asdict(c) for c in detection.candidates]
    labels = ", ".join(f"{c['stack']}/{c['manager']}" for c in candidates) or "none"
    update = {
        "candidates": candidates,
        "conflicts": list(detection.conflicts),
        "notes": list(detection.notes),
    }
    return update, StepNote(f"Found: {labels}.")


def _prerequisites(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    prereqs = {
        _key(c): [asdict(check) for check in check_prerequisites(_candidate(c), deps.probe)]
        for c in state["candidates"]
    }
    missing = sorted(
        {c["tool"] for checks in prereqs.values() for c in checks if not c["is_installed"]}
    )
    summary = (
        f"Missing tools: {', '.join(missing)}." if missing else "All needed tools are installed."
    )
    return {"prereqs": prereqs}, StepNote(summary)


def _choose_stack(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    candidates = [c for c in state["candidates"] if c["manager"] in SUPPORTED_MANAGERS]
    unsupported = [_key(c) for c in state["candidates"] if c not in candidates]
    if not candidates:
        found = ", ".join(unsupported) or "no setup files at all"
        raise StackNotFound(
            "SetupPilot found nothing it can set up in this repo.",
            details=f"Found: {found}. " + " ".join(state.get("notes", [])),
        )
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        answer = interrupt(
            {
                "kind": "choose_stack",
                "options": [
                    {
                        "label": _key(c),
                        "evidence": c["evidence"],
                        "prereqs": state["prereqs"][_key(c)],
                    }
                    for c in candidates
                ],
                "conflicts": state.get("conflicts", []),
            }
        )
        index = answer.get("choice")
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            raise StackNotFound("That choice is not one of the options.", details=f"Got {index!r}.")
        chosen = candidates[index]
    notes = list(state.get("notes", []))
    notes += [f"Not offered, SetupPilot cannot set it up yet: {u}." for u in unsupported]
    if state.get("use_docker") and not _runs_on_host(state | {"chosen": chosen}):
        docker_image_for(chosen["manager"])
        docker = StackCandidate("docker", "dockerfile", (), None)
        checks = [asdict(c) for c in check_prerequisites(docker, deps.probe)]
    else:
        checks = state["prereqs"][_key(chosen)]
    missing = [c for c in checks if not c["is_installed"]]
    if missing:
        # The stack is still recorded, so the report and the evaluation show what was found.
        error = PrereqMissing(
            "Some tools this repo needs are not installed: "
            + ", ".join(c["tool"] for c in missing)
            + ".",
            details="\n".join(f"{c['tool']}: {c['problem']}" for c in missing),
        )
        update = {"chosen": chosen, "notes": notes, "error": _error(error)}
        return update, StepNote(error.message)
    notes += [c["problem"] for c in checks if c["problem"]]
    return {"chosen": chosen, "notes": notes}, StepNote(f"Chose {_key(chosen)}.")


def _plan(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    candidate = _candidate(state["chosen"])
    repo = Path(state["repo_dir"])
    scan = scan_repo(repo)
    top = frozenset(f for f in scan.files if "/" not in f)
    installs = install_plan(candidate, top, is_installable_project(repo))
    plan = [_item(c.text, c.reason, "install") for c in installs]
    if candidate.manager == "compose":
        (compose,) = find_compose_files(repo, (candidate.evidence[0],))
        if compose.warnings:
            plan[0]["reason"] += " Careful: " + " ".join(compose.warnings)
    checks = [_item(c.text, c.reason, "check") for c in code_checks(candidate, scan.files)]
    planned = [p["text"] for p in plan + checks]
    tips, manual_steps, note, skip_reason = _readme_tips(deps, state, candidate, planned, top)
    guard = PathGuard(repo)
    for tip in tips:
        item = _item(tip["command"], f"From the README: {tip['reason']}", "readme")
        try:
            validate_command(item["text"], guard)
        except CommandBlocked as error:
            item |= {"status": "blocked", "block_reason": error.details}
        if item["text"] not in planned:
            plan.append(item)
            planned.append(item["text"])
    plan += checks
    notes = list(state.get("notes", [])) + ([skip_reason] if skip_reason else [])
    update = {"plan": plan, "manual_steps": manual_steps, "next_index": 0, "notes": notes}
    pending = sum(p["status"] == "pending" for p in plan)
    manual = len(manual_steps)
    summary = f"{_count(pending, 'command')} to approve"
    summary += f"; the README lists {_count(manual, 'manual step')}." if manual else "."
    return update, StepNote(
        summary, "llm" if note.model else None, None, None, note.tokens, note.model
    )


def _readme_tips(
    deps: AgentDeps, state: RunState, candidate: StackCandidate, planned: list[str], top: frozenset
) -> tuple[list[dict], list[str], StepNote, str | None]:
    """Ask the LLM for extra steps from the README. They are optional, so any failure here
    becomes a note (the last value) and the run goes on."""
    readme_name = next((f for f in sorted(top) if f.lower() in _README_NAMES), None)
    if readme_name is None or deps.llm is None:
        reason = "the repo has no README" if readme_name is None else "there is no Groq key"
        return [], [], StepNote(""), f"README tips skipped: {reason}."
    readme_path = PathGuard(Path(state["repo_dir"])).check(readme_name)
    readme = readme_path.read_text(encoding="utf-8", errors="replace")[
        : deps.settings.agent.max_readme_chars
    ]
    stack = f"{candidate.stack}/{candidate.manager}"
    try:
        result = deps.llm.complete(
            readme_tips_messages(stack, planned, readme),
            choice="big_then_small",
            max_tokens=deps.settings.agent.plan_max_tokens,
            is_json=True,
            run_id=state["run_id"],
        )
    except SetupPilotError as error:
        return [], [], StepNote(""), f"README tips skipped: {error.message} {error.details}"
    note = StepNote("", tokens=result.input_tokens + result.output_tokens, model=result.model)
    try:
        data = json.loads(result.text)
        tips = [
            {"command": str(t["command"]), "reason": str(t.get("reason", ""))[:200]}
            for t in data.get("extra_commands", [])[:_MAX_TIPS]
        ]
        manual = [str(m)[:200] for m in data.get("manual_steps", [])[:_MAX_MANUAL_STEPS]]
    except (ValueError, AttributeError, TypeError, KeyError):
        return [], [], note, "README tips skipped: the AI's answer could not be read."
    return tips, manual, note, None


def _approve(state: RunState) -> tuple[dict, StepNote]:
    index = _pending_index(state)
    if index is None:
        raise RuntimeError(f"Run {state['run_id']} reached approve with no command waiting.")
    plan = [dict(p) for p in state["plan"]]
    item = plan[index]
    answer = interrupt(
        {
            "kind": "approve_command",
            "step": f"{index + 1} of {len(plan)}",
            "command": item["text"],
            "reason": item["reason"],
            "is_check": item["kind"] == "check",
            "warning": INSTALL_SCRIPT_WARNING,
            "in_docker": bool(state.get("use_docker")),
            "edit_error": state.get("edit_error"),
        }
    )
    action = answer.get("action")
    if action == "approve":
        update: dict = {"decision": "approve", "edit_error": None, "next_index": index}
        return update, StepNote(f"Approved: {item['text']}")
    if action == "edit":
        new_text = str(answer.get("command", ""))
        try:
            validate_command(new_text, PathGuard(Path(state["repo_dir"])))
        except CommandBlocked as error:
            return {"decision": "edit", "edit_error": error.details}, StepNote("Edit was blocked.")
        item |= {"text": new_text, "kind": "install"}
        # The edited command is shown again, so the user approves exactly what will run.
        update = {"plan": plan, "decision": "edit", "edit_error": None}
        return update, StepNote(f"Edited to: {new_text}")
    item["status"] = "rejected"
    update = {"plan": plan, "next_index": index + 1, "decision": "reject", "edit_error": None}
    return update, StepNote(f"Rejected: {item['text']}")


def _execute(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    index = state["next_index"]
    plan = [dict(p) for p in state["plan"]]
    item = plan[index]
    # Checked again right before running: nothing can slip in between approval and run.
    command = validate_command(item["text"], PathGuard(Path(state["repo_dir"])))
    if state.get("use_docker") and not _runs_on_host(state):
        command = in_docker(command, docker_image_for(state["chosen"]["manager"]))
    arguments = {"command": redact(item["text"])}
    try:
        output = deps.run(command)
    except SetupPilotError as error:
        item["status"] = "failed"
        update: dict = {"plan": plan, "next_index": index + 1, "error": _error(error)}
        return update, StepNote(f"{item['text']}: {error.message}", command.argv[0], arguments)
    item |= {
        "status": "done",
        "exit_code": output.exit_code,
        "output_tail": output.output_tail[-_STATE_OUTPUT_CHARS:],
    }
    update = {"plan": plan, "next_index": index + 1}
    if output.exit_code != 0 and item["kind"] != "check":
        update["error"] = _error(
            InstallFailed(
                f"`{arguments['command']}` failed with exit code {output.exit_code}.",
                details=output.output_tail[-_STATE_OUTPUT_CHARS:],
            )
        )
    # A negative exit code means a signal stopped it, which here is almost always Cancel.
    ran_text = f"Ran {item['text']}: exit {output.exit_code}."
    if output.exit_code < 0:
        ran_text = f"{item['text']} was stopped before it finished."
    note = StepNote(
        ran_text,
        command.argv[0],
        arguments,
        output.exit_code,
    )
    return update, note


def _verify(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    candidate = _candidate(state["chosen"])
    repo = Path(state["repo_dir"])
    guard = PathGuard(repo)

    ran: list[str] = []

    def run_quietly(text: str) -> CommandOutput:
        command = validate_command(text, guard)
        if state.get("use_docker") and not _runs_on_host(state):
            command = in_docker(command, docker_image_for(candidate.manager))
        ran.append(text)
        return deps.run(command)

    # README suggestions are optional; skipping SetupPilot's own steps is not.
    skipped = [
        p["text"] for p in state["plan"] if p["status"] == "rejected" and p["kind"] != "readme"
    ]
    # After a skip nothing else runs, not even read-only checks: the user said no.
    checks = [] if skipped else [asdict(c) for c in quiet_checks(candidate, repo, run_quietly)]
    for item in state["plan"]:
        if item["kind"] == "check" and item["status"] == "done":
            checks.append(
                {
                    "name": item["reason"],
                    "is_passed": item["exit_code"] == 0,
                    "detail": item.get("output_tail", "")[-500:],
                }
            )
    if skipped:
        checks.append(
            {
                "name": "You approved every step",
                "is_passed": False,
                "detail": "Rejected: " + "; ".join(skipped),
            }
        )
    if not checks:
        checks.append(
            {
                "name": "A check exists for this stack",
                "is_passed": False,
                "detail": "No automatic check yet.",
            }
        )
    failed = [c for c in checks if not c["is_passed"]]
    update: dict = {"checks": checks, "is_verified": not failed}
    if skipped:
        update["error"] = _error(
            VerifyFailed(
                "You rejected some steps, so the setup was not checked.",
                details=f"Rejected: {'; '.join(skipped)}. Start a new run and approve them to "
                "finish the setup.",
            )
        )
    elif failed:
        update["error"] = _error(
            VerifyFailed(
                "The install finished, but the setup could not be confirmed as working.",
                details="; ".join(f"{c['name']}: {c['detail'][:200]}" for c in failed),
            )
        )
    summary = f"{len(checks) - len(failed)} of {len(checks)} checks passed."
    return update, StepNote(summary, "checks" if ran else None, {"commands": ran})


def _report(deps: AgentDeps, state: RunState) -> dict:
    """Write the report and finish the run. This step never fails: the run must end clearly."""
    started = time.monotonic()
    error = state.get("error")
    # Cancel is saved by another thread. A command it kills comes back as a plain failure, and
    # verify leads straight here, so the saved status decides.
    is_cancelled = deps.runs.get_run(state["run_id"]).status == "cancelled"
    if is_cancelled:
        error = _error(_cancelled())
    is_verified = bool(state.get("is_verified")) and not error
    # Short facts only (no paths, timings or raw output), so the same run makes the same prompt.
    # README commands and readme_mentions are repo text; REPORT_SYSTEM marks all facts as data.
    facts = {
        "repo": state.get("repo_url"),
        "stack": _key(state["chosen"]) if state.get("chosen") else None,
        "in_docker": bool(state.get("use_docker")),
        "commands": [
            {"command": redact(p["text"]), "status": p["status"], "exit_code": p.get("exit_code")}
            for p in state.get("plan", [])
        ],
        "checks": [{"name": c["name"], "passed": c["is_passed"]} for c in state.get("checks", [])],
        "verified": is_verified,
        "problem": error["message"] if error else None,
        "readme_mentions": state.get("manual_steps", []),
        "notes": state.get("notes", []) + state.get("conflicts", []),
    }
    report, tokens, model = _plain_report(facts), 0, None
    if deps.llm is not None and not is_cancelled:
        try:
            result = deps.llm.complete(
                report_messages(facts),
                choice="big_then_small",
                max_tokens=deps.settings.agent.report_max_tokens,
                is_json=False,
                run_id=state["run_id"],
            )
            if result.text.strip():
                report = f"{_verdict(facts)}\n\n{result.text.strip()}\n\nDetails:\n{report}"
            tokens, model = result.input_tokens + result.output_tokens, result.model
        except SetupPilotError as llm_error:
            report += f"\n(AI summary skipped: {llm_error.message})"
    status: RunStatus = "cancelled" if is_cancelled else "succeeded" if is_verified else "failed"
    deps.runs.set_status(
        state["run_id"],
        status,
        error["error_type"] if error else None,
        error["message"] if error else None,
    )
    note = StepNote("Wrote the report.", "llm" if model else None, None, None, tokens, model)
    _save_step(deps, state["run_id"], "report", note, None, started)
    update = {"report": report, "is_verified": is_verified, "error": error}
    return update | {"steps": state.get("steps", 0) + 1}


def _verdict(facts: dict) -> str:
    if facts["verified"]:
        return "Setup verified: it works."
    return "Setup NOT verified." + (f" Problem: {facts['problem']}" if facts["problem"] else "")


_COMMAND_STATUS_WORDS = {
    "done": "ran",
    "rejected": "rejected by you",
    "failed": "could not run",
    "pending": "not run",
    "blocked": "blocked (not allowed)",
}


def _command_outcome(command: dict) -> str:
    words = _COMMAND_STATUS_WORDS[command["status"]]
    code = command["exit_code"]
    if code is None:
        return words
    return (
        f"{words}, but was stopped before it finished" if code < 0 else f"{words}, exit code {code}"
    )


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"


def _plain_report(facts: dict) -> str:
    """The report without the LLM. It states only facts, so it can always be trusted."""
    lines = [_verdict(facts)]
    for command in facts["commands"]:
        lines.append(f"- {command['command']}: {_command_outcome(command)}")
    for check in facts["checks"]:
        lines.append(f"- check '{check['name']}': {'passed' if check['passed'] else 'failed'}")
    if facts["readme_mentions"]:
        lines.append("The README also mentions (from the repo, not checked by SetupPilot):")
        lines += [f"  - {step}" for step in facts["readme_mentions"]]
    lines += [f"Note: {note}" for note in facts["notes"]]
    return "\n".join(lines)


def _pending_index(state: RunState) -> int | None:
    """The next command waiting for approval. Blocked suggestions are skipped: they are only
    listed in the report, never offered to run."""
    plan = state.get("plan", [])
    start = state.get("next_index", 0)
    return next((i for i in range(start, len(plan)) if plan[i]["status"] == "pending"), None)


def _next_pending(state: RunState) -> str:
    return "verify" if _pending_index(state) is None else "approve"


def _after_approve(state: RunState) -> str:
    if state.get("error"):
        return "report"
    if state.get("decision") == "approve":
        return "execute"
    if state.get("decision") == "edit":
        return "approve"
    return _next_pending(state)


def _runs_on_host(state: RunState) -> bool:
    """Compose stacks already run in containers, so Docker mode leaves their commands alone."""
    return state["chosen"]["manager"] == "compose"


def _item(text: str, reason: str, kind: str) -> dict:
    return {"text": text, "reason": reason, "kind": kind, "status": "pending"}


def _candidate(data: dict) -> StackCandidate:
    return StackCandidate(
        data["stack"], data["manager"], tuple(data["evidence"]), data["version_hint"]
    )


def _key(candidate: dict) -> str:
    return f"{candidate['stack']}/{candidate['manager']}"


def _cancelled() -> RunCancelled:
    return RunCancelled("You cancelled this run.", details="Nothing more was run.")


def _error(error: SetupPilotError) -> dict:
    return {"error_type": type(error).__name__, "message": error.message, "details": error.details}
