"""Terminal front end for setup runs: shows progress and asks the user at each pause."""

import sys
from collections.abc import Callable
from pathlib import Path

from setuppilot.agent.driver import Finished, SetupAgent, Stopped, Waiting, answers_for
from setuppilot.agent.wiring import build_agent
from setuppilot.config import Settings
from setuppilot.db.stores import Stores
from setuppilot.errors import LLMUnavailable
from setuppilot.llm.client import LLMClient
from setuppilot.llm.groq import build_llm_client
from setuppilot.tools.clone import public_github

Ask = Callable[[str], str]


def make_agent(settings: Settings, stores: Stores) -> SetupAgent:
    # The terminal has no Cancel button (Ctrl+C stops the command), so nothing is tracked.
    llm = _llm_or_none(settings, stores)
    return build_agent(settings, stores, llm, on_start=lambda t: None, github=public_github())


def interact(
    agent: SetupAgent, stores: Stores, outcome: Waiting | Stopped | Finished, ask: Ask
) -> bool:
    """Answer pauses until the run ends. Returns whether the setup was verified."""
    shown = 0
    while True:
        steps = stores.runs.list_steps(outcome.run_id)
        for step in steps[shown:]:
            print(f"  [{'ok' if step.is_ok else 'FAILED'}] {step.name}: {step.result_summary}")
        shown = len(steps)
        if isinstance(outcome, Finished):
            print("\n" + outcome.report)
            if outcome.error and outcome.error["details"]:
                print(f"\nDetails:\n{outcome.error['details']}")
            return outcome.is_verified
        if isinstance(outcome, Stopped):
            print("\nThe app stopped in the middle of a step. Carrying on may run the last")
            print("approved command again.")
            if ask("Carry on? [y/n]: ").strip().lower() != "y":
                return False
            outcome = agent.carry_on(outcome.run_id)
            continue
        reply = _question(outcome, ask)
        if reply is None:
            if stores.database_warning:
                print("\nStopped. Without a database this run cannot be resumed; start a new run.")
            else:
                print(f"\nPaused. Continue later with: setuppilot resume {outcome.run_id}")
            return False
        outcome = agent.answer(outcome.run_id, outcome.question_id, reply)


def _question(waiting: Waiting, ask: Ask) -> dict | None:
    question = waiting.question
    if question["kind"] == "choose_stack":
        print("\nThis repo can be set up in more than one way:")
        for number, option in enumerate(question["options"], start=1):
            missing = [c["tool"] for c in option["prereqs"] if not c["is_installed"]]
            extra = f" (missing: {', '.join(missing)})" if missing else ""
            print(f"  {number}. {option['label']}, from {', '.join(option['evidence'])}{extra}")
        for conflict in question["conflicts"]:
            print(f"  Note: {conflict}")
        while True:
            answer = ask("Pick a number (or q to stop for now): ").strip()
            if answer == "q":
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(question["options"]):
                return {"choice": int(answer) - 1}
    if question["kind"] == "install_tool":
        print(f"\n{question['tool']} is missing (try {question['round']}): {question['problem']}")
        if question["reason"]:
            print(f"  It is needed: {question['reason']}")
        if question["command"]:
            admin = "  (it may ask for admin rights)" if question["needs_admin"] else ""
            print(f"  SetupPilot can run: {question['command']}{admin}")
        else:
            print(f"  Install it yourself: {question['manual']}")
        if question["note"]:
            print(f"  Note: {question['note']}")
        if last := question["last_install"]:
            print(f"  Last try: {last['command']} ended with exit code {last['exit_code']}.")
        return _pick(question, ask)
    if question["kind"] == "fix_by_hand":
        print(f"\n`{question['command']}` failed with exit code {question['exit_code']}.")
        print(f"  The AI's guess (it may be wrong): {question['diagnosis']['explanation']}")
        if question["user_action"]:
            print(f"  What to do: {question['user_action']}")
        for refused in question["refused"]:
            print(f"  Not run by SetupPilot: {refused['command']} ({refused['reason']})")
        return _pick(question, ask)
    print(f"\nStep {question['step']}: {question['reason']}")
    print(f"  Command: {question['command']}" + ("  (in Docker)" if question["in_docker"] else ""))
    print(f"  Warning: {question['warning']}")
    if question["edit_error"]:
        print(f"  Your edit was blocked: {question['edit_error']}")
    while True:
        answer = ask("[a]pprove, [r]eject, [e]dit, or [q] to stop for now: ").strip().lower()
        if answer == "q":
            return None
        if answer in {"a", "r"}:
            return {"action": {"a": "approve", "r": "reject"}[answer]}
        if answer == "e":
            return {"action": "edit", "command": ask("New command: ")}


def _pick(question: dict, ask: Ask) -> dict | None:
    """Ask for one of the question's answers by its first letter, or q to pause."""
    answers = {answer[0]: answer for answer in answers_for(question)}
    words = ", ".join(f"[{key}]{answer[1:]}" for key, answer in answers.items())
    while True:
        answer = ask(f"{words}, or [q] to stop for now: ").strip().lower()
        if answer == "q":
            return None
        if answer in answers:
            return {"action": answers[answer]}


def resolve_destination(dest: str) -> str:
    return str(Path(dest).expanduser().resolve())


def _llm_or_none(settings: Settings, stores: Stores) -> LLMClient | None:
    try:
        return build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
    except LLMUnavailable as error:
        print(f"{error.message} The AI's reading, fixes and report are skipped.", file=sys.stderr)
        return None
