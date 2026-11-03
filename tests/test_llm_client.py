import pytest

from setuppilot.config import BudgetConfig
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.errors import (
    BudgetExceeded,
    DatabaseUnavailable,
    LLMRequestRejected,
    LLMUnavailable,
)
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.client import DAILY_LIMIT_MESSAGE, backoff_delay
from setuppilot.llm.transport import RateLimited, ServiceDown
from setuppilot.llm.usage import InMemoryUsageStore
from tests.fakes import FakeClock, FakeTransport, groq_config, make_client, reply

QUESTION = [{"role": "user", "content": "What does this repo do?"}]


def ask(client, choice="big", run_id="run-1", messages=QUESTION, is_json=False):
    return client.complete(messages, choice=choice, max_tokens=100, is_json=is_json, run_id=run_id)


def per_minute_429(retry_after=7.0):
    return RateLimited("tokens per minute (TPM)", retry_after=retry_after, is_daily=False)


def per_day_429():
    return RateLimited("requests per day (RPD)", retry_after=3600.0, is_daily=True)


def test_answer_and_token_counts_are_returned_and_saved():
    usage = InMemoryUsageStore()
    result = ask(make_client(FakeTransport(reply("hi", 12, 3)), usage=usage))
    assert (result.text, result.input_tokens, result.output_tokens) == ("hi", 12, 3)
    assert usage.tokens_for_run("run-1") == 15


def test_same_prompt_twice_is_served_from_cache_without_a_call():
    transport = FakeTransport(reply("hi"))
    client = make_client(transport)
    ask(client)
    second = ask(client)
    assert (second.is_cached, second.text, second.input_tokens) == (True, "hi", 0)
    assert transport.calls == ["big-model"]


def test_cache_hit_spends_no_tokens():
    usage = InMemoryUsageStore()
    client = make_client(FakeTransport(reply("hi", 12, 3)), usage=usage)
    ask(client)
    ask(client)
    assert usage.tokens_for_run("run-1") == 15


def test_different_prompt_is_not_a_cache_hit():
    transport = FakeTransport(reply("a"), reply("b"))
    client = make_client(transport)
    ask(client)
    assert ask(client, messages=[{"role": "user", "content": "other"}]).text == "b"


def test_big_and_small_answers_are_cached_separately():
    transport = FakeTransport(reply("big"), reply("small"))
    client = make_client(transport)
    ask(client, choice="big")
    ask(client, choice="small")
    assert transport.calls == ["big-model", "small-model"]


def test_cached_small_answer_is_used_before_calling_big():
    transport = FakeTransport(reply("small"))
    client = make_client(transport)
    ask(client, choice="small")
    result = ask(client, choice="big_then_small")
    assert (result.model, result.is_cached, transport.calls) == (
        "small-model",
        True,
        ["small-model"],
    )


def test_empty_answer_is_not_cached():
    transport = FakeTransport(reply(""), reply("full"))
    client = make_client(transport)
    ask(client)
    assert ask(client).text == "full"


def test_cut_off_answer_is_not_cached():
    transport = FakeTransport(reply("par", is_truncated=True), reply("full"))
    client = make_client(transport)
    ask(client)
    assert ask(client).text == "full"


def test_per_minute_429_waits_for_retry_after_then_succeeds():
    clock = FakeClock()
    transport = FakeTransport(per_minute_429(retry_after=7.0), reply("ok"))
    assert ask(make_client(transport, clock=clock)).text == "ok"
    assert clock.sleeps == [7.0]


def test_429_without_retry_after_uses_backoff():
    clock = FakeClock()
    transport = FakeTransport(per_minute_429(retry_after=None), reply("ok"))
    ask(make_client(transport, clock=clock))
    assert clock.sleeps == [1.0]


def test_per_minute_429_with_long_wait_under_a_minute_is_still_retried():
    clock = FakeClock()
    transport = FakeTransport(per_minute_429(retry_after=45.0), reply("ok"))
    assert ask(make_client(transport, clock=clock), choice="big").text == "ok"
    assert clock.sleeps == [45.0]


def test_failed_try_releases_its_reserved_tokens():
    limits = groq_config().big.limits.model_copy(update={"tokens_per_minute": 200})
    config = groq_config(big=groq_config().big.model_copy(update={"limits": limits}))
    clock = FakeClock()
    transport = FakeTransport(ServiceDown("502"), reply("ok"))
    ask(make_client(transport, config=config, clock=clock))
    assert clock.sleeps == [1.0]


def test_refused_request_does_not_switch_to_small():
    transport = FakeTransport(LLMRequestRejected("bad key", details="401"))
    with pytest.raises(LLMRequestRejected):
        ask(make_client(transport), choice="big_then_small")
    assert transport.calls == ["big-model"]


def test_daily_429_blocks_big_so_the_next_call_goes_straight_to_small():
    transport = FakeTransport(per_day_429(), reply("a"), reply("b"))
    client = make_client(transport)
    ask(client, choice="big_then_small")
    ask(client, choice="big_then_small", messages=[{"role": "user", "content": "again"}])
    assert transport.calls == ["big-model", "small-model", "small-model"]


def test_daily_429_on_big_switches_to_small_when_allowed():
    transport = FakeTransport(per_day_429(), reply("from small"))
    result = ask(make_client(transport), choice="big_then_small")
    assert (result.model, transport.calls) == ("small-model", ["big-model", "small-model"])


def test_daily_429_on_big_stops_with_friendly_message_when_small_not_allowed():
    with pytest.raises(LLMUnavailable) as error:
        ask(make_client(FakeTransport(per_day_429())), choice="big")
    assert error.value.message == DAILY_LIMIT_MESSAGE


def test_retry_after_longer_than_backoff_max_gives_up_without_sleeping():
    clock = FakeClock()
    transport = FakeTransport(per_minute_429(retry_after=120.0), reply("from small"))
    result = ask(make_client(transport, clock=clock), choice="big_then_small")
    assert (result.model, clock.sleeps) == ("small-model", [])


def test_service_down_is_retried_with_backoff_then_gives_up():
    clock = FakeClock()
    transport = FakeTransport(*[ServiceDown("502")] * 3)
    with pytest.raises(LLMUnavailable, match="busy"):
        ask(make_client(transport, clock=clock))
    assert len(transport.calls) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_run_budget_stops_before_calling_the_llm():
    transport = FakeTransport(reply("a", 400, 100))
    client = make_client(transport, budget=BudgetConfig(tokens_per_run=550, tokens_per_day=10_000))
    ask(client)
    with pytest.raises(BudgetExceeded, match="This run"):
        ask(client, messages=[{"role": "user", "content": "next"}])
    assert len(transport.calls) == 1


def test_day_budget_counts_all_runs_and_chat():
    transport = FakeTransport(reply("a", 400, 250))
    client = make_client(transport, budget=BudgetConfig(tokens_per_run=700, tokens_per_day=700))
    ask(client, run_id="run-1")
    with pytest.raises(BudgetExceeded, match="Today's"):
        ask(client, run_id=None, messages=[{"role": "user", "content": "chat"}])


def test_local_daily_limit_switches_to_small_without_calling_big():
    limits = groq_config().big.limits.model_copy(update={"requests_per_day": 1})
    config = groq_config(big=groq_config().big.model_copy(update={"limits": limits}))
    transport = FakeTransport(reply("a"), reply("b"))
    client = make_client(transport, config=config)
    ask(client, choice="big_then_small")
    ask(client, choice="big_then_small", messages=[{"role": "user", "content": "again"}])
    assert transport.calls == ["big-model", "small-model"]


def test_backoff_grows_exponentially_and_is_capped():
    delays = [backoff_delay(n, base=1, cap=10, random=lambda: 1.0) for n in range(6)]
    assert delays == [1, 2, 4, 8, 10, 10]


def test_backoff_jitter_scales_the_wait_down():
    assert backoff_delay(3, base=1, cap=10, random=lambda: 0.25) == 2


class UsageStoreThatLosesTheDatabaseOnWrite(InMemoryUsageStore):
    def add(self, usage):
        raise DatabaseUnavailable("Lost the connection to the database.", details="test")


class CacheThatLosesTheDatabaseOnWrite(InMemoryCache):
    def put(self, key, reply):
        raise DatabaseUnavailable("Lost the connection to the database.", details="test")


def test_paid_answer_is_kept_when_saving_usage_fails():
    usage = UsageStoreThatLosesTheDatabaseOnWrite()
    assert ask(make_client(FakeTransport(reply("paid")), usage=usage)).text == "paid"


def test_paid_answer_is_kept_when_saving_to_cache_fails():
    cache = CacheThatLosesTheDatabaseOnWrite()
    assert ask(make_client(FakeTransport(reply("paid")), cache=cache)).text == "paid"


def test_json_and_text_answers_to_the_same_prompt_are_cached_separately():
    transport = FakeTransport(reply("text"), reply('{"a": 1}'))
    client = make_client(transport)
    ask(client)
    assert ask(client, is_json=True).text == '{"a": 1}'


def test_rate_limit_hits_and_small_model_switches_are_counted():
    metrics = InMemoryMetricsStore()
    transport = FakeTransport(per_minute_429(1.0), per_day_429(), reply("small"))
    ask(make_client(transport, metrics=metrics), choice="big_then_small")
    assert (metrics.count("rate_limit_hit"), metrics.count("small_model_fallback")) == (2, 1)


class MetricsThatLoseTheDatabase(InMemoryMetricsStore):
    def add(self, name, run_id):
        raise DatabaseUnavailable("Lost the connection to the database.", details="test")


def test_answer_is_kept_when_saving_a_metric_fails():
    transport = FakeTransport(per_minute_429(1.0), reply("ok"))
    client = make_client(transport, metrics=MetricsThatLoseTheDatabase())
    assert ask(client).text == "ok"
