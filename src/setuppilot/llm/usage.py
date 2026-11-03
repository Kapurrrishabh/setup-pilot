"""Token usage records, and the per-run and per-day budget check."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from setuppilot.config import BudgetConfig
from setuppilot.errors import BudgetExceeded


@dataclass(frozen=True)
class Usage:
    run_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    is_cached: bool
    at: datetime


class UsageStore(Protocol):
    def add(self, usage: Usage) -> None: ...
    def tokens_for_run(self, run_id: str) -> int: ...
    def tokens_for_runs(self, run_ids: list[str]) -> dict[str, int]: ...
    def tokens_on_day(self, day: date) -> int: ...
    def calls_on_day_by_model(self, day: date) -> dict[str, int]: ...


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._rows: list[Usage] = []

    def add(self, usage: Usage) -> None:
        self._rows.append(usage)

    def tokens_for_run(self, run_id: str) -> int:
        return sum(u.input_tokens + u.output_tokens for u in self._rows if u.run_id == run_id)

    def tokens_for_runs(self, run_ids: list[str]) -> dict[str, int]:
        return {run_id: self.tokens_for_run(run_id) for run_id in run_ids}

    def tokens_on_day(self, day: date) -> int:
        return sum(u.input_tokens + u.output_tokens for u in self._rows if u.at.date() == day)

    def calls_on_day_by_model(self, day: date) -> dict[str, int]:
        """Live calls only: cache hits do not count against Groq's limits."""
        calls: dict[str, int] = {}
        for u in self._rows:
            if u.at.date() == day and not u.is_cached:
                calls[u.model] = calls.get(u.model, 0) + 1
        return calls


def check_budget(budget: BudgetConfig, run_tokens: int, day_tokens: int, next_tokens: int) -> None:
    if day_tokens + next_tokens > budget.tokens_per_day:
        raise BudgetExceeded(
            "Today's token budget is used up. Try again tomorrow.",
            details=f"Used {day_tokens} of {budget.tokens_per_day} tokens today; the next call "
            f"needs about {next_tokens}. The limit is budget.tokens_per_day in config.yaml.",
        )
    if run_tokens + next_tokens > budget.tokens_per_run:
        raise BudgetExceeded(
            "This run has used its token budget, so it stopped here.",
            details=f"Used {run_tokens} of {budget.tokens_per_run} tokens in this run; the next "
            f"call needs about {next_tokens}. The limit is budget.tokens_per_run in config.yaml.",
        )
