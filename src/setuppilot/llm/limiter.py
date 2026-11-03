"""Count requests and tokens per model, and wait before Groq would send a 429."""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from setuppilot.config import ModelLimits
from setuppilot.errors import LLMRequestRejected

MINUTE = 60.0
# Groq does not say when daily limits reset, so count the last 24 hours (the safe choice).
DAY = 86_400.0


class DailyLimitReached(Exception):
    pass


@dataclass
class Use:
    at: float
    tokens: int


class RateLimiter:
    def __init__(
        self,
        model_name: str,
        limits: ModelLimits,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._model_name = model_name
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._uses: list[Use] = []
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def acquire(self, tokens: int) -> Use:
        """Wait until a request this size fits, then count it. Settle it with the real size."""
        if tokens > self._limits.tokens_per_minute:
            raise LLMRequestRejected(
                "This request is too long for the model.",
                details=f"About {tokens} tokens is more than {self._model_name} allows per minute "
                f"({self._limits.tokens_per_minute}). Make the prompt or max_tokens smaller.",
            )
        while True:
            with self._lock:
                wait = self._seconds_to_wait(tokens)
                if wait == 0:
                    use = Use(at=self._clock(), tokens=tokens)
                    self._uses.append(use)
                    return use
            self._sleep(wait)

    def settle(self, use: Use, actual_tokens: int) -> None:
        with self._lock:
            use.tokens = actual_tokens

    def block_for(self, seconds: float) -> None:
        """Groq said this model's daily limit is reached: refuse it locally until then."""
        with self._lock:
            self._blocked_until = self._clock() + seconds

    def _seconds_to_wait(self, tokens: int) -> float:
        now = self._clock()
        if now < self._blocked_until:
            raise DailyLimitReached(
                f"{self._model_name}: Groq reported its daily limit; blocked for "
                f"{self._blocked_until - now:.0f} more seconds."
            )
        self._uses = [u for u in self._uses if u.at > now - DAY]
        limits = self._limits
        day_tokens = sum(u.tokens for u in self._uses)
        if (
            len(self._uses) >= limits.requests_per_day
            or day_tokens + tokens > limits.tokens_per_day
        ):
            raise DailyLimitReached(
                f"{self._model_name}: {len(self._uses)} requests and {day_tokens} tokens in the "
                f"last 24 hours (limits {limits.requests_per_day} and {limits.tokens_per_day})."
            )
        minute = [u for u in self._uses if u.at > now - MINUTE]
        oldest_to_expire = 0
        while (
            len(minute) - oldest_to_expire >= limits.requests_per_minute
            or sum(u.tokens for u in minute[oldest_to_expire:]) + tokens > limits.tokens_per_minute
        ):
            oldest_to_expire += 1
        if oldest_to_expire == 0:
            return 0.0
        return minute[oldest_to_expire - 1].at + MINUTE - now
