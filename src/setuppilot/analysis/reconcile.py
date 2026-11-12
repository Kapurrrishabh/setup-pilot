"""Check the AI's reading against the real files, and against the rules-based detector."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from setuppilot.analysis.schema import Project, RepoAnalysis, Step
from setuppilot.setup.plans import SUPPORTED_MANAGERS
from setuppilot.tools.detect import LOCK_FILES, STACK_FOR_MANAGER, Detection, StackCandidate
from setuppilot.tools.prereqs import known_tool
from setuppilot.tools.scan import RepoScan

# PROVISIONAL caps: Groq's strict mode has no maxItems or maxLength, so they are kept here.
MAX_PROJECTS, MAX_STEPS, MAX_NOTES, MAX_TEXT = 5, 8, 5, 200
_MAX_ITEMS, _MAX_COMMAND = 8, 300
_BAD_PATH = re.compile(r"(^|/)\.\.(/|$)|^[/~]|\\|:|\x00")

Source = Literal["both", "files", "ai"]
# Why a command may not run here, or None when it may: (project folder, manager, command).
CommandProblem = Callable[[str, str, str], str | None]


@dataclass(frozen=True)
class PlanOption:
    """One way to set up one project. source: "both" when the files and the AI agree."""

    id: str
    project: str
    stack: str
    manager: str
    source: Source
    evidence: tuple[str, ...]
    version_hint: str | None
    is_runnable: bool
    note: str | None


def option_id(project: str, stack: str, manager: str) -> str:
    return f"{stack}/{manager}" if project == "." else f"{project}: {stack}/{manager}"


def uses_own_plan(option: PlanOption) -> bool:
    """SetupPilot's hand-written plan needs the files the detector found."""
    return option.source != "ai" and option.manager in SUPPORTED_MANAGERS


def ground_analysis(analysis: RepoAnalysis, scan: RepoScan) -> tuple[RepoAnalysis, list[str]]:
    """Drop what the files do not back up, cap the sizes, and say what was dropped."""
    files = set(scan.files)
    folders = {"/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)}
    kept: list[Project] = []
    notes = []
    if len(analysis.projects) > MAX_PROJECTS:
        notes.append(f"The AI listed {len(analysis.projects)} projects; only {MAX_PROJECTS} kept.")
    for project in analysis.projects[:MAX_PROJECTS]:
        path = _clean_path(project.path)
        if path is None or (path != "." and path not in folders):
            notes.append(f"The AI named a folder that is not in the repo: {project.path[:80]!r}.")
            continue
        # On a real .NET repo the AI listed four samples, which made a simple repo a 5-way pick.
        if path != "." and project.role in ("docs", "example"):
            notes.append(f"Not offered: {path}, which the AI says is {project.role}.")
            continue
        evidence = [e for e in map(_clean_path, project.evidence) if e and _is_file(e, path, files)]
        if not evidence:
            notes.append(f"The AI named no real file for {path}, so it was left out.")
            continue
        steps = [s for s in project.steps if len(s.command) <= _MAX_COMMAND][:MAX_STEPS]
        kept.append(
            project.model_copy(
                update={
                    "path": path,
                    "name": _cut(project.name),
                    "frameworks": [_cut(f) for f in project.frameworks[:_MAX_ITEMS]],
                    "languages": project.languages[:_MAX_ITEMS],
                    "tools": project.tools[:_MAX_ITEMS],
                    "env": project.env[:_MAX_ITEMS],
                    "services": project.services[:_MAX_ITEMS],
                    "steps": [s.model_copy(update={"reason": _cut(s.reason)}) for s in steps],
                    "start_command": project.start_command
                    if project.start_command and len(project.start_command) <= _MAX_COMMAND
                    else None,
                    "evidence": evidence[:_MAX_ITEMS],
                }
            )
        )
    grounded = RepoAnalysis(
        summary=_cut(analysis.summary),
        projects=kept,
        notes=[_cut(n) for n in analysis.notes[:MAX_NOTES]],
    )
    return grounded, notes


def folders_to_detect(analysis: RepoAnalysis | None) -> list[str]:
    """The root, then each folder the AI named, in its order."""
    paths = ["."] + ([p.path for p in analysis.projects] if analysis else [])
    return list(dict.fromkeys(paths))


def reconcile(
    analysis: RepoAnalysis | None,
    detections: Mapping[str, Detection],
    command_problem: CommandProblem,
) -> list[PlanOption]:
    """detections: the detector's result for each folder to offer, the root first (see
    folders_to_detect). Without an analysis, only the root's detector candidates are offered."""
    projects = analysis.projects if analysis else []
    options: list[PlanOption] = []
    for folder, detection in detections.items():
        found = list(detection.candidates)
        here: list[PlanOption] = []
        files_first = False
        for project in (p for p in projects if p.path == folder):
            match = next((c for c in found if c.manager == project.package_manager), None)
            if match is not None:
                found.remove(match)
                here.append(_files_option(folder, match, "both", project, command_problem))
                continue
            note = None
            if found:
                files = ", ".join(f"{c.manager} ({c.evidence[0]})" for c in found)
                note = f"The files point to {files}; the AI thinks {project.package_manager}."
                files_first = files_first or any(set(c.evidence) & LOCK_FILES for c in found)
            here.append(_ai_option(folder, project, note, command_problem))
        rest = [_files_option(folder, c, "files", None, command_problem) for c in found]
        options += rest + here if files_first else here + rest
    # The AI may list the same project twice; the first one counts.
    unique: dict[str, PlanOption] = {}
    for option in options:
        unique.setdefault(option.id, option)
    return list(unique.values())


def ai_project(analysis: RepoAnalysis | None, option: PlanOption) -> Project | None:
    """The AI's project behind an option, when the option came from (or agrees with) the AI."""
    if analysis is None or option.source == "files":
        return None
    return next(
        (
            p
            for p in analysis.projects
            if p.path == option.project and p.package_manager == option.manager
        ),
        None,
    )


def runnable_steps(steps: list[Step]) -> list[Step]:
    return [s for s in steps if not s.is_manual]


def _files_option(
    folder: str, found: StackCandidate, source: Source, project: Project | None,
    command_problem: CommandProblem,
) -> PlanOption:  # fmt: skip
    problem = None
    if found.manager not in SUPPORTED_MANAGERS:
        # Then only the AI's own plan could set it up.
        problem = _ai_plan_problem(project, command_problem) if project else "no plan for it"
    return PlanOption(
        option_id(folder, found.stack, found.manager),
        folder,
        found.stack,
        found.manager,
        source,
        found.evidence,
        found.version_hint,
        problem is None,
        f"Not set up automatically: {problem}." if problem else None,
    )


def _ai_option(
    folder: str, project: Project, note: str | None, command_problem: CommandProblem
) -> PlanOption:
    stack = STACK_FOR_MANAGER.get(project.package_manager) or (
        project.languages[0] if project.languages else "other"
    )
    problem = _ai_plan_problem(project, command_problem)
    if problem:
        note = " ".join(filter(None, [note, f"Not set up automatically: {problem}."]))
    runtime = project.runtime
    return PlanOption(
        option_id(folder, stack, project.package_manager),
        folder,
        stack,
        project.package_manager,
        "ai",
        tuple(project.evidence),
        runtime.constraint if runtime else None,
        problem is None,
        note,
    )


def _ai_plan_problem(project: Project, command_problem: CommandProblem) -> str | None:
    """Why the AI's own plan cannot run here, or None. Every step must pass the same checks as
    any command, and every tool it needs must be one SetupPilot can check."""
    unknown = [t.name[:40] for t in project.tools if t.is_required and not known_tool(t.tool)]
    if unknown:
        return f"it needs tools SetupPilot cannot check ({', '.join(unknown)})"
    steps = runnable_steps(project.steps)
    if not any(s.phase != "verify" for s in steps):
        return "the AI gave no install step SetupPilot may run"
    for step in steps:
        problem = command_problem(project.path, project.package_manager, step.command)
        if problem:
            return f"the AI's step `{step.command[:80]}` is not allowed ({problem})"
    return None


def _clean_path(path: str) -> str | None:
    """A plain relative folder or file path with / only, or None for anything else."""
    path = path.strip().removeprefix("./").rstrip("/") or "."
    return None if _BAD_PATH.search(path) else path


def _is_file(path: str, folder: str, files: set[str]) -> bool:
    return path in files or f"{folder}/{path}" in files


def _cut(text: str) -> str:
    return text[:MAX_TEXT]
