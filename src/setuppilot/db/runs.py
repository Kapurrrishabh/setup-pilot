"""Runs and their steps, and an in-memory store for tests and for when the database is down."""

import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from setuppilot.errors import NotFound

RunStatus = Literal["running", "waiting_approval", "paused", "succeeded", "failed", "cancelled"]
FINISHED_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class Run:
    id: str
    repo_url: str
    dest_dir: str
    status: RunStatus
    error_type: str | None
    message: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Step:
    run_id: str
    name: str
    tool: str | None
    arguments: dict[str, Any]
    result_summary: str
    is_ok: bool
    error_type: str | None
    exit_code: int | None
    duration_ms: int
    tokens: int
    model: str | None


class RunStore(Protocol):
    def create_run(self, repo_url: str, dest_dir: str) -> Run: ...
    def get_run(self, run_id: str) -> Run: ...
    def list_runs(self, limit: int) -> list[Run]: ...
    def set_status(
        self, run_id: str, status: RunStatus, error_type: str | None, message: str | None
    ) -> None: ...
    def delete_run(self, run_id: str) -> None: ...
    def add_step(self, step: Step) -> None: ...
    def list_steps(self, run_id: str) -> list[Step]: ...


def no_such_run(run_id: str) -> NotFound:
    return NotFound(f"No run with id {run_id!r}. Check the run id, or start a new run.")


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._steps: list[Step] = []

    def create_run(self, repo_url: str, dest_dir: str) -> Run:
        run = Run(
            str(uuid.uuid4()), repo_url, dest_dir, "running", None, None, datetime.now(UTC), None
        )
        self._runs[run.id] = run
        return run

    def get_run(self, run_id: str) -> Run:
        if run_id not in self._runs:
            raise no_such_run(run_id)
        return self._runs[run_id]

    def list_runs(self, limit: int) -> list[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def set_status(
        self, run_id: str, status: RunStatus, error_type: str | None, message: str | None
    ) -> None:
        finished_at = datetime.now(UTC) if status in FINISHED_STATUSES else None
        self._runs[run_id] = replace(
            self.get_run(run_id),
            status=status,
            error_type=error_type,
            message=message,
            finished_at=finished_at,
        )

    def delete_run(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        self._steps = [s for s in self._steps if s.run_id != run_id]

    def add_step(self, step: Step) -> None:
        self.get_run(step.run_id)
        # Same as the database: arguments must be JSON, and come back the way JSON does.
        self._steps.append(replace(step, arguments=json.loads(json.dumps(step.arguments))))

    def list_steps(self, run_id: str) -> list[Step]:
        return [s for s in self._steps if s.run_id == run_id]
