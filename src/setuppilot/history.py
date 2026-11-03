"""Print past runs, one run's timeline, and the metrics, for the terminal."""

from setuppilot.db.reports import metrics_summary, recent_runs
from setuppilot.db.stores import Stores


def print_runs(stores: Stores, limit: int) -> None:
    for row in recent_runs(stores.runs, stores.usage, limit):
        seconds = "-" if row.seconds is None else f"{row.seconds:.0f}s"
        run = row.run
        print(f"{run.id}  {run.status:<16} {seconds:>6} {row.tokens:>7} tokens  {run.repo_url}")


def print_run(stores: Stores, run_id: str) -> None:
    run = stores.runs.get_run(run_id)
    print(f"{run.repo_url} -> {run.dest_dir}")
    print(f"status: {run.status}   error: {run.error_type or '-'}   message: {run.message or '-'}")
    print(f"tokens: {stores.usage.tokens_for_run(run.id)}")
    for step in stores.runs.list_steps(run.id):
        mark = "ok" if step.is_ok else f"FAILED ({step.error_type})"
        timing = f"{step.duration_ms:>7} ms {step.tokens:>6} tok"
        print(f"  {step.name:<14} {timing}  {mark}  {step.result_summary}")


def print_metrics(stores: Stores, limit: int) -> None:
    summary = metrics_summary(stores.runs, stores.usage, stores.metrics, limit)
    rate = "-" if summary.success_rate is None else f"{summary.success_rate:.0%}"
    print(f"finished runs: {summary.finished_runs}   success rate: {rate}")
    print(f"average time: {_number(summary.average_seconds, 's')}")
    print(f"average tokens: {_number(summary.average_tokens, '')}")
    print(f"rate-limit hits (all time, setup and chat): {summary.rate_limit_hits}")
    print(f"switched to small model (all time, setup and chat): {summary.small_model_fallbacks}")
    for error_type, count in summary.common_errors:
        print(f"  {error_type}: {count}")


def _number(value: float | None, unit: str) -> str:
    return "-" if value is None else f"{value:.0f}{unit}"
