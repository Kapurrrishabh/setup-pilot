"""Setup runs in background threads, so a page refresh or a closed tab never stops a run."""

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from setuppilot.agent.driver import Finished, SetupAgent, Stopped, Waiting
from setuppilot.db.runs import FINISHED_STATUSES, RunStore
from setuppilot.errors import SetupPilotError
from setuppilot.guardrails.runner import cancel_group
from setuppilot.logging_setup import run_id_var

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
        self._busy: set[str] = set()
        self._problems: dict[str, dict[str, str]] = {}

    def start(self, repo_url: str, dest_dir: str, use_docker: bool) -> str:
        run = self._runs.create_run(repo_url, dest_dir)
        self._in_background(
            run.id, lambda: self._agent.begin(run.id, repo_url, dest_dir, use_docker)
        )
        return run.id

    def answer(self, run_id: str, question_id: str, reply: dict[str, Any]) -> None:
        state = self._agent.peek(run_id)
        if not isinstance(state, Waiting) or state.question_id != question_id:
            raise RunNotWaiting("This question was already answered or has changed.")
        self._in_background(run_id, lambda: self._agent.answer(run_id, question_id, reply))

    def carry_on(self, run_id: str) -> None:
        if not isinstance(self._agent.peek(run_id), Stopped):
            raise RunNotWaiting("This run has nothing left to carry on with.")
        self._in_background(run_id, lambda: self._agent.carry_on(run_id))

    def cancel(self, run_id: str) -> None:
        if self._runs.get_run(run_id).status in FINISHED_STATUSES:
            raise RunNotWaiting("This run has already finished, so there is nothing to cancel.")
        # Saved first: every step checks it, so even a busy run stops at its next step.
        self._runs.set_status(run_id, "cancelled", "RunCancelled", "You cancelled this run.")
        group = self._tracker.group_for(run_id)
        if group is not None:
            threading.Thread(target=cancel_group, args=(group,), daemon=True).start()

    def view(self, run_id: str) -> dict[str, Any]:
        """Everything the live page shows. Read from saved state, so it survives a refresh."""
        run = self._runs.get_run(run_id)
        with self._lock:
            is_busy = run_id in self._busy
            problem = self._problems.get(run_id)
        state: Waiting | Stopped | Finished | None = None
        if not is_busy:
            try:
                state = self._agent.peek(run_id)
            except LookupError:
                state = None
        is_cancelled = run.status == "cancelled"
        return {
            "run": {k: str(v) if v is not None else None for k, v in asdict(run).items()},
            "steps": [asdict(s) for s in self._runs.list_steps(run_id)],
            "is_busy": is_busy,
            "question": state.question if isinstance(state, Waiting) and not is_cancelled else None,
            "question_id": state.question_id if isinstance(state, Waiting) else None,
            "can_carry_on": isinstance(state, Stopped) and not is_cancelled,
            "report": state.report if isinstance(state, Finished) else None,
            "error": state.error if isinstance(state, Finished) else None,
            "is_verified": state.is_verified if isinstance(state, Finished) else False,
            "problem": problem,
        }

    def _in_background(self, run_id: str, work: Callable[[], object]) -> None:
        # Read from the database too: a cancel from before a restart still counts.
        if self._runs.get_run(run_id).status == "cancelled":
            raise RunNotWaiting("This run was cancelled. Start a new run instead.")
        with self._lock:
            if run_id in self._busy:
                raise RunNotWaiting("This run is still working. Wait for it to finish this step.")
            self._busy.add(run_id)
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
                self._busy.discard(run_id)

    def _remember_problem(self, run_id: str, message: str, details: str) -> None:
        with self._lock:
            self._problems[run_id] = {"message": message, "details": details}


class RunNotWaiting(SetupPilotError):
    def __init__(self, message: str) -> None:
        super().__init__(message, details="Reload the page to see where the run is now.")
