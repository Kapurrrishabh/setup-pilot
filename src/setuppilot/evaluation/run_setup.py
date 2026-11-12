"""Run the setup evaluation on real repos, one fresh folder each. Carries on after a stop."""

import os
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from setuppilot.agent.driver import SetupAgent
from setuppilot.db.stores import Stores
from setuppilot.evaluation.setup_eval import (
    SetupCase,
    SetupScore,
    load_setup_done,
    run_setup_case,
    save_setup_score,
    setup_table,
)
from setuppilot.guardrails.runner import HOST_TARGET
from setuppilot.tools.github_url import parse_github_url


def run_setup_eval(
    stores: Stores, agent: SetupAgent, cases: list[SetupCase], workspace: Path, results_dir: Path,
    use_docker: bool,
) -> list[SetupScore]:  # fmt: skip
    # Docker-mode scores get their own files, so neither mode carries on from the other's.
    name = "setup-docker" if use_docker else "setup"
    progress = results_dir / f"{name}.jsonl"
    done = {s.url for s in load_setup_done(progress)}
    for case in cases:
        if case.url in done:
            continue
        ref = parse_github_url(case.url)
        dest = workspace / f"{ref.owner}__{ref.name}"
        # Each repo gets an empty folder; the evaluator made it, so it may clear it too.
        if dest.exists():
            shutil.rmtree(dest, onexc=_clear_read_only)
        dest.mkdir(parents=True)
        score = run_setup_case(
            case, agent, stores.runs, stores.usage, dest, use_docker, HOST_TARGET
        )
        print(f"{case.url}: {'verified' if score.is_verified else score.error_type}")
        save_setup_score(progress, score)
    scores = load_setup_done(progress)
    table = setup_table(scores)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    heading = f"# Setup evaluation, {stamp} UTC\n\n{len(scores)} of {len(cases)} repos done."
    (results_dir / f"{name}-{stamp}.md").write_text(f"{heading}\n\n{table}\n", encoding="utf-8")
    print(table)
    return scores


def _clear_read_only(remove: Callable[[str], object], path: str, error: BaseException) -> None:
    """Windows will not delete a read-only file, and git makes its object files read-only."""
    if not isinstance(error, PermissionError) or remove not in (os.unlink, os.rmdir):
        raise error
    os.chmod(path, stat.S_IWRITE)
    remove(path)
