"""What the Runs and Metrics views show, worked out from the stores (database or memory)."""

from collections import Counter
from dataclasses import dataclass

from setuppilot.db.metrics import MetricsStore
from setuppilot.db.runs import FINISHED_STATUSES, Run, RunStore
from setuppilot.llm.usage import UsageStore

# How many recent runs the Runs and Metrics views look at; plenty for one person's history.
HISTORY_LIMIT = 200


@dataclass(frozen=True)
class RunRow:
    run: Run
    tokens: int
    seconds: float | None


@dataclass(frozen=True)
class MetricsSummary:
    finished_runs: int
    success_rate: float | None
    average_seconds: float | None
    average_tokens: float | None
    rate_limit_hits: int
    small_model_fallbacks: int
    common_errors: list[tuple[str, int]]


def recent_runs(runs: RunStore, usage: UsageStore, limit: int) -> list[RunRow]:
    found = runs.list_runs(limit)
    tokens = usage.tokens_for_runs([run.id for run in found])
    rows = []
    for run in found:
        seconds = (run.finished_at - run.created_at).total_seconds() if run.finished_at else None
        rows.append(RunRow(run, tokens[run.id], seconds))
    return rows


def metrics_summary(
    runs: RunStore, usage: UsageStore, metrics: MetricsStore, limit: int
) -> MetricsSummary:
    """Success, averages and errors use the finished runs among the last `limit`. Rate-limit
    hits and small-model switches are all-time counts, chat questions included."""
    finished = [r for r in recent_runs(runs, usage, limit) if r.run.status in FINISHED_STATUSES]

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    succeeded = sum(r.run.status == "succeeded" for r in finished)
    errors = Counter(r.run.error_type for r in finished if r.run.error_type)
    return MetricsSummary(
        finished_runs=len(finished),
        success_rate=succeeded / len(finished) if finished else None,
        average_seconds=average([r.seconds for r in finished if r.seconds is not None]),
        average_tokens=average([r.tokens for r in finished]),
        rate_limit_hits=metrics.count("rate_limit_hit"),
        small_model_fallbacks=metrics.count("small_model_fallback"),
        common_errors=errors.most_common(5),
    )
