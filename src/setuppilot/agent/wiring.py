"""Build the real setup agent: real clone, real tool checks, real command runner."""

from collections.abc import Callable

from setuppilot.agent.driver import SetupAgent
from setuppilot.agent.graph import AgentDeps
from setuppilot.config import Settings
from setuppilot.db.stores import Stores
from setuppilot.guardrails.runner import run_command
from setuppilot.llm.client import LLMClient
from setuppilot.tools.clone import check_github_repo, clone_repo
from setuppilot.tools.prereqs import SystemToolProbe


def build_agent(
    settings: Settings, stores: Stores, llm: LLMClient | None, on_start: Callable[[int], None]
) -> SetupAgent:
    """on_start gets each command's process group, so a Cancel button can stop it."""
    deps = AgentDeps(
        settings=settings,
        runs=stores.runs,
        llm=llm,
        clone=lambda ref, dest: clone_repo(ref, dest, settings.repo, check_github_repo),
        probe=SystemToolProbe(),
        run=lambda command: run_command(command, settings.runner, on_start=on_start),
    )
    return SetupAgent(deps, stores.checkpointer)
