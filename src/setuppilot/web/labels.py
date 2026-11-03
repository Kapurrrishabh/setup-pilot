"""The plain words the web pages show for stored values. Templates and scripts both read these."""

from typing import Any, Literal

from setuppilot.db.runs import Step

# "paused" is never saved: the pages show it for a run that a restart stopped mid-step.
STATUS_LABELS = {
    "running": "Working",
    "waiting_approval": "Waiting for you",
    "paused": "Paused",
    "succeeded": "Verified",
    "failed": "Not verified",
    "cancelled": "Cancelled",
}

PHASES = [
    "Download",
    "Look at the repo",
    "Check tools",
    "Plan",
    "Run commands",
    "Check it works",
    "Report",
]

# "then" is what the run does right after the step; the run page shows it while the run is busy.
STEPS: dict[str, dict[str, Any]] = {
    "clone": {"label": "Download the repo", "phase": 0, "then": "Looking at the files"},
    "scan": {"label": "Look at the files", "phase": 1, "then": "Finding the stack"},
    "detect": {"label": "Find the stack", "phase": 1, "then": "Checking your tools"},
    "prerequisites": {"label": "Check your tools", "phase": 2, "then": "Picking the stack"},
    "choose_stack": {"label": "Pick the stack", "phase": 2, "then": "Making a plan"},
    "plan": {"label": "Make a plan", "phase": 3, "then": "Getting the first command ready"},
    "approve": {"label": "Your answer", "phase": 4, "then": "Working on your answer"},
    "execute": {"label": "Run a command", "phase": 4, "then": "Getting the next step ready"},
    "verify": {"label": "Check that it works", "phase": 5, "then": "Writing the report"},
    "report": {"label": "Write the report", "phase": 6, "then": "Finishing"},
}
FIRST_ACTIVITY = "Downloading the repo"

ERROR_LABELS = {
    "ConfigInvalid": "Config file problem",
    "BadURL": "Not a GitHub repo URL",
    "CloneFailed": "Could not download the repo",
    "PrereqMissing": "Missing tools",
    "PathBlocked": "Unsafe paths in the repo",
    "CommandBlocked": "Command not allowed",
    "CommandTimeout": "Command took too long",
    "StackNotFound": "Nothing to set up",
    "InstallFailed": "Install command failed",
    "VerifyFailed": "Setup not confirmed",
    "RunCancelled": "Cancelled by you",
    "TooManySteps": "Too many steps",
    "LLMUnavailable": "AI not available",
    "LLMRequestRejected": "AI request refused",
    "BudgetExceeded": "Token budget used up",
    "DatabaseUnavailable": "Database not reachable",
}

PAGE_LABELS = {
    "statuses": STATUS_LABELS,
    "phases": PHASES,
    "steps": STEPS,
    "first_activity": FIRST_ACTIVITY,
    "errors": ERROR_LABELS,
}

# Step summaries that mean "this did not go to plan", although the step itself worked.
_ANSWERS_TO_FLAG = ("Edit was blocked", "Rejected:")


def error_label(error_type: str) -> str:
    # Old runs can hold error names from an earlier version; show those as they are.
    return ERROR_LABELS.get(error_type, error_type)


def step_tone(step: Step) -> Literal["ok", "warn", "bad"]:
    if not step.is_ok or step.exit_code not in (None, 0):
        return "bad"
    if step.name == "approve" and step.result_summary.startswith(_ANSWERS_TO_FLAG):
        return "warn"
    return "ok"


def duration_text(seconds: float) -> str:
    """1.4 s, 12 s, 2 min 5 s, 1 h 3 min."""
    if seconds < 1:
        return "under 1 s"
    if seconds < 9.95:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(round(seconds), 60)
    if minutes == 0:
        return f"{rest} s"
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def repo_name(repo_url: str) -> str:
    """owner/name, for places where the full URL is too long."""
    return repo_url.removeprefix("https://github.com/")
