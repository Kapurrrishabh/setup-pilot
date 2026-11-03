import pytest

from setuppilot.config import ModelLimits
from setuppilot.errors import LLMRequestRejected
from setuppilot.llm.limiter import DAY, MINUTE, DailyLimitReached, RateLimiter
from tests.fakes import FakeClock


def limiter(clock, rpm=30, rpd=1000, tpm=8000, tpd=200000):
    limits = ModelLimits(
        requests_per_minute=rpm, requests_per_day=rpd, tokens_per_minute=tpm, tokens_per_day=tpd
    )
    return RateLimiter("m", limits, clock, clock.sleep)


def test_requests_under_the_limits_do_not_wait():
    clock = FakeClock()
    rl = limiter(clock)
    for _ in range(5):
        rl.acquire(100)
    assert clock.sleeps == []


def test_requests_per_minute_limit_waits_for_the_oldest_to_expire():
    clock = FakeClock()
    rl = limiter(clock, rpm=2)
    rl.acquire(10)
    clock.now += 10
    rl.acquire(10)
    rl.acquire(10)
    assert clock.sleeps == [MINUTE - 10]


def test_tokens_per_minute_limit_waits_until_enough_tokens_expire():
    clock = FakeClock()
    rl = limiter(clock, tpm=1000)
    rl.acquire(600)
    clock.now += 5
    rl.acquire(300)
    rl.acquire(500)
    assert clock.sleeps == [MINUTE - 5]


def test_settle_frees_tokens_that_were_reserved_but_not_used():
    clock = FakeClock()
    rl = limiter(clock, tpm=1000)
    use = rl.acquire(900)
    rl.settle(use, 100)
    rl.acquire(800)
    assert clock.sleeps == []


def test_requests_per_day_limit_raises_instead_of_waiting():
    clock = FakeClock()
    rl = limiter(clock, rpd=2)
    rl.acquire(10)
    rl.acquire(10)
    with pytest.raises(DailyLimitReached, match="2 requests"):
        rl.acquire(10)


def test_tokens_per_day_limit_raises_instead_of_waiting():
    clock = FakeClock()
    rl = limiter(clock, tpm=1000, tpd=1500)
    rl.acquire(1000)
    clock.now += MINUTE
    with pytest.raises(DailyLimitReached, match="1000 tokens"):
        rl.acquire(600)


def test_day_window_rolls_after_24_hours():
    clock = FakeClock()
    rl = limiter(clock, rpd=1)
    rl.acquire(10)
    clock.now += DAY
    rl.acquire(10)


def test_request_bigger_than_tokens_per_minute_is_rejected():
    with pytest.raises(LLMRequestRejected):
        limiter(FakeClock(), tpm=100).acquire(101)


def test_block_for_refuses_the_model_until_the_time_is_up():
    clock = FakeClock()
    rl = limiter(clock)
    rl.block_for(100)
    with pytest.raises(DailyLimitReached, match="Groq reported"):
        rl.acquire(10)
    clock.now += 100
    rl.acquire(10)
