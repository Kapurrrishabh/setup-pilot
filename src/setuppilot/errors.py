"""Errors the user sees: a short friendly message, plus details for the "details" section."""


class NotFound(LookupError):
    """A run or repo id that does not exist."""


class SetupPilotError(Exception):
    def __init__(self, message: str, details: str) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class BadURL(SetupPilotError):
    """The repo URL is not https://github.com/<owner>/<repo>."""


class CloneFailed(SetupPilotError):
    """git clone did not work, or the repo is too big."""


class PrereqMissing(SetupPilotError):
    """A tool the setup needs (git, node, docker...) is not installed."""


class PathBlocked(SetupPilotError):
    """A path points outside the workspace folder."""


class CommandBlocked(SetupPilotError):
    """A command is not on the allowlist, or uses chaining, redirects or other blocked parts."""


class CommandTimeout(SetupPilotError):
    """A command ran longer than its time limit and was stopped."""


class StackNotFound(SetupPilotError):
    """The repo has no file that says how to install it (no manifest)."""


class InstallFailed(SetupPilotError):
    """An install command ran but ended with an error."""


class VerifyFailed(SetupPilotError):
    """The install finished, but the check that it really works did not pass."""


class RunCancelled(SetupPilotError):
    """The user pressed Cancel; the run stops before its next step."""


class TooManySteps(SetupPilotError):
    """A run took more steps than agent.max_steps allows, so it was stopped."""


class LLMUnavailable(SetupPilotError):
    """Groq could not answer: a limit was reached, the key is missing, or the service is down."""


class LLMRequestRejected(SetupPilotError):
    """Groq refused the request: bad key, unknown model, or a prompt that is too long."""


class BudgetExceeded(SetupPilotError):
    """A token budget from config.yaml is used up."""


class DatabaseUnavailable(SetupPilotError):
    """Supabase is not set up, not reachable, or the connection was lost."""
