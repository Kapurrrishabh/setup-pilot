"""Counts of events that runs and steps do not show, like Groq rate-limit hits."""

from typing import Literal, Protocol

MetricName = Literal["rate_limit_hit", "small_model_fallback"]


class MetricsStore(Protocol):
    def add(self, name: MetricName, run_id: str | None) -> None: ...
    def count(self, name: MetricName) -> int: ...


class InMemoryMetricsStore:
    def __init__(self) -> None:
        self._events: list[tuple[MetricName, str | None]] = []

    def add(self, name: MetricName, run_id: str | None) -> None:
        self._events.append((name, run_id))

    def count(self, name: MetricName) -> int:
        return sum(1 for event, _ in self._events if event == name)
