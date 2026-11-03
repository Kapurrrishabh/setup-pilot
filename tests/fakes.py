"""Fakes shared by tests: a clock that moves only when slept, and a scripted LLM."""

from setuppilot.config import BudgetConfig, GroqConfig, GroqModel, ModelLimits
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.client import LLMClient
from setuppilot.llm.transport import Messages, Reply
from setuppilot.llm.usage import InMemoryUsageStore

LIMITS = ModelLimits(
    requests_per_minute=30, requests_per_day=1000, tokens_per_minute=8000, tokens_per_day=200000
)
BUDGET = BudgetConfig(tokens_per_run=30000, tokens_per_day=300000)


def groq_config(**changes) -> GroqConfig:
    values = {
        "base_url": "https://groq.test/openai/v1",
        "timeout_seconds": 5,
        "max_attempts": 3,
        "backoff_base_seconds": 1,
        "backoff_max_seconds": 30,
        "big": GroqModel(name="big-model", reasoning_effort="low", limits=LIMITS),
        "small": GroqModel(name="small-model", reasoning_effort=None, limits=LIMITS),
    }
    return GroqConfig(**(values | changes))


def reply(text="ok", input_tokens=10, output_tokens=5, is_truncated=False) -> Reply:
    return Reply(text, input_tokens, output_tokens, is_truncated)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Plays back scripted replies or errors, one per call, and records which model was called."""

    def __init__(self, *steps: Reply | Exception) -> None:
        self._steps = list(steps)
        self.calls: list[str] = []
        self.requests: list[dict] = []

    def send(self, model: GroqModel, messages: Messages, max_tokens: int, is_json: bool) -> Reply:
        self.calls.append(model.name)
        self.requests.append({"messages": messages, "max_tokens": max_tokens, "is_json": is_json})
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def make_client(
    transport, config=None, budget=BUDGET, usage=None, cache=None, clock=None, metrics=None
):
    clock = clock or FakeClock()
    return LLMClient(
        config or groq_config(),
        budget,
        transport,
        usage if usage is not None else InMemoryUsageStore(),
        cache if cache is not None else InMemoryCache(),
        metrics if metrics is not None else InMemoryMetricsStore(),
        clock=clock,
        sleep=clock.sleep,
        random=lambda: 1.0,
    )


def make_repo(root, files: dict[str, str]):
    """Write a fake repo: {relative path: text}. Returns the root folder."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def fake_executable(folder, name: str, script: str):
    """A tiny shell script on disk, used as a stand-in for a real tool on PATH."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)
    return path


class FakeEmbedder:
    """Bag-of-words vectors: texts that share words come out close. Fast, no model download."""

    dimensions = 384

    def embed(self, texts):
        import math
        import re
        import zlib

        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                vector[zlib.crc32(word.encode()) % self.dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors
