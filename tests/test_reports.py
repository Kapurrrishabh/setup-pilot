from dataclasses import replace
from datetime import UTC, datetime, timedelta

from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.reports import metrics_summary, recent_runs
from setuppilot.db.runs import InMemoryRunStore
from setuppilot.llm.usage import InMemoryUsageStore, Usage

REPO = "https://github.com/psf/requests"


class RunsWithFixedTimes(InMemoryRunStore):
    """Finished runs take exactly the seconds asked for, so averages can be checked."""

    def __init__(self):
        super().__init__()
        self.seconds = {}

    def list_runs(self, limit):
        return [
            replace(run, finished_at=run.created_at + timedelta(seconds=self.seconds[run.id]))
            if run.finished_at and run.id in self.seconds
            else run
            for run in super().list_runs(limit)
        ]


def finished_run(runs, usage, status, tokens, seconds, error_type=None):
    run = runs.create_run(REPO, "/home/dev/work", None)
    runs.set_status(run.id, status, error_type, None)
    runs.seconds[run.id] = seconds
    usage.add(Usage(run.id, "big", tokens, 0, False, datetime.now(UTC)))
    return run


def test_recent_runs_show_tokens_and_time():
    runs, usage = RunsWithFixedTimes(), InMemoryUsageStore()
    finished_run(runs, usage, "succeeded", 300, 40)
    (row,) = recent_runs(runs, usage, 10)
    assert (row.tokens, row.seconds) == (300, 40)


def test_unfinished_run_has_no_time_yet():
    runs, usage = InMemoryRunStore(), InMemoryUsageStore()
    runs.create_run(REPO, "/home/dev/work", None)
    assert recent_runs(runs, usage, 10)[0].seconds is None


def test_summary_counts_only_finished_runs():
    runs, usage, metrics = RunsWithFixedTimes(), InMemoryUsageStore(), InMemoryMetricsStore()
    finished_run(runs, usage, "succeeded", 100, 10)
    finished_run(runs, usage, "failed", 300, 30, "InstallFailed")
    finished_run(runs, usage, "failed", 200, 20, "InstallFailed")
    runs.create_run(REPO, "/home/dev/still-running", None)
    metrics.add("rate_limit_hit", None)
    summary = metrics_summary(runs, usage, metrics, 10)
    assert (summary.finished_runs, summary.success_rate) == (3, 1 / 3)
    assert (summary.average_seconds, summary.average_tokens) == (20, 200)
    assert summary.common_errors == [("InstallFailed", 2)]
    assert summary.rate_limit_hits == 1


def test_summary_with_no_runs_has_no_averages():
    summary = metrics_summary(InMemoryRunStore(), InMemoryUsageStore(), InMemoryMetricsStore(), 10)
    assert (summary.finished_runs, summary.success_rate, summary.average_tokens) == (0, None, None)
