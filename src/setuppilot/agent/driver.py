"""Start, answer and resume setup runs. The CLI and the web app both use this."""

from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from setuppilot.agent.graph import AgentDeps, build_graph
from setuppilot.errors import NotFound, SetupPilotError
from setuppilot.logging_setup import run_id_var


@dataclass(frozen=True)
class Waiting:
    """The run is paused until the user answers `question`."""

    run_id: str
    # Each answer must name the question it answers, so a late or repeated click cannot
    # approve a command the user never saw.
    question_id: str
    question: dict[str, Any]


@dataclass(frozen=True)
class Stopped:
    """The app stopped in the middle of a step. It can carry on from the last saved point."""

    run_id: str


@dataclass(frozen=True)
class Finished:
    run_id: str
    report: str
    is_verified: bool
    # "stack/manager" that was set up, or None when the run stopped before choosing one.
    stack: str | None
    # What went wrong (error_type, message, details), or None for a verified run.
    error: dict[str, str] | None


class SetupAgent:
    def __init__(self, deps: AgentDeps, checkpointer: BaseCheckpointSaver) -> None:
        self._deps = deps
        self._graph = build_graph(deps).compile(checkpointer=checkpointer)
        # Each node is one graph step; this backs up agent.max_steps with LangGraph's own stop.
        self._recursion_limit = deps.settings.agent.max_steps + 10

    def start(self, repo_url: str, dest_dir: str, use_docker: bool) -> Waiting | Finished:
        run = self._deps.runs.create_run(repo_url, dest_dir)
        return self.begin(run.id, repo_url, dest_dir, use_docker)

    def begin(
        self, run_id: str, repo_url: str, dest_dir: str, use_docker: bool
    ) -> Waiting | Finished:
        """Run the first steps of a run that is already saved (the web app makes it first)."""
        first_state = {
            "run_id": run_id,
            "repo_url": repo_url,
            "dest_dir": dest_dir,
            "use_docker": use_docker,
            "steps": 0,
            "next_index": 0,
        }
        return self._advance(run_id, first_state)

    def answer(self, run_id: str, question_id: str, reply: dict[str, Any]) -> Waiting | Finished:
        state = self.peek(run_id)
        if not isinstance(state, Waiting) or state.question_id != question_id:
            raise QuestionChanged(
                "This question was already answered or has changed.",
                details="Look at the run again to see what it is waiting for now.",
            )
        options = state.question.get("options")
        is_known_action = reply.get("action") in {"approve", "reject", "edit"}
        if options is None and not is_known_action:
            raise QuestionChanged(
                "Answer with approve, reject or edit.", details=f"Got {reply.get('action')!r}."
            )
        if options is not None and reply.get("choice") not in range(len(options)):
            raise QuestionChanged(
                "That choice is not one of the options.",
                details=f"Pick a number from 1 to {len(options)}.",
            )
        self._deps.runs.set_status(run_id, "running", None, None)
        return self._advance(run_id, Command(resume=reply))

    def peek(self, run_id: str) -> Waiting | Stopped | Finished:
        """Where a saved run stands, without moving it on."""
        snapshot = self._graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise NotFound(f"No saved run with id {run_id!r}. Start a new run instead.")
        questions = [i for task in snapshot.tasks for i in task.interrupts]
        if questions:
            return Waiting(run_id, questions[0].id, questions[0].value)
        if snapshot.next:
            return Stopped(run_id)
        return _finished(run_id, snapshot.values)

    def carry_on(self, run_id: str) -> Waiting | Finished:
        self._deps.runs.set_status(run_id, "running", None, None)
        return self._advance(run_id, None)

    def _advance(self, run_id: str, payload: Any) -> Waiting | Finished:
        # Every log line written while the run moves on carries its run id.
        token = run_id_var.set(run_id)
        try:
            result = self._graph.invoke(payload, self._config(run_id))
        finally:
            run_id_var.reset(token)
        if result.get("__interrupt__"):
            self._deps.runs.set_status(run_id, "waiting_approval", None, None)
            question = result["__interrupt__"][0]
            return Waiting(run_id, question.id, question.value)
        return _finished(run_id, result)

    def _config(self, run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": self._recursion_limit}


def _finished(run_id: str, values: dict[str, Any]) -> Finished:
    chosen = values.get("chosen")
    stack = f"{chosen['stack']}/{chosen['manager']}" if chosen else None
    return Finished(run_id, values["report"], values["is_verified"], stack, values.get("error"))


class QuestionChanged(SetupPilotError):
    """An answer did not match the question the run is waiting on now."""
