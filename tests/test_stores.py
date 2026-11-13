"""The same tests run against the in-memory fakes and against real Postgres."""

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest

from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.postgres import (
    PostgresCache,
    PostgresMetricsStore,
    PostgresRunStore,
    PostgresUsageStore,
)
from setuppilot.db.runs import InMemoryRunStore, RunStatus, Step
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.transport import Reply
from setuppilot.llm.usage import InMemoryUsageStore, Usage

BACKENDS = ["memory", "postgres"]
REPO = "https://github.com/psf/requests"


@pytest.fixture(params=BACKENDS)
def stores(request):
    if request.param == "memory":
        return InMemoryRunStore(), InMemoryUsageStore(), InMemoryCache()
    pool = request.getfixturevalue("empty_pg")
    return PostgresRunStore(pool), PostgresUsageStore(pool), PostgresCache(pool)


@pytest.fixture(params=BACKENDS)
def runs_and_metrics(request):
    if request.param == "memory":
        return InMemoryRunStore(), InMemoryMetricsStore()
    pool = request.getfixturevalue("empty_pg")
    return PostgresRunStore(pool), PostgresMetricsStore(pool)


def step(run_id, name="clone", arguments=None):
    return Step(
        run_id, name, "git", arguments or {"url": REPO}, "cloned", True, None, 0, 1200, 0, None
    )


def usage(run_id, tokens, at, is_cached=False):
    return Usage(run_id, "big-model", tokens, 0, is_cached, at)


def test_new_run_reads_back_as_running(stores):
    runs, _, _ = stores
    created = runs.create_run(REPO, "/home/dev/work", None)
    got = runs.get_run(created.id)
    assert (got.repo_url, got.dest_dir, got.status, got.finished_at) == (
        REPO,
        "/home/dev/work",
        "running",
        None,
    )


def test_a_run_keeps_the_github_user_it_belongs_to(stores):
    runs, _, _ = stores
    # Past 2**31, so the column must hold any GitHub id.
    signed_in = runs.create_run(REPO, "/home/dev/work", 2**40).id
    public = runs.create_run(REPO, "/home/dev/work", None).id
    got = [runs.get_run(i) for i in (signed_in, public)]
    assert [(r.owner_github_id, r.access) for r in got] == [(2**40, "github"), (None, "public")]
    assert sorted(r.access for r in runs.list_runs(10)) == ["github", "public"]


def test_failed_run_keeps_error_and_message(stores):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.set_status(run_id, "failed", "InstallFailed", "pip install exited with 1")
    got = runs.get_run(run_id)
    assert (got.status, got.error_type, got.message) == (
        "failed",
        "InstallFailed",
        "pip install exited with 1",
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_finished_run_gets_a_finish_time(stores, status):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.set_status(run_id, status, None, None)
    assert runs.get_run(run_id).finished_at is not None


@pytest.mark.parametrize("status", ["running", "waiting_approval"])
def test_unfinished_run_has_no_finish_time(stores, status):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.set_status(run_id, status, None, None)
    assert runs.get_run(run_id).finished_at is None


@pytest.mark.parametrize("late", ["running", "waiting_approval", "succeeded", "failed"])
def test_a_cancelled_run_stays_cancelled_when_a_worker_updates_it_late(stores, late):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.set_status(run_id, "cancelled", "RunCancelled", "You cancelled this run.")
    runs.set_status(run_id, late, None, None)
    assert runs.get_run(run_id).status == "cancelled"


@pytest.mark.parametrize("status", get_args(RunStatus))
def test_every_run_status_can_be_saved(stores, status):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.set_status(run_id, status, None, None)
    assert runs.get_run(run_id).status == status


@pytest.mark.parametrize("run_id", ["00000000-0000-0000-0000-000000000000", "run-1"])
def test_unknown_run_raises_lookup_error(stores, run_id):
    runs, _, _ = stores
    with pytest.raises(LookupError, match="No run with id"):
        runs.get_run(run_id)


def test_step_for_unknown_run_raises_lookup_error(stores):
    runs, _, _ = stores
    with pytest.raises(LookupError):
        runs.add_step(step("00000000-0000-0000-0000-000000000000"))


def test_step_arguments_that_are_not_json_are_refused(stores):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    with pytest.raises(TypeError):
        runs.add_step(step(run_id, arguments={"path": object()}))


def test_deleted_run_and_its_steps_are_gone(stores):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    runs.add_step(step(run_id))
    runs.delete_run(run_id)
    with pytest.raises(LookupError):
        runs.get_run(run_id)
    assert runs.list_steps(run_id) == []


def test_status_of_unknown_run_raises_lookup_error(stores):
    runs, _, _ = stores
    with pytest.raises(LookupError, match="No run with id"):
        runs.set_status("00000000-0000-0000-0000-000000000000", "failed", None, None)


def test_steps_come_back_in_order_with_every_field(stores):
    runs, _, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    first = Step(run_id, "clone", "git", {"depth": 1}, "cloned", True, None, 0, 1200, 0, None)
    second = Step(
        run_id, "plan", "llm", {"k": [1, 2]}, "3 steps", False, "LLMUnavailable", None, 50, 420, "m"
    )
    runs.add_step(first)
    runs.add_step(second)
    assert runs.list_steps(run_id) == [first, second]


def test_tokens_are_summed_per_run(stores):
    runs, usage_store, _ = stores
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    now = datetime.now(UTC)
    usage_store.add(usage(run_id, 100, now))
    usage_store.add(usage(run_id, 50, now))
    usage_store.add(usage(None, 999, now))
    assert usage_store.tokens_for_run(run_id) == 150


def test_tokens_per_day_use_utc_days_and_include_chat(stores):
    _, usage_store, _ = stores
    midnight = datetime(2026, 11, 2, tzinfo=UTC)
    usage_store.add(usage(None, 10, midnight - timedelta(seconds=1)))
    usage_store.add(usage(None, 20, midnight))
    usage_store.add(usage(None, 30, midnight + timedelta(hours=23, minutes=59)))
    usage_store.add(usage(None, 40, midnight + timedelta(days=1)))
    assert usage_store.tokens_on_day(midnight.date()) == 50


def test_cache_miss_is_none_and_hit_returns_the_reply(stores):
    _, _, cache = stores
    assert cache.get("k") is None
    cache.put("k", Reply("answer", 10, 5, is_truncated=False))
    assert cache.get("k") == Reply("answer", 10, 5, is_truncated=False)


def test_cache_keeps_the_first_reply_for_a_key(stores):
    _, _, cache = stores
    cache.put("k", Reply("first", 1, 1, is_truncated=False))
    cache.put("k", Reply("second", 2, 2, is_truncated=False))
    assert cache.get("k").text == "first"


def test_runs_are_listed_newest_first_up_to_the_limit(stores):
    runs, _, _ = stores
    ids = [runs.create_run(REPO, f"/home/dev/{n}", None).id for n in range(3)]
    assert [r.id for r in runs.list_runs(2)] == [ids[2], ids[1]]


def test_metric_events_are_counted_by_name(runs_and_metrics):
    runs, metrics = runs_and_metrics
    run_id = runs.create_run(REPO, "/home/dev/work", None).id
    metrics.add("rate_limit_hit", run_id)
    metrics.add("rate_limit_hit", None)
    metrics.add("small_model_fallback", run_id)
    assert (metrics.count("rate_limit_hit"), metrics.count("small_model_fallback")) == (2, 1)


def test_tokens_for_many_runs_come_back_in_one_call(stores):
    runs, usage_store, _ = stores
    first = runs.create_run(REPO, "/home/dev/a", None).id
    second = runs.create_run(REPO, "/home/dev/b", None).id
    usage_store.add(usage(first, 70, datetime.now(UTC)))
    assert usage_store.tokens_for_runs([first, second]) == {first: 70, second: 0}
