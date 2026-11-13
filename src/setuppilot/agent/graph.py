"""The setup flow. The AI reads the repo and explains failures; plain code checks every command
it proposes, runs only what the user approves, and decides whether the setup works."""

import hashlib
import json
import logging
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast, get_args

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from setuppilot.agent.prompts import analyze_messages, diagnose_messages, report_messages
from setuppilot.agent.recovery import decide_recovery
from setuppilot.analysis.evidence import build_evidence, evidence_text, local_reader
from setuppilot.analysis.merge import merge_plan, plan_item
from setuppilot.analysis.reconcile import (
    PlanOption,
    ai_project,
    folders_to_detect,
    ground_analysis,
    option_id,
    reconcile,
    runnable_steps,
    uses_own_plan,
)
from setuppilot.analysis.schema import (
    ANALYSIS_FORMAT,
    DIAGNOSIS_FORMAT,
    Diagnosis,
    Project,
    RepoAnalysis,
    ToolId,
)
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
from setuppilot.guardrails.commands import (
    INSTALL_SCRIPT_WARNING,
    ExecTarget,
    allowlist_digest,
    command_verb,
    recipe_command,
    validate_ai_command,
    validate_command,
)
from setuppilot.guardrails.docker_mode import RUNS_ON_HOST, docker_image_for, exec_target, in_docker
from setuppilot.guardrails.paths import PathGuard
from setuppilot.guardrails.runner import CommandOutput
from setuppilot.llm.client import LLMClient, LLMResult
from setuppilot.redact import redact
from setuppilot.setup.plans import (
    QUIET_COMMANDS,
    SUPPORTED_MANAGERS,
    PlannedCommand,
    code_checks,
    install_plan,
    is_installable_project,
    quiet_checks,
)
from setuppilot.tools.clone import CloneRequest, RepoAccess
from setuppilot.tools.compose import find_compose_files
from setuppilot.tools.detect import Detection, Stack, StackCandidate, detect_stacks
from setuppilot.tools.github_url import RepoRef, parse_github_url
from setuppilot.tools.host import OS_LABELS, add_to_path, host_os, refresh_path
from setuppilot.tools.prereqs import (
    ToolNeed,
    ToolProbe,
    check_tools,
    known_tool,
    merged_needs,
    needs_for,
    needs_for_command,
    recipe_for,
    usable_recipe,
)
from setuppilot.tools.scan import scan_repo

log = logging.getLogger(__name__)

# Only the end of command output is kept in the saved run state, to keep the database small.
_STATE_OUTPUT_CHARS = 2000
_OPTIONAL_KINDS = ("extra", "readme")  # "readme": an optional step in runs saved before v2
AI_ONLY_TEXT = "Checked only by the AI-proposed checks, not by SetupPilot's own"
_LICENSE_NOTE = (
    "winget accepts the package's license terms for you. Windows may ask for admin rights in "
    "another window."
)
Verification = Literal["setuppilot", "ai_only", "none"]


class RunState(TypedDict, total=False):
    run_id: str
    repo_url: str
    dest_dir: str
    use_docker: bool
    access: RepoAccess
    # The project folder: what to check out, then the sub-project that was picked.
    subdir: str | None
    repo_dir: str
    candidates: list[dict]
    conflicts: list[str]
    notes: list[str]
    analysis: dict | None
    options: list[dict]
    # Tool checks per option id.
    prereqs: dict[str, list[dict]]
    chosen: dict
    install_offer: str | None
    last_install: dict | None
    install_rounds: int
    # Tools a diagnosis says are missing; checked like the others from then on.
    extra_tools: list[str]
    plan: list[dict]
    manual_steps: list[str]
    start_command: str | None
    next_index: int
    decision: str
    edit_error: str | None
    failure: dict | None
    attempts: list[dict]
    fixes_used: int
    diagnoses: list[dict]
    user_action: dict | None
    checks: list[dict]
    verification: Verification
    is_verified: bool
    error: dict | None
    report: str
    steps: int


@dataclass(frozen=True)
class AgentDeps:
    settings: Settings
    runs: RunStore
    llm: LLMClient | None
    clone: Callable[[RepoRef, Path, CloneRequest], Path]
    probe: ToolProbe
    run: Callable[[Any], CommandOutput]
    # The system SetupPilot runs on. Docker mode may still run commands for "posix".
    host: ExecTarget


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
        "analyze": lambda s: _analyze(deps, s),
        "prerequisites": lambda s: _prerequisites(deps, s),
        "choose_stack": lambda s: _choose_stack(deps, s),
        "offer_install": lambda s: _offer_install(deps, s),
        "install_tool": lambda s: _install_tool(deps, s),
        "plan": lambda s: _plan(deps, s),
        "approve": lambda s: _approve(deps, s),
        "execute": lambda s: _execute(deps, s),
        "diagnose": lambda s: _diagnose(deps, s),
        "ask_user": _ask_user,
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
        ("detect", "analyze"),
        ("analyze", "prerequisites"),
        ("install_tool", "prerequisites"),
    ]:
        graph.add_conditional_edges(name, then(nxt))
    graph.add_conditional_edges("prerequisites", _after_prerequisites)
    graph.add_conditional_edges("choose_stack", _after_prerequisites)
    graph.add_conditional_edges("offer_install", _after_offer)
    graph.add_conditional_edges("plan", lambda s: "report" if s.get("error") else _next_pending(s))
    graph.add_conditional_edges("approve", _after_approve)
    graph.add_conditional_edges("execute", _after_execute)
    graph.add_conditional_edges("diagnose", _after_diagnose)
    graph.add_conditional_edges(
        "ask_user", lambda s: "report" if s.get("error") else _next_pending(s)
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
    # Runs saved before v2 have no access field, and were all public.
    request = CloneRequest(state.get("access", "public"), state.get("subdir"))
    target = deps.clone(ref, Path(state["dest_dir"]), request)
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
    _refuse_shipped_tools(scan.skipped_dirs)
    links = len(scan.skipped_symlinks)
    summary = _count(len(scan.files), "file") + (
        f"; {_count(links, 'link')} not followed" * bool(links)
    )
    summary += "."
    return {}, StepNote(summary)


def _refuse_shipped_tools(skipped_dirs: tuple[str, ...]) -> None:
    if skipped_dirs:
        raise CommandBlocked(
            "This repo ships its own " + " and ".join(skipped_dirs) + ", so it was not set up.",
            details="Tools would run programs from those folders. Delete them from the cloned "
            "folder yourself if you trust the repo, then start a new run.",
        )


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


def _analyze(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    """Ask the AI how to set the repo up, then check its answer against the files. Any
    failure here leaves the files-only options, as before the AI, and says why."""
    repo = Path(state["repo_dir"])
    scan = scan_repo(repo)
    analysis: RepoAnalysis | None = None
    note = StepNote("")
    if not deps.settings.agent.use_ai_planning:
        notes = ["The AI analysis is off (agent.use_ai_planning), so only the files were used."]
    elif deps.llm is None:
        notes = ["AI analysis skipped: there is no Groq key, so only the files were used."]
    else:
        analysis, notes, note = _ask_for_analysis(deps, deps.llm, state, scan)
    subdir = state.get("subdir")
    folders = folders_to_detect(analysis) + ([subdir] if subdir else [])
    root = Detection(tuple(_candidate(c) for c in state["candidates"]), (), ())
    detections = {".": root}
    for folder in dict.fromkeys(folders[1:]):
        path = PathGuard(repo).check(folder)
        if not path.is_dir():
            raise StackNotFound(
                f"The folder {folder} is not in this repo.",
                details="Give the folder's path from the repo's top, with / between names.",
            )
        detections[folder] = detect_stacks(scan_repo(path))
    options = reconcile(
        analysis,
        detections,
        lambda project, manager, command: _command_problem(deps, state, project, manager, command),
    )
    if subdir:
        options = [o for o in options if o.project == subdir]
    update = {
        "analysis": analysis.model_dump() if analysis else None,
        "options": [asdict(o) for o in options],
        "notes": list(state.get("notes", [])) + notes,
    }
    shown = [f"{o.id} ({_SOURCE_WORDS[o.source]})" for o in options if o.is_runnable]
    summary = ("Ways to set it up: " + ", ".join(shown) + ".") if shown else "No way found."
    arguments = {
        "summary": analysis.summary if analysis else None,
        "options": [
            {"id": o.id, "source": o.source, "is_runnable": o.is_runnable, "note": o.note}
            for o in options
        ],
        "notes": notes,
    }
    tool = "llm" if note.model else None
    return update, StepNote(summary, tool, arguments, None, note.tokens, note.model)


_SOURCE_WORDS = {"both": "the files and the AI agree", "files": "from the files", "ai": "the AI"}


def _ask_for_analysis(
    deps: AgentDeps, llm: LLMClient, state: RunState, scan: Any
) -> tuple[RepoAnalysis | None, list[str], StepNote]:
    repo = Path(state["repo_dir"])
    body, tag = evidence_text(
        build_evidence(scan, local_reader(repo), deps.settings.agent.max_evidence_chars)
    )
    ref = parse_github_url(state["repo_url"])
    target: ExecTarget = "posix" if state.get("use_docker") else deps.host
    messages = analyze_messages(
        _os_label(deps, state), f"{ref.owner}/{ref.name}", allowlist_digest(target), body, tag
    )
    try:
        result = llm.complete(
            messages,
            choice="big_then_small",
            max_tokens=deps.settings.agent.analyze_max_tokens,
            response_format=ANALYSIS_FORMAT,
            run_id=state["run_id"],
            is_cacheable=_is_cacheable(state),
        )
    except SetupPilotError as error:
        return None, [f"AI analysis skipped: {error.message}"], StepNote("")
    note = StepNote("", tokens=result.input_tokens + result.output_tokens, model=result.model)
    analysis = _parsed(RepoAnalysis, result)
    if analysis is None:
        return (
            None,
            ["AI analysis skipped: its answer was cut off or not in the right shape."],
            note,
        )
    grounded, dropped = ground_analysis(analysis, scan)
    return grounded, dropped + [f"From the AI: {n}" for n in grounded.notes], note


def _parsed[Answer: BaseModel](model: type[Answer], result: LLMResult) -> Answer | None:
    """None when the answer was cut off or does not fit the schema."""
    if result.is_truncated:
        return None
    try:
        return model.model_validate_json(result.text)
    except ValidationError:
        return None


def _command_problem(
    deps: AgentDeps, state: RunState, project: str, manager: str, command: str
) -> str | None:
    target = exec_target(deps.host, bool(state.get("use_docker")), manager)
    try:
        guard = _folder_guard(Path(state["repo_dir"]), project)
    except PathBlocked as error:
        return error.details
    return _ai_problem(command, guard, target)


def _ai_problem(command: str, guard: PathGuard, target: ExecTarget) -> str | None:
    """Why a command the AI wrote may not run here, or None when it may."""
    try:
        validate_ai_command(command, guard, target)
    except CommandBlocked as error:
        return error.details
    return None


def _prerequisites(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    # A tool installed since the last check, by SetupPilot or by hand, may be on Windows' saved
    # PATH only, and "Check again" must find it.
    refresh_path()
    # Once a way is picked, only its tools matter.
    options = [_option(state)] if state.get("chosen") else _runnable(state)
    prereqs = dict(state.get("prereqs", {}))
    for option in options:
        needs = _needs(deps, state, option)
        prereqs[option.id] = [asdict(check) for check in check_tools(needs, deps.probe)]
    missing = sorted({c["tool"] for o in options for c in prereqs[o.id] if c["is_blocking"]})
    summary = (
        f"Missing tools: {', '.join(missing)}." if missing else "All needed tools are installed."
    )
    # Saved in the step too, so the run page can still show each way's tools after a refresh.
    tools = {o.id: prereqs[o.id] for o in options}
    return {"prereqs": prereqs}, StepNote(summary, None, {"tools": tools})


def _needs(deps: AgentDeps, state: RunState, option: PlanOption) -> list[ToolNeed]:
    if state.get("use_docker") and option.manager not in RUNS_ON_HOST:
        # Everything runs inside the image, so only Docker itself is needed here.
        return needs_for(StackCandidate("docker", "dockerfile", (), None), "posix")
    target = _target_of(deps, state, option)
    files = needs_for(_candidate(option), target)
    project = ai_project(_analysis(state), option)
    ai = _ai_needs(project) if project else []
    if project and not uses_own_plan(option):
        # No hand-written plan says what runs, so the AI's own commands do.
        steps = runnable_steps(project.steps)
        ai += [need for s in steps for need in needs_for_command(s.command, target)]
    reason = "the AI's diagnosis says it is missing"
    ai += [ToolNeed(t, True, None, reason) for t in state.get("extra_tools", [])]
    return merged_needs(files, ai)


def _ai_needs(project: Project) -> list[ToolNeed]:
    """The AI's tool list, in SetupPilot's names. A Python version is left to uv."""
    needs = []
    if project.runtime and (tool := known_tool(project.runtime.tool)):
        constraint = None if tool == "uv" else _short(project.runtime.constraint)
        reason = f"the AI read it in {project.runtime.source[:80]}"
        needs.append(ToolNeed(tool, True, constraint, reason))
    for need in project.tools:
        if tool := known_tool(need.tool):
            constraint = None if tool == "uv" else _short(need.version)
            needs.append(ToolNeed(tool, need.is_required, constraint, need.reason[:120]))
    return needs


def _short(constraint: str | None) -> str | None:
    return constraint if constraint and len(constraint) <= 40 else None


def _after_prerequisites(state: RunState) -> str:
    if state.get("error"):
        return "report"
    if not state.get("chosen"):
        return "choose_stack"
    if _missing_required(state):
        return "offer_install"
    return _next_pending(state) if state.get("plan") else "plan"


def _choose_stack(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    options = _options(state)
    runnable = [o for o in options if o.is_runnable]
    if not runnable:
        found = "; ".join(f"{o.id}: {o.note}" if o.note else o.id for o in options)
        raise StackNotFound(
            "SetupPilot found nothing it can set up in this repo.",
            details=f"Found: {found or 'no setup files at all'}. "
            + " ".join(state.get("notes", [])),
        )
    if len(runnable) == 1:
        chosen = runnable[0]
    else:
        answer = interrupt(
            {
                "kind": "choose_stack",
                "options": [
                    {
                        "label": o.id,
                        "evidence": list(o.evidence),
                        "prereqs": state["prereqs"][o.id],
                        "project": o.project,
                        "source": o.source,
                        "note": o.note,
                    }
                    for o in runnable
                ],
                "conflicts": state.get("conflicts", []),
            }
        )
        index = answer.get("choice")
        if not isinstance(index, int) or not 0 <= index < len(runnable):
            raise StackNotFound("That choice is not one of the options.", details=f"Got {index!r}.")
        chosen = runnable[index]
    notes = list(state.get("notes", []))
    notes += [f"Not offered: {o.id}. {o.note or ''}".strip() for o in options if not o.is_runnable]
    if state.get("use_docker") and chosen.manager not in RUNS_ON_HOST:
        docker_image_for(chosen.manager)
        if chosen.project != ".":
            # Docker mode mounts the repo's .git, which a sub-folder does not have.
            raise CommandBlocked(
                "Docker mode cannot set up a project in a sub-folder yet.",
                details=f"{chosen.id} is in {chosen.project}. Turn Docker mode off to set it up "
                "on your machine.",
            )
    checks = state["prereqs"][chosen.id]
    notes += [c["problem"] for c in checks if c["problem"] and not _is_blocking(c)]
    update = {
        "chosen": asdict(chosen),
        "notes": notes,
        "subdir": None if chosen.project == "." else chosen.project,
    }
    return update, StepNote(f"Chose {chosen.id}.")


def _offer_install(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    """Ask about one missing tool at a time, so each answer is about one command."""
    blocking = [c for c in state["prereqs"][_option(state).id] if _is_blocking(c)]
    rounds = state.get("install_rounds", 0)
    limit = deps.settings.agent.max_install_rounds
    if rounds >= limit:
        raise _tools_missing(
            blocking, f"SetupPilot asked {rounds} times (agent.max_install_rounds)."
        )
    check = blocking[0]
    os_name = host_os(deps.host)
    recipe = usable_recipe(check["tool"], os_name, deps.probe)
    command = None
    if recipe.command:
        command = recipe_command(*recipe.command, Path(tempfile.gettempdir())).text
    last = state.get("last_install")
    answer = interrupt(
        {
            "kind": "install_tool",
            "tool": check["tool"],
            # Tool checks saved before v2 have no reason.
            "reason": check.get("reason", ""),
            "problem": check["problem"],
            "command": command,
            "needs_admin": recipe.needs_admin,
            "manual": recipe.manual,
            "note": _LICENSE_NOTE if recipe.command and recipe.command[0] == "winget" else None,
            "os": OS_LABELS[os_name],
            "last_install": last if last and last["tool"] == check["tool"] else None,
            "round": f"{rounds + 1} of {limit}",
        }
    )
    action = answer.get("action")
    if action == "stop":
        raise _tools_missing(blocking, "You chose to stop.")
    update = {
        "install_rounds": rounds + 1,
        "install_offer": check["tool"] if action == "install" else None,
    }
    words = "Install" if action == "install" else "Check again"
    return update, StepNote(f"{words}: {check['tool']}.")


def _after_offer(state: RunState) -> str:
    if state.get("error"):
        return "report"
    return "install_tool" if state.get("install_offer") else "prerequisites"


def _tools_missing(blocking: list[dict], why: str) -> PrereqMissing:
    return PrereqMissing(
        "Some tools this repo needs are not installed: "
        + ", ".join(c["tool"] for c in blocking)
        + ".",
        details="\n".join(f"{c['tool']}: {c['problem']}" for c in blocking) + f"\n{why}",
    )


def _install_tool(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    """The command comes from SetupPilot's own table; the state only names the tool."""
    tool = state["install_offer"]
    if tool is None:
        raise RuntimeError(f"Run {state['run_id']} reached install_tool with no tool to install.")
    recipe = recipe_for(tool, host_os(deps.host))
    if recipe.command is None:
        raise RuntimeError(f"There is no install command for {tool}, so it was not offered.")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        command = recipe_command(*recipe.command, Path(folder))
        try:
            output = deps.run(command)
        except SetupPilotError as error:
            # Not the end of the run: the user can install it another way and check again.
            failed: dict[str, Any] = {"tool": tool, "command": command.text, "exit_code": None}
            failed["output"] = error.message
            update = {"install_offer": None, "last_install": failed}
            return update, StepNote(f"{command.text}: {error.message}", command.argv[0])
    if recipe.command[0] == "uv_tool":
        # uv puts the tool in its own folder, which is often not on PATH yet.
        add_to_path(deps.probe.run(("uv", "tool", "dir", "--bin")).output)
    tail = output.output_tail[-500:]
    last = {"tool": tool, "command": command.text, "exit_code": output.exit_code, "output": tail}
    note = StepNote(
        f"Ran {command.text}: exit {output.exit_code}.",
        command.argv[0],
        {"command": command.text},
        output.exit_code,
    )
    return {"install_offer": None, "last_install": last}, note


def _plan(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    option = _option(state)
    guard = _guard(state)
    scan = scan_repo(guard.workspace)
    if option.project != ".":
        # The root's scan only looked for these at the top.
        _refuse_shipped_tools(scan.skipped_dirs)
    target = _target(deps, state)
    installs: list[PlannedCommand] = []
    checks: list[PlannedCommand] = []
    quiet: tuple[str, ...] = ()
    if uses_own_plan(option):
        candidate = _candidate(option)
        top = frozenset(f for f in scan.files if "/" not in f)
        installs = install_plan(candidate, top, is_installable_project(guard.workspace), target)
        checks = code_checks(candidate, scan.files, target)
        quiet = (QUIET_COMMANDS[option.manager],) if option.manager in QUIET_COMMANDS else ()
    project = ai_project(_analysis(state), option)
    merged = merge_plan(
        installs,
        checks,
        quiet,
        project,
        lambda text: _ai_problem(text, guard, target),
        lambda text: command_verb(text, target),
    )
    plan = merged.items
    if option.manager == "compose" and uses_own_plan(option):
        (compose,) = find_compose_files(guard.workspace, (option.evidence[0],))
        if compose.warnings:
            plan[0]["reason"] += " Careful: " + " ".join(compose.warnings)
    update = {
        "plan": plan,
        "manual_steps": merged.manual_steps,
        "start_command": project.start_command if project else None,
        "notes": list(state.get("notes", [])) + merged.notes,
        "next_index": 0,
    }
    pending = sum(p["status"] == "pending" for p in plan)
    blocked = sum(p["status"] == "blocked" for p in plan)
    manual = len(merged.manual_steps)
    summary = f"{_count(pending, 'command')} to approve"
    summary += f"; {_count(blocked, 'AI command')} not allowed" * bool(blocked)
    summary += f"; the AI lists {_count(manual, 'manual step')}." if manual else "."
    return update, StepNote(summary, None, {"start_command": update["start_command"]})


def _approve(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    index = _pending_index(state)
    if index is None:
        raise RuntimeError(f"Run {state['run_id']} reached approve with no command waiting.")
    plan = [dict(p) for p in state["plan"]]
    item = plan[index]
    if not all(_succeeded(plan, i) for i in item.get("needs_ok", [])):
        item["status"] = "skipped"
        error = InstallFailed(
            f"`{redact(item['text'])}` was not tried again, because its fix did not run.",
            details=plan[item["retry_of"]].get("output_tail", ""),
        )
        return {"plan": plan, "error": _error(error)}, StepNote(error.message)
    answer = interrupt(
        {
            "kind": "approve_command",
            "step": f"{index + 1} of {len(plan)}",
            "command": item["text"],
            "reason": item["reason"],
            "is_check": item["kind"] == "check",
            "is_optional": _is_optional(item),
            "origin": item.get("origin", "setuppilot"),
            # Only on fixes: the AI's guess at what went wrong.
            "diagnosis": item.get("diagnosis"),
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
            validate_command(new_text, _guard(state), _target(deps, state))
        except CommandBlocked as error:
            return {"decision": "edit", "edit_error": error.details}, StepNote("Edit was blocked.")
        # Your own command: no longer a check or optional, but a fix stays a fix.
        kind = "fix" if item["kind"] == "fix" else "install"
        item |= {"text": new_text, "kind": kind, "origin": "you"}
        # The edited command is shown again, so the user approves exactly what will run.
        update = {"plan": plan, "decision": "edit", "edit_error": None}
        return update, StepNote(f"Edited to: {new_text}")
    item["status"] = "rejected"
    update = {"plan": plan, "next_index": index + 1, "decision": "reject", "edit_error": None}
    return update, StepNote(f"Rejected: {item['text']}")


def _execute(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    if _missing_required(state):
        # Routing never gets here then; this keeps it so if routing changes.
        raise PrereqMissing(
            "A tool this repo needs is missing, so the command was not run.",
            details="Install the tools listed in the tool check, then start a new run.",
        )
    index = state["next_index"]
    plan = [dict(p) for p in state["plan"]]
    item = plan[index]
    # Checked again right before running: nothing can slip in between approval and run.
    command = validate_command(item["text"], _guard(state), _target(deps, state))
    if state.get("use_docker") and not _runs_on_host(state):
        command = in_docker(command, docker_image_for(state["chosen"]["manager"]))
    arguments = {"command": redact(item["text"])}
    try:
        output = deps.run(command)
    except SetupPilotError as error:
        item["status"] = "failed"
        update: dict = {"plan": plan, "next_index": index + 1, "error": _error(error)}
        return update, StepNote(f"{item['text']}: {error.message}", command.argv[0], arguments)
    tail = output.output_tail[-_STATE_OUTPUT_CHARS:]
    item |= {
        "status": "done",
        "exit_code": output.exit_code,
        "is_stopped": output.is_stopped,
        "output_tail": tail,
    }
    update = {"plan": plan, "next_index": index + 1}
    # A failed optional step is only reported; the checks show whether the setup still works.
    if output.exit_code != 0 and item["kind"] not in ("check", *_OPTIONAL_KINDS):
        update["attempts"] = list(state.get("attempts", [])) + [
            {"text": item["text"], "exit_code": output.exit_code}
        ]
        failed = f"`{arguments['command']}` failed with exit code {output.exit_code}."
        if item["kind"] == "fix":
            fix_failed = InstallFailed(f"The AI's fix did not work: {failed}", details=tail)
            update["error"] = _error(fix_failed)
        elif (why_not := _why_not_diagnosed(deps, state, plan, index, output)) is None:
            update["failure"] = {"index": index, "exit_code": output.exit_code, "output_tail": tail}
        else:
            update["error"] = _error(InstallFailed(failed, details=f"{tail}{why_not}"))
    ran_text = f"Ran {item['text']}: exit {output.exit_code}."
    if output.is_stopped:
        ran_text = f"{item['text']} was stopped before it finished."
    note = StepNote(
        ran_text,
        command.argv[0],
        arguments,
        output.exit_code,
    )
    return update, note


def _why_not_diagnosed(
    deps: AgentDeps, state: RunState, plan: list[dict], index: int, output: CommandOutput
) -> str | None:
    """None when the AI may look at this failure; else a note for the details ("" for none)."""
    agent = deps.settings.agent
    if not agent.use_ai_planning or deps.llm is None or output.is_stopped:
        return ""
    rounds = plan[_root(plan, index)].get("fix_rounds", 0)
    if rounds >= agent.max_fixes_per_step:
        return (
            f"\n\nSetupPilot already tried {_count(rounds, 'fix')} for this step "
            f"(agent.max_fixes_per_step = {agent.max_fixes_per_step})."
        )
    used = state.get("fixes_used", 0)
    if used >= agent.max_fixes_per_run:
        return (
            f"\n\nSetupPilot already tried {_count(used, 'fix')} in this run "
            f"(agent.max_fixes_per_run = {agent.max_fixes_per_run})."
        )
    return None


def _after_execute(state: RunState) -> str:
    if state.get("error"):
        return "report"
    return "diagnose" if state.get("failure") else _next_pending(state)


def _diagnose(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    """One AI call about one failure. What it proposes is checked here, then asked about."""
    failure = state["failure"]
    if failure is None or deps.llm is None:
        raise RuntimeError(f"Run {state['run_id']} reached diagnose with nothing to diagnose.")
    plan = [dict(p) for p in state["plan"]]
    index = failure["index"]
    item = plan[index]
    option = _option(state)
    guard, target = _guard(state), _target(deps, state)
    # A token you typed into an edit must not reach Groq, nor the shared LLM cache with it. The
    # AI only sees the hidden form, so a fix that repeats that form is a repeat too.
    tried = [redact(a["text"]) for a in state.get("attempts", [])]
    command = redact(item["text"])
    failed = f"`{command}` failed with exit code {failure['exit_code']}."
    checks = state.get("prereqs", {}).get(option.id, [])
    facts = {
        "os": _os_label(deps, state),
        "project": option.project,
        "package_manager": option.manager,
        "command": command,
        "exit_code": failure["exit_code"],
        "tried": tried,
        "installed_tools": {c["tool"]: c["version"] for c in checks if c["is_installed"]},
    }
    # The runner hid secrets already; this is the second pass the research asks for.
    output = redact(failure["output_tail"])
    tag = hashlib.sha256(output.encode()).hexdigest()[:16]
    tools = ", ".join(t for t in get_args(ToolId) if t != "other")
    messages = diagnose_messages(facts, allowlist_digest(target), tools, output, tag)
    try:
        result = deps.llm.complete(
            messages,
            choice="big_then_small",
            max_tokens=deps.settings.agent.diagnose_max_tokens,
            response_format=DIAGNOSIS_FORMAT,
            run_id=state["run_id"],
            is_cacheable=_is_cacheable(state),
        )
    except SetupPilotError as error:
        details = f"{failure['output_tail']}\n\nAI diagnosis skipped: {error.message}"
        return {"failure": None, "error": _error(InstallFailed(failed, details))}, StepNote(
            error.message
        )
    tokens = result.input_tokens + result.output_tokens
    diagnosis = _parsed(Diagnosis, result)
    if diagnosis is None:
        unread = "The AI's diagnosis was cut off or not in the right shape."
        unknown = _error(InstallFailed(failed, f"{failure['output_tail']}\n\n{unread}"))
        return {"failure": None, "error": unknown}, StepNote(
            unread, "llm", None, None, tokens, result.model
        )
    decision = decide_recovery(diagnosis, tried, lambda text: _ai_problem(text, guard, target))
    # The AI may be wrong about what is missing, so the probe has the last word.
    claimed = [ToolNeed(t, True, None, "") for t in decision.missing_tools]
    still_missing = [c.tool for c in check_tools(claimed, deps.probe) if not c.is_installed]
    root = _root(plan, index)
    plan[root]["fix_rounds"] = plan[root].get("fix_rounds", 0) + 1
    guess = {"category": diagnosis.category, "explanation": diagnosis.explanation[:300]}
    record: dict[str, Any] = {
        "command": command,
        "exit_code": failure["exit_code"],
        **guess,
        "proposed": [redact(f.command) for f in diagnosis.commands],
        "refused": [{"command": redact(c), "reason": redact(r)} for c, r in decision.refused],
        "missing_tools": still_missing,
    }
    update: dict = {
        "failure": None,
        "fixes_used": state.get("fixes_used", 0) + 1,
        "diagnoses": list(state.get("diagnoses", [])) + [record],
    }
    reason = f"Try it again: {plan[root]['reason']}"
    retry = plan_item(item["text"], reason, item["kind"], item.get("origin", "setuppilot"))
    retry |= {"retry_of": index, "needs_ok": []}
    after = index + 1
    if still_missing:
        extra = list(dict.fromkeys(state.get("extra_tools", []) + still_missing))
        update["extra_tools"] = extra
        update["prereqs"] = dict(state.get("prereqs", {})) | {
            option.id: [
                asdict(c)
                for c in check_tools(
                    _needs(deps, state | {"extra_tools": extra}, option), deps.probe
                )
            ]
        }
        plan.insert(after, retry)
    elif decision.fixes:
        fixes = [
            plan_item(text, f"A fix from the AI: {why}", "fix", "fix") | {"diagnosis": guess}
            for text, why in decision.fixes
        ]
        retry["needs_ok"] = list(range(after, after + len(fixes)))
        plan[after:after] = [*fixes, retry]
    elif decision.user_action or decision.refused:
        plan.insert(after, retry)
        update["user_action"] = {
            "command": command,
            "exit_code": failure["exit_code"],
            "diagnosis": guess,
            "user_action": decision.user_action,
            "refused": record["refused"],
        }
    else:
        details = (
            f"{failure['output_tail']}\n\nThe AI's guess: {guess['explanation']} It gave no fix."
        )
        update["error"] = _error(InstallFailed(failed, details))
    update["plan"] = plan
    summary = f"AI diagnosis: {diagnosis.category}: {diagnosis.explanation[:150]}"
    keys = ("command", "exit_code", "category", "proposed", "refused", "missing_tools")
    arguments = {k: record[k] for k in keys}
    return update, StepNote(summary, "llm", arguments, None, tokens, result.model)


def _after_diagnose(state: RunState) -> str:
    if state.get("error"):
        return "report"
    if _missing_required(state):
        return "offer_install"
    return "ask_user" if state.get("user_action") else _next_pending(state)


def _ask_user(state: RunState) -> tuple[dict, StepNote]:
    action = state["user_action"]
    if action is None:
        raise RuntimeError(f"Run {state['run_id']} reached ask_user with nothing to ask.")
    answer = interrupt({"kind": "fix_by_hand", **action})
    if answer.get("action") == "stop":
        raise InstallFailed(
            f"`{redact(action['command'])}` failed, and you stopped to fix it by hand.",
            details=action["user_action"] or "",
        )
    return {"user_action": None}, StepNote("You fixed it by hand; it will be tried again.")


def _verify(deps: AgentDeps, state: RunState) -> tuple[dict, StepNote]:
    option = _option(state)
    guard = _guard(state)
    target = _target(deps, state)

    ran: list[str] = []

    def run_quietly(text: str) -> CommandOutput:
        command = validate_command(text, guard, target)
        if state.get("use_docker") and not _runs_on_host(state):
            command = in_docker(command, docker_image_for(option.manager))
        ran.append(text)
        return deps.run(command)

    skipped = [
        p["text"] for p in state["plan"] if p["status"] == "rejected" and not _is_optional(p)
    ]
    # After a skip nothing else runs, not even read-only checks: the user said no. SetupPilot's
    # own checks go with its own plan; an option only the AI knows is checked by the AI's.
    own = []
    if not skipped and uses_own_plan(option):
        own = quiet_checks(_candidate(option), guard.workspace, run_quietly, target)
    checks = [asdict(c) | {"origin": "setuppilot"} for c in own]
    for item in state["plan"]:
        if item["kind"] == "check" and item["status"] == "done":
            checks.append(
                {
                    "name": item["reason"],
                    "is_passed": item["exit_code"] == 0,
                    "detail": item.get("output_tail", "")[-500:],
                    "origin": item.get("origin", "setuppilot"),
                }
            )
    if skipped:
        checks.append(
            {
                "name": _ALL_APPROVED,
                "is_passed": False,
                "detail": "Rejected: " + "; ".join(skipped),
                "origin": "setuppilot",
            }
        )
    if not checks:
        checks.append(
            {
                "name": "A check exists for this stack",
                "is_passed": False,
                "detail": "No automatic check yet.",
                "origin": "setuppilot",
            }
        )
    failed = [c for c in checks if not c["is_passed"]]
    is_own = any(c["origin"] == "setuppilot" for c in checks)
    verification: Verification = "none" if failed else "setuppilot" if is_own else "ai_only"
    update: dict = {
        "checks": checks,
        "is_verified": verification == "setuppilot",
        "verification": verification,
    }
    if skipped:
        ran_check = any(i["kind"] == "check" and i["status"] == "done" for i in state["plan"])
        details = f"Rejected: {'; '.join(skipped)}. Start a new run and approve them to finish."
        details += "".join(
            f" {c['name']}: {c['detail'][:200]}" for c in failed if c["name"] != _ALL_APPROVED
        )
        update["error"] = _error(
            VerifyFailed(
                "You rejected some steps, so the setup "
                + ("could not be confirmed." if ran_check else "was not checked."),
                details=details,
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
    if verification == "ai_only":
        summary += f" {AI_ONLY_TEXT}."
    return update, StepNote(summary, "checks" if ran else None, {"commands": ran})


_ALL_APPROVED = "You approved every step"


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
    verification = state.get("verification", "none") if not error else "none"
    # Short facts only (no paths, timings or raw output), so the same run makes the same prompt.
    # Commands, notes and the AI's steps hold repo text; REPORT_SYSTEM marks all facts as data.
    facts = {
        "repo": state.get("repo_url"),
        "stack": _option(state).id if state.get("chosen") else None,
        "in_docker": bool(state.get("use_docker")),
        "commands": [
            {
                "command": redact(p["text"]),
                "status": p["status"],
                "exit_code": p.get("exit_code"),
                # Only a command that ran has this.
                "is_stopped": bool(p.get("is_stopped")),
                "origin": p.get("origin", "setuppilot"),
            }
            for p in state.get("plan", [])
        ],
        "checks": [
            {"name": c["name"], "passed": c["is_passed"], "origin": c.get("origin", "setuppilot")}
            for c in state.get("checks", [])
        ],
        "verified": is_verified,
        "verification": verification,
        "problem": error["message"] if error else None,
        "manual_steps": state.get("manual_steps", []),
        "start_command": state.get("start_command"),
        "diagnoses": [
            {"category": d["category"], "explanation": d["explanation"]}
            for d in state.get("diagnoses", [])
        ],
        "notes": state.get("notes", []) + state.get("conflicts", []),
    }
    report, tokens, model = _plain_report(facts), 0, None
    if deps.llm is not None and not is_cancelled:
        try:
            # The small model, so the big one's minute limit is left for analysis and fixes.
            result = deps.llm.complete(
                report_messages(facts),
                choice="small",
                max_tokens=deps.settings.agent.report_max_tokens,
                response_format="text",
                run_id=state["run_id"],
                is_cacheable=_is_cacheable(state),
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
    # A Cancel during the LLM call above wins (the store keeps it), so the report must agree.
    if status != "cancelled" and deps.runs.get_run(state["run_id"]).status == "cancelled":
        error, is_verified, verification = _error(_cancelled()), False, "none"
        report = _plain_report(
            facts | {"verified": False, "verification": "none", "problem": error["message"]}
        )
    note = StepNote("Wrote the report.", "llm" if model else None, None, None, tokens, model)
    _save_step(deps, state["run_id"], "report", note, None, started)
    update = {
        "report": report,
        "is_verified": is_verified,
        "verification": verification,
        "error": error,
    }
    return update | {"steps": state.get("steps", 0) + 1}


def _verdict(facts: dict) -> str:
    if facts["verified"]:
        return "Setup verified: it works."
    if facts["verification"] == "ai_only":
        return f"Setup NOT verified by SetupPilot. {AI_ONLY_TEXT}, and they all passed."
    return "Setup NOT verified." + (f" Problem: {facts['problem']}" if facts["problem"] else "")


_COMMAND_STATUS_WORDS = {
    "done": "ran",
    "rejected": "rejected by you",
    "failed": "could not run",
    "pending": "not run",
    "blocked": "blocked (not allowed)",
    "skipped": "not tried again",
}
_ORIGIN_WORDS = {"ai": " (from the AI)", "fix": " (a fix the AI suggested)", "you": " (your edit)"}


def _command_outcome(command: dict) -> str:
    words = _COMMAND_STATUS_WORDS[command["status"]]
    code = command["exit_code"]
    if code is None:
        return words
    if command["is_stopped"]:
        return f"{words}, but was stopped before it finished"
    return f"{words}, exit code {code}"


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 'es' if word.endswith('x') else 's'}"


def _plain_report(facts: dict) -> str:
    """The report without the LLM. It states only facts, so it can always be trusted."""
    lines = [_verdict(facts)]
    for command in facts["commands"]:
        origin = _ORIGIN_WORDS.get(command["origin"], "")
        lines.append(f"- {command['command']}{origin}: {_command_outcome(command)}")
    for check in facts["checks"]:
        origin = " (AI-proposed)" if check["origin"] == "ai" else ""
        lines.append(
            f"- check '{check['name']}'{origin}: {'passed' if check['passed'] else 'failed'}"
        )
    if facts["diagnoses"]:
        lines.append("What the AI thought went wrong (a guess, it may be wrong):")
        lines += [f"  - {d['category']}: {d['explanation']}" for d in facts["diagnoses"]]
    if facts["manual_steps"]:
        lines.append("From the AI, not checked by SetupPilot (read them before doing any):")
        lines += [f"  - {step}" for step in facts["manual_steps"]]
    if facts["start_command"]:
        lines.append(
            f"To start it (from the AI; SetupPilot did not run it): {facts['start_command']}"
        )
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


def _succeeded(plan: list[dict], index: int) -> bool:
    return plan[index]["status"] == "done" and plan[index].get("exit_code") == 0


def _root(plan: list[dict], index: int) -> int:
    """The step a re-run belongs to, so fixes are counted per step."""
    while "retry_of" in plan[index]:
        index = plan[index]["retry_of"]
    return index


def _missing_required(state: RunState) -> bool:
    if not state.get("chosen"):
        return False
    return any(_is_blocking(c) for c in state.get("prereqs", {}).get(_option(state).id, []))


def _is_optional(item: dict) -> bool:
    """A step the user may reject and still get a verdict. The AI's checks only add to
    SetupPilot's own, so rejecting one leaves SetupPilot's checks to decide."""
    return item["kind"] in _OPTIONAL_KINDS or (
        item["kind"] == "check" and item.get("origin") == "ai"
    )


def _is_blocking(check: dict) -> bool:
    # Runs saved before v2 have no is_blocking: then any missing tool blocked.
    return check.get("is_blocking", not check["is_installed"])


def _runs_on_host(state: RunState) -> bool:
    """Compose stacks already run in containers, so Docker mode leaves their commands alone."""
    return state["chosen"]["manager"] in RUNS_ON_HOST


def _target(deps: AgentDeps, state: RunState) -> ExecTarget:
    return exec_target(deps.host, bool(state.get("use_docker")), state["chosen"]["manager"])


def _target_of(deps: AgentDeps, state: RunState, option: PlanOption) -> ExecTarget:
    return exec_target(deps.host, bool(state.get("use_docker")), option.manager)


def _guard(state: RunState) -> PathGuard:
    """The guard for the project's folder: the repo, or the sub-project that was picked."""
    return _folder_guard(Path(state["repo_dir"]), _option(state).project)


def _folder_guard(repo: Path, project: str) -> PathGuard:
    # The folder's links are resolved inside the repo's guard first, so it cannot point out.
    return PathGuard(PathGuard(repo).check(project))


def _os_label(deps: AgentDeps, state: RunState) -> str:
    if state.get("use_docker"):
        return "Linux, inside a Docker container"
    return OS_LABELS[host_os(deps.host)]


def _is_cacheable(state: RunState) -> bool:
    """The LLM cache is shared and kept, so a private repo's text never goes in it."""
    return state.get("access", "public") == "public"


def _analysis(state: RunState) -> RepoAnalysis | None:
    data = state.get("analysis")
    return RepoAnalysis.model_validate(data) if data else None


def _options(state: RunState) -> list[PlanOption]:
    if "options" in state:
        return [_as_option(o) for o in state["options"]]
    # A run saved before v2 was paused before its options existed.
    return [_as_option(c) for c in state["candidates"]]


def _runnable(state: RunState) -> list[PlanOption]:
    return [o for o in _options(state) if o.is_runnable]


def _option(state: RunState) -> PlanOption:
    return _as_option(state["chosen"])


def _as_option(data: dict) -> PlanOption:
    if "id" not in data:
        # A run saved before v2 holds a detector candidate: a root option from the files. v1
        # offered only the managers with a plan, and its detector found no newer ones.
        project, stack, manager = ".", data["stack"], data["manager"]
        return PlanOption(
            option_id(project, stack, manager), project, stack, manager, "files",
            tuple(data["evidence"]), data["version_hint"], manager in SUPPORTED_MANAGERS, None,
        )  # fmt: skip
    return PlanOption(**data | {"evidence": tuple(data["evidence"])})


def _candidate(data: dict | PlanOption) -> StackCandidate:
    if isinstance(data, dict):
        data = _as_option(data)
    # Only options from the files reach the hand-written plans, and their stack is the detector's.
    stack = cast(Stack, data.stack)
    return StackCandidate(stack, data.manager, data.evidence, data.version_hint)


def _cancelled() -> RunCancelled:
    return RunCancelled("You cancelled this run.", details="Nothing more was run.")


def _error(error: SetupPilotError) -> dict:
    return {"error_type": type(error).__name__, "message": error.message, "details": error.details}
