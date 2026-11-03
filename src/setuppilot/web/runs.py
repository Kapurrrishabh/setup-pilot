"""Setup runs in background threads, so a page refresh or a closed tab never stops a run."""

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from setuppilot.agent.driver import Finished, SetupAgent, Stopped, Waiting
from setuppilot.db.runs import FINISHED_STATUSES, Run, RunStore
from setuppilot.errors import SetupPilotError
from setuppilot.guardrails.runner import cancel_group
from setuppilot.logging_setup import run_id_var
from setuppilot.tools.clone import repo_folder
from setuppilot.tools.github_url import parse_github_url
from setuppilot.web.labels import duration_text, step_tone

log = logging.getLogger(__name__)


class CommandTracker:
    """Which command each run is running now, so Cancel can stop it. The runner reports in."""

    def __init__(self) -> None:
        self._groups: dict[str, int] = {}
        self._lock = threading.Lock()

    def track(self, group_id: int) -> None:
        run_id = run_id_var.get()
        if run_id is not None:
            with self._lock:
                self._groups[run_id] = group_id

    def group_for(self, run_id: str) -> int | None:
        with self._lock:
            return self._groups.get(run_id)

    def forget(self, run_id: str) -> None:
        with self._lock:
            self._groups.pop(run_id, None)


class RunManager:
    def __init__(self, agent: SetupAgent, runs: RunStore, tracker: CommandTracker) -> None:
        self._agent = agent
        self._runs = runs
        self._tracker = tracker
        self._lock = threading.Lock()
        # Each busy run, with the approved command it is running (None for other work).
        self._busy: dict[str, str | None] = {}
        self._problems: dict[str, dict[str, str]] = {}

    def start(self, repo_url: str, dest_dir: str, use_docker: bool) -> str:
        run = self._runs.create_run(repo_url, dest_dir)
        self._in_background(
            run.id, lambda: self._agent.begin(run.id, repo_url, dest_dir, use_docker), command=None
        )
        return run.id

    def answer(self, run_id: str, question_id: str, reply: dict[str, Any]) -> None:
        state = self._agent.peek(run_id)
        if not isinstance(state, Waiting) or state.question_id != question_id:
            raise RunNotWaiting("This question was already answered or has changed.")
        command = state.question.get("command") if reply.get("action") == "approve" else None
        self._in_background(
            run_id, lambda: self._agent.answer(run_id, question_id, reply), command=command
        )

    def carry_on(self, run_id: str) -> None:
        if not isinstance(self._agent.peek(run_id), Stopped):
            raise RunNotWaiting("This run has nothing left to carry on with.")
        self._in_background(run_id, lambda: self._agent.carry_on(run_id), command=None)

    def cancel(self, run_id: str) -> None:
        if self._runs.get_run(run_id).status in FINISHED_STATUSES:
            raise RunNotWaiting("This run has already finished, so there is nothing to cancel.")
        # Saved first: every step checks it, so even a busy run stops at its next step.
        self._runs.set_status(run_id, "cancelled", "RunCancelled", "You cancelled this run.")
        group = self._tracker.group_for(run_id)
        if group is not None:
            threading.Thread(target=cancel_group, args=(group,), daemon=True).start()

    def shown_status(self, run: Run) -> str:
        """A run saved as running with no worker was stopped by a restart, so it waits for you."""
        with self._lock:
            is_busy = run.id in self._busy
        return "paused" if run.status == "running" and not is_busy else run.status

    def view(self, run_id: str) -> dict[str, Any]:
        """Everything the live page shows. Read from saved state, so it survives a refresh."""
        run = self._runs.get_run(run_id)
        with self._lock:
            is_busy = run_id in self._busy
            running_command = self._busy.get(run_id)
            problem = self._problems.get(run_id)
        state: Waiting | Stopped | Finished | None = None
        if not is_busy:
            try:
                state = self._agent.peek(run_id)
            except LookupError:
                state = None
        is_cancelled = run.status == "cancelled"
        steps = self._runs.list_steps(run_id)
        is_cloned = any(s.name == "clone" and s.is_ok for s in steps)
        # A clone that worked also proves the URL parses.
        repo_dir = (
            repo_folder(parse_github_url(run.repo_url), Path(run.dest_dir)) if is_cloned else None
        )
        return {
            "run": {k: str(v) if v is not None else None for k, v in asdict(run).items()},
            "steps": [
                asdict(s) | {"tone": step_tone(s), "duration": duration_text(s.duration_ms / 1000)}
                for s in steps
            ],
            "repo_dir": str(repo_dir) if repo_dir else None,
            "is_busy": is_busy,
            "running_command": running_command,
            "question": state.question if isinstance(state, Waiting) and not is_cancelled else None,
            "question_id": state.question_id if isinstance(state, Waiting) else None,
            "can_carry_on": isinstance(state, Stopped) and not is_cancelled,
            "report": state.report if isinstance(state, Finished) else None,
            "error": state.error if isinstance(state, Finished) else None,
            "is_verified": state.is_verified if isinstance(state, Finished) else False,
            "problem": problem,
        }

    def _in_background(
        self, run_id: str, work: Callable[[], object], *, command: str | None
    ) -> None:
        # Read from the database too: a cancel from before a restart still counts.
        if self._runs.get_run(run_id).status == "cancelled":
            raise RunNotWaiting("This run was cancelled. Start a new run instead.")
        with self._lock:
            if run_id in self._busy:
                raise RunNotWaiting("This run is still working. Wait for it to finish this step.")
            self._busy[run_id] = command
            self._problems.pop(run_id, None)
        threading.Thread(target=self._work, args=(run_id, work), daemon=True).start()

    def _work(self, run_id: str, work: Callable[[], object]) -> None:
        token = run_id_var.set(run_id)
        try:
            work()
        except SetupPilotError as error:
            self._remember_problem(run_id, error.message, error.details)
        except Exception as error:
            # A bug: keep the server up, show it on the page, and log the full traceback.
            log.exception("run worker crashed", extra={"run_id": run_id})
            self._remember_problem(run_id, "Something went wrong inside SetupPilot.", repr(error))
        finally:
            run_id_var.reset(token)
            self._tracker.forget(run_id)
            with self._lock:
                self._busy.pop(run_id, None)

    def _remember_problem(self, run_id: str, message: str, details: str) -> None:
        with self._lock:
            self._problems[run_id] = {"message": message, "details": details}


class RunNotWaiting(SetupPilotError):
    def __init__(self, message: str) -> None:
        super().__init__(message, details="Reload the page to see where the run is now.")
