"""Join SetupPilot's own plan with the AI's steps. Every AI command is checked like any other;
one that fails the checks is shown as blocked and never offered to run."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from setuppilot.analysis.reconcile import runnable_steps
from setuppilot.analysis.schema import Project, Step
from setuppilot.guardrails.commands import split_command
from setuppilot.setup.plans import PlannedCommand

# PROVISIONAL: optional AI steps and AI checks offered per run, so a long AI list cannot bury
# SetupPilot's own steps under approvals.
MAX_EXTRAS = 3
MAX_AI_CHECKS = 3
# Flags the AI may add to SetupPilot's own install: they only pick which of the project's own
# packages go in, or keep to the lock file. Others (an index URL, a script shell) could change
# what runs while the step still looks like SetupPilot's, so then SetupPilot runs its own.
_SAFE_EXTRAS: dict[str, set[str]] = {
    "uv": {"--all-extras", "--frozen", "--locked", "--seed"},
    "poetry": {"--all-extras", "--no-root"},
    "npm": {"--legacy-peer-deps", "--no-audit", "--no-fund"},
    "yarn": {"--frozen-lockfile", "--immutable"},
    "pnpm": {"--frozen-lockfile"},
    "bun": {"--frozen-lockfile"},
    "cargo": {"--locked"},
    "composer": {"--no-dev", "--prefer-dist"},
    "dotnet": {"--locked-mode"},
}


@dataclass(frozen=True)
class MergedPlan:
    items: list[dict]
    # Things for the user to do by hand, from the AI. Never run, and not checked.
    manual_steps: list[str]


def plan_item(text: str, reason: str, kind: str, origin: str) -> dict:
    """kind: install, check, extra (optional) or fix. origin: setuppilot, ai, fix or you."""
    return {"text": text, "reason": reason, "kind": kind, "status": "pending", "origin": origin}


def merge_plan(
    installs: list[PlannedCommand],
    checks: list[PlannedCommand],
    quiet: Sequence[str],
    project: Project | None,
    problem: Callable[[str], str | None],
    verb: Callable[[str], tuple[str, ...] | None],
) -> MergedPlan:
    """quiet: the commands SetupPilot's quiet checks run, so the AI's copy of one is left out.
    problem: why a command may not run here, or None. verb: a command's program and allowed
    form, so `npm ci` from the AI can stand in for SetupPilot's `npm ci`."""
    steps = runnable_steps(project.steps) if project else []
    allowed: list[Step] = []
    blocked = []
    for step in steps:
        reason = problem(step.command)
        if reason is None:
            allowed.append(step)
            continue
        blocked.append(_blocked(step, "check" if step.phase == "verify" else "extra", reason))
    setup = [s for s in allowed if s.phase != "verify"]
    own = []
    for command in installs:
        item, not_used = _install(command, setup, verb)
        own.append(item)
        blocked += not_used
    taken = {verb(i["text"]) for i in own} | {verb(c.text) for c in checks}
    extras = [s for s in setup if verb(s.command) not in taken]
    # With no plan of its own, SetupPilot runs the AI's steps as the install itself.
    kind = "extra" if installs else "install"
    extras = extras[:MAX_EXTRAS] if installs else extras
    added = [plan_item(s.command, f"From the AI: {s.reason}", kind, "ai") for s in extras]
    # A prepare step runs first: `uv venv` after `uv sync` would replace the synced venv.
    first = [item for item, step in zip(added, extras, strict=True) if step.phase == "prepare"]
    items = first + own + [item for item in added if item not in first]
    items += [plan_item(c.text, c.reason, "check", "setuppilot") for c in checks]
    own_checks = {verb(c.text) for c in checks} | {verb(q) for q in quiet}
    ai_checks = [s for s in allowed if s.phase == "verify" and verb(s.command) not in own_checks]
    items += [
        plan_item(s.command, f"Check from the AI: {s.reason}", "check", "ai")
        for s in ai_checks[:MAX_AI_CHECKS]
    ]
    return MergedPlan(items + blocked, _manual_steps(project) if project else [])


def _blocked(step: Step, kind: str, reason: str) -> dict:
    item = plan_item(step.command, f"From the AI: {step.reason}", kind, "ai")
    return item | {"status": "blocked", "block_reason": reason}


def _install(
    own: PlannedCommand, setup: list[Step], verb: Callable[[str], object]
) -> tuple[dict, list[dict]]:
    """SetupPilot's command, or the AI's wording of it when that only adds safe flags. Any other
    AI version comes back blocked, so the plan shows it was not used."""
    mine = plan_item(own.text, own.reason, "install", "setuppilot")
    match = next((s for s in setup if verb(s.command) == verb(own.text)), None)
    if match is None:
        return mine, []
    setup.remove(match)
    extra = _extra_arguments(own.text, match.command)
    unsafe = None if extra is None else extra - _SAFE_EXTRAS.get(_program(own.text), set())
    if unsafe == set():
        reason = f"From the AI: {match.reason}"
        if match.command != own.text:
            reason += f" SetupPilot's default: `{own.text}`."
        return plan_item(match.command, reason, "install", "ai"), []
    why = f"Not used: SetupPilot runs its own `{own.text}`, which its checks are made for."
    if unsafe:
        added = ", ".join(sorted(unsafe))
        why = f"Not used: SetupPilot does not take {added} from the AI for its own `{own.text}`."
    return mine, [_blocked(match, "extra", why)]


def _extra_arguments(own: str, ai: str) -> set[str] | None:
    """What the AI's version adds, or None when it runs another program or drops any of
    SetupPilot's arguments, so its checks may not fit: on real Groq the AI restored one .csproj
    where the check builds the whole .sln. The same rule keeps conda's -p ./.conda-env and a
    repo's ./mvnw (not an mvn that may be missing)."""
    mine, theirs = split_command(own), split_command(ai)
    if _program(own) != _program(ai) or not Counter(mine[1:]) <= Counter(theirs[1:]):
        return None
    return set(Counter(theirs[1:]) - Counter(mine[1:]))


def _program(text: str) -> str:
    return split_command(text)[0].replace("\\", "/").removeprefix("./")


def _manual_steps(project: Project) -> list[str]:
    steps = [f"{s.command} ({s.reason})" for s in project.steps if s.is_manual]
    for env in project.env:
        what = "file" if env.kind == "config_file" else "environment variable"
        need = "needed" if env.is_required else "optional"
        steps.append(f"Set the {what} {env.name} ({need}; see {env.source})")
    for service in project.services:
        version = f" {service.version}" if service.version else ""
        steps.append(f"It uses {service.kind}{version} ({service.how})")
    return steps
