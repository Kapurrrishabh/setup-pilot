"""Join SetupPilot's own plan with the AI's steps. Every AI command is checked like any other;
one that fails the checks is shown as blocked and never offered to run, unless it is only the
AI's version of a command SetupPilot already runs."""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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
    # The AI's versions of SetupPilot's own commands that were left out because they add to them.
    notes: list[str]


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
    own_texts = [*quiet, *(c.text for c in checks), *(c.text for c in installs)]
    # SetupPilot's own command for each form it runs. The AI's copy of one is never blocked.
    own_by_verb: dict[object, str] = {v: t for t in own_texts if (v := verb(t)) is not None}
    own_checks = {verb(c.text) for c in checks} | {verb(q) for q in quiet}
    allowed: list[Step] = []
    blocked = []
    notes: list[str] = []
    for step in steps:
        reason = problem(step.command)
        if reason is None:
            allowed.append(step)
        elif (mine := own_by_verb.get(verb(step.command))) is None:
            blocked.append(_blocked(step, "check" if step.phase == "verify" else "extra", reason))
        # SetupPilot runs its own check of that form anyway, so no note, as for an allowed one.
        elif step.phase != "verify" or verb(step.command) not in own_checks:
            notes += _not_used(step.command, mine)
    setup = [s for s in allowed if s.phase != "verify"]
    own = []
    for command in installs:
        item, not_used = _install(command, setup, verb)
        own.append(item)
        notes += not_used
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
    ai_checks = [s for s in allowed if s.phase == "verify" and verb(s.command) not in own_checks]
    items += [
        plan_item(s.command, f"Check from the AI: {s.reason}", "check", "ai")
        for s in ai_checks[:MAX_AI_CHECKS]
    ]
    manual = _manual_steps(project, own_by_verb, verb) if project else []
    return MergedPlan(items + blocked, manual, notes)


def _blocked(step: Step, kind: str, reason: str) -> dict:
    item = plan_item(step.command, f"From the AI: {step.reason}", kind, "ai")
    return item | {"status": "blocked", "block_reason": reason}


def _install(
    own: PlannedCommand, setup: list[Step], verb: Callable[[str], object]
) -> tuple[dict, list[str]]:
    """SetupPilot's command, or the AI's wording of it when that only adds safe flags. Any other
    AI version is left out, with a note when it adds something."""
    mine = plan_item(own.text, own.reason, "install", "setuppilot")
    match = next((s for s in setup if verb(s.command) == verb(own.text)), None)
    if match is None:
        return mine, []
    setup.remove(match)
    extra = _extra_arguments(own.text, match.command)
    unsafe = None if extra is None else extra - _SAFE_EXTRAS.get(_program(own.text), set())
    if unsafe == set() and match.command != own.text:
        reason = f"From the AI: {match.reason} SetupPilot's default: `{own.text}`."
        return plan_item(match.command, reason, "install", "ai"), []
    return mine, _not_used(match.command, own.text)


def _not_used(ai: str, own: str) -> list[str]:
    """One note when the AI's version adds to SetupPilot's own command. One that only leaves
    words out (real Groq wrote `uv venv` for `uv venv .venv`) says nothing new, so it gets none."""
    mine, theirs = Counter(split_command(own)[1:]), Counter(split_command(ai)[1:])
    added, left_out = theirs - mine, mine - theirs
    if not added:
        return []
    change = " ".join(added.elements())
    if left_out:
        change += " and leaves out " + " ".join(left_out.elements())
    return [f"Not used: the AI's `{ai}`, which adds {change}. SetupPilot runs its own `{own}`."]


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


def _manual_steps(
    project: Project, own_by_verb: Mapping[object, str], verb: Callable[[str], object]
) -> list[str]:
    """Steps for the user, leaving out SetupPilot's own commands that the AI marked manual,
    unless the AI's version adds to them."""
    steps = [
        f"{s.command} ({s.reason})"
        for s in project.steps
        if s.is_manual
        and ((mine := own_by_verb.get(verb(s.command))) is None or _not_used(s.command, mine))
    ]
    for env in project.env:
        what = "file" if env.kind == "config_file" else "environment variable"
        need = "needed" if env.is_required else "optional"
        steps.append(f"Set the {what} {env.name} ({need}; see {env.source})")
    for service in project.services:
        version = f" {service.version}" if service.version else ""
        steps.append(f"It uses {service.kind}{version} ({service.how})")
    return steps
