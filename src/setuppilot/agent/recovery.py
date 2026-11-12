"""What to do after a failed command, from the AI's diagnosis. The AI only proposes: every fix is
checked like any command, and a command that already failed is never offered again."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from setuppilot.analysis.schema import Diagnosis
from setuppilot.guardrails.commands import split_command
from setuppilot.tools.prereqs import known_tool

# PROVISIONAL (research-planning.md): two fixes per diagnosis keep each round easy to review.
# Confirm on the failures eval-setup finds.
MAX_FIX_COMMANDS = 2
_MAX_ACTION_CHARS = 300


@dataclass(frozen=True)
class RecoveryDecision:
    # (command, reason) for each fix that passed the checks.
    fixes: tuple[tuple[str, str], ...]
    # (command, why it was refused). These become steps for the user to do by hand.
    refused: tuple[tuple[str, str], ...]
    # SetupPilot's names for the tools the AI says are missing. The probe checks them again.
    missing_tools: tuple[str, ...]
    user_action: str | None


def is_repeat(text: str, tried: Sequence[str]) -> bool:
    """The same arguments as a command that already failed in this run."""
    argv = _argv(text)
    return argv is not None and argv in {_argv(t) for t in tried}


def decide_recovery(
    diagnosis: Diagnosis, tried: Sequence[str], problem: Callable[[str], str | None]
) -> RecoveryDecision:
    """tried: every command that failed in this run, the one being fixed included.
    problem: why a command may not run here, or None."""
    fixes, refused = [], []
    for fix in diagnosis.commands[:MAX_FIX_COMMANDS]:
        reason = "it already failed in this run" if is_repeat(fix.command, tried) else None
        reason = reason or problem(fix.command)
        if reason:
            refused.append((fix.command, reason))
        else:
            fixes.append((fix.command, fix.reason[:_MAX_ACTION_CHARS]))
    tools = [known_tool(t) for t in diagnosis.missing_tools]
    action = diagnosis.user_action if diagnosis.needs_user else None
    return RecoveryDecision(
        tuple(fixes),
        tuple(refused),
        tuple(dict.fromkeys(t for t in tools if t)),
        action[:_MAX_ACTION_CHARS] if action else None,
    )


def _argv(text: str) -> tuple[str, ...] | None:
    try:
        return tuple(split_command(text))
    except ValueError:
        return None
