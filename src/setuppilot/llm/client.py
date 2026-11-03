"""One way to ask the LLM: cache, budget, rate limits, retries and the small-model fallback."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from setuppilot.config import BudgetConfig, GroqConfig
from setuppilot.db.metrics import MetricName, MetricsStore
from setuppilot.errors import DatabaseUnavailable, LLMUnavailable
from setuppilot.llm.cache import LLMCache, cache_key
from setuppilot.llm.limiter import MINUTE, DailyLimitReached, RateLimiter
from setuppilot.llm.transport import ChatTransport, Messages, RateLimited, Reply, ServiceDown
from setuppilot.llm.usage import Usage, UsageStore, check_budget

log = logging.getLogger(__name__)

# "big_then_small": use BIG, and switch to SMALL if BIG hits a limit. Only for tasks SMALL can do.
ModelChoice = Literal["big", "big_then_small", "small"]
_ROLES: dict[ModelChoice, tuple[str, ...]] = {
    "big": ("big",),
    "big_then_small": ("big", "small"),
    "small": ("small",),
}
DAILY_LIMIT_MESSAGE = "Daily limit reached, try again later."
BUSY_MESSAGE = "Groq is busy or not answering. Try again in a few minutes."


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    is_cached: bool
    is_truncated: bool


def estimate_tokens(messages: Messages, max_tokens: int) -> int:
    # PROVISIONAL: about 3 characters per token for English plus code. Groq reports real counts.
    return len(json.dumps(messages, ensure_ascii=False)) // 3 + max_tokens


def backoff_delay(attempt: int, base: float, cap: float, random: Callable[[], float]) -> float:
    # Random wait up to the exponential step ("full jitter"), so retries do not bunch up.
    return random() * min(cap, base * 2**attempt)


class LLMClient:
    def __init__(
        self,
        config: GroqConfig,
        budget: BudgetConfig,
        transport: ChatTransport,
        usage: UsageStore,
        cache: LLMCache,
        metrics: MetricsStore,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        random: Callable[[], float],
    ) -> None:
        self._config = config
        self._budget = budget
        self._transport = transport
        self._usage = usage
        self._cache = cache
        self._metrics = metrics
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._models = {"big": config.big, "small": config.small}
        self._limiters = {
            role: RateLimiter(model.name, model.limits, clock, sleep)
            for role, model in self._models.items()
        }

    def complete(
        self,
        messages: Messages,
        *,
        choice: ModelChoice,
        max_tokens: int,
        is_json: bool,
        run_id: str | None,
    ) -> LLMResult:
        roles = _ROLES[choice]
        # Check every allowed model's cache first, so a replay never pays for a new answer.
        for role in roles:
            model = self._models[role]
            key = cache_key(model, messages, max_tokens, is_json)
            if (cached := self._cache.get(key)) is not None:
                return self._finish(model.name, cached, run_id, is_cached=True, attempt=0)
        *first_roles, last_role = roles
        for role in first_roles:
            try:
                return self._complete_with(role, messages, max_tokens, is_json, run_id)
            except LLMUnavailable as error:
                log.warning("switching to the small model", extra={"reason": error.details})
                self._count("small_model_fallback", run_id)
        return self._complete_with(last_role, messages, max_tokens, is_json, run_id)

    def _complete_with(
        self, role: str, messages: Messages, max_tokens: int, is_json: bool, run_id: str | None
    ) -> LLMResult:
        model = self._models[role]
        estimate = estimate_tokens(messages, max_tokens)
        now = datetime.fromtimestamp(self._clock(), UTC)
        run_tokens = self._usage.tokens_for_run(run_id) if run_id else 0
        check_budget(self._budget, run_tokens, self._usage.tokens_on_day(now.date()), estimate)

        limiter = self._limiters[role]
        last_error = ""
        for attempt in range(self._config.max_attempts):
            try:
                use = limiter.acquire(estimate)
            except DailyLimitReached as error:
                raise LLMUnavailable(DAILY_LIMIT_MESSAGE, details=str(error)) from error
            try:
                reply = self._transport.send(model, messages, max_tokens, is_json)
            except RateLimited as error:
                log.warning(
                    "rate limit hit", extra={"model": model.name, "is_daily": error.is_daily}
                )
                self._count("rate_limit_hit", run_id)
                # A wait longer than a minute can only be a daily limit, whatever the message says.
                if error.is_daily or (error.retry_after or 0) > MINUTE:
                    if error.retry_after:
                        limiter.block_for(error.retry_after)
                    details = f"{model.name}: {error}"
                    raise LLMUnavailable(DAILY_LIMIT_MESSAGE, details=details) from error
                last_error = str(error)
                wait = max(error.retry_after or 0, self._backoff(attempt))
            except ServiceDown as error:
                last_error = str(error)
                wait = self._backoff(attempt)
            else:
                limiter.settle(use, reply.input_tokens + reply.output_tokens)
                result = self._finish(model.name, reply, run_id, is_cached=False, attempt=attempt)
                if reply.text and not reply.is_truncated:
                    try:
                        self._cache.put(cache_key(model, messages, max_tokens, is_json), reply)
                    except DatabaseUnavailable as error:
                        log.warning(
                            "could not save llm answer to cache", extra={"reason": error.details}
                        )
                return result
            # A failed try spends no tokens, but it still counts as a request.
            limiter.settle(use, 0)
            if attempt + 1 < self._config.max_attempts:
                self._sleep(wait)
        raise LLMUnavailable(
            BUSY_MESSAGE,
            details=f"{model.name}: gave up after {self._config.max_attempts} tries. "
            f"Last error: {last_error}",
        )

    def _count(self, name: MetricName, run_id: str | None) -> None:
        try:
            self._metrics.add(name, run_id)
        except DatabaseUnavailable as error:
            log.warning("could not save metric", extra={"metric": name, "reason": error.details})

    def _backoff(self, attempt: int) -> float:
        config = self._config
        return backoff_delay(
            attempt, config.backoff_base_seconds, config.backoff_max_seconds, self._random
        )

    def _finish(
        self, model: str, reply: Reply, run_id: str | None, is_cached: bool, attempt: int
    ) -> LLMResult:
        # A cache hit spends no tokens, so it is saved and returned as zero.
        spent_in, spent_out = (0, 0) if is_cached else (reply.input_tokens, reply.output_tokens)
        at = datetime.fromtimestamp(self._clock(), UTC)
        try:
            self._usage.add(Usage(run_id, model, spent_in, spent_out, is_cached, at))
        except DatabaseUnavailable as error:
            # The answer is already paid for, so keep it even if saving the record fails.
            log.warning("could not save llm usage", extra={"reason": error.details})
        log.info(
            "llm call",
            extra={
                "model": model,
                "input_tokens": spent_in,
                "output_tokens": spent_out,
                "is_cached": is_cached,
                "attempt": attempt,
            },
        )
        if reply.is_truncated:
            log.warning("llm answer was cut off at max_tokens", extra={"model": model})
        return LLMResult(reply.text, model, spent_in, spent_out, is_cached, reply.is_truncated)
