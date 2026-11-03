"""Every tool returns a ToolResult, never a raw exception."""

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from setuppilot.errors import SetupPilotError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolResult:
    data: Any
    error_type: str | None
    message: str
    details: str

    @property
    def ok(self) -> bool:
        return self.error_type is None


def run_tool(tool: Callable[..., Any], *args: Any, **kwargs: Any) -> ToolResult:
    name = getattr(tool, "__name__", type(tool).__name__)
    try:
        data = tool(*args, **kwargs)
    except SetupPilotError as error:
        return ToolResult(None, type(error).__name__, error.message, error.details)
    except Exception:
        # A bug, not a user problem. Keep the run alive but log the full traceback loudly.
        log.exception("tool crashed", extra={"tool": name})
        return ToolResult(None, "UnexpectedError", f"{name} failed.", traceback.format_exc())
    return ToolResult(data, None, f"{name} done.", "")
