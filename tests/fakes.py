"""Fakes shared by tests: a clock that moves only when slept, and a scripted LLM."""

import sys
from collections.abc import Callable

from setuppilot.config import BudgetConfig, GroqConfig, GroqModel, ModelLimits
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.client import LLMClient
from setuppilot.llm.transport import Messages, Reply, ResponseFormat
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
    """Plays back scripted replies or errors, one per call, and records which model was called.
    A step can also be a function of the messages, for a reply that depends on the prompt."""

    def __init__(self, *steps: Reply | Exception | Callable[[Messages], Reply]) -> None:
        self._steps = list(steps)
        self.calls: list[str] = []
        self.requests: list[dict] = []

    def send(
        self, model: GroqModel, messages: Messages, max_tokens: int, response_format: ResponseFormat
    ) -> Reply:
        self.calls.append(model.name)
        self.requests.append(
            {"messages": messages, "max_tokens": max_tokens, "response_format": response_format}
        )
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def reply_citing(path: str, text: str = "An answer [1].", found: bool = True):
    """A fake LLM step: the chatbot's JSON answer, citing the source the prompt labels `path`.
    Its number depends on search order, so it is read from the prompt."""
    import json
    import re

    def reply_for(messages: Messages) -> Reply:
        prompt = messages[1]["content"]
        number = re.search(rf"^\[(\d+)\] {re.escape(path)}[:\s]", prompt, re.MULTILINE)
        if number is None:
            raise AssertionError(f"{path} is not among the sources:\n{prompt}")
        return reply(json.dumps({"answer": text, "found": found, "sources": [int(number[1])]}))

    return reply_for


def ai_step(command, phase="install", is_manual=False, reason="the README says so") -> dict:
    return {"phase": phase, "command": command, "reason": reason, "is_manual": is_manual}


def ai_project(path=".", manager="pip", steps=(), **changes) -> dict:
    """One project in the AI's analysis, in the strict schema's shape."""
    project = {
        "path": path, "name": "demo", "role": "app", "languages": ["python"], "frameworks": [],
        "package_manager": manager, "runtime": None, "tools": [], "env": [], "services": [],
        "steps": list(steps), "start_command": None, "evidence": ["requirements.txt"],
    }  # fmt: skip
    return project | changes


def analysis_reply(*projects, notes=(), summary="A small demo repo.") -> Reply:
    """The AI's answer to the analyze call."""
    import json

    body = {"summary": summary, "projects": list(projects), "notes": list(notes)}
    return reply(json.dumps(body), input_tokens=900, output_tokens=300)


def diagnosis_reply(
    commands=(), category="dependency_conflict", missing_tools=(), user_action=None,
    explanation="The lock file is older than package.json.",
) -> Reply:  # fmt: skip
    """The AI's answer to the diagnose call. commands: (command, reason) pairs."""
    import json

    body = {
        "category": category,
        "explanation": explanation,
        "needs_user": user_action is not None,
        "user_action": user_action,
        "missing_tools": list(missing_tools),
        "commands": [{"command": c, "reason": r} for c, r in commands],
    }
    return reply(json.dumps(body), input_tokens=700, output_tokens=200)


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
        path.write_text(text, encoding="utf-8")
    return root


def fake_executable(folder, name: str, code: str):
    """A stand-in for a real tool on PATH: a little Python program, started the way the system
    starts programs (a #! line on macOS and Linux, a .cmd file on Windows)."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (folder / f"{name}.py").write_text(code, encoding="utf-8")
        path = folder / f"{name}.cmd"
        path.write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\n', encoding="utf-8")
        return path
    path = folder / name
    path.write_text(f"#!{sys.executable}\n{code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeEmbedder:
    """Bag-of-words vectors: texts that share words come out close. Fast, no model download."""

    model_name = "fake-bag-of-words"
    dimensions = 384
    # The same window as all-MiniLM-L6-v2 (256 tokens, less its 2 special ones).
    max_tokens = 254

    def count_tokens(self, text):
        """Words and punctuation marks, close to how a real tokenizer counts."""
        import re

        return len(re.findall(r"\w+|[^\w\s]", text))

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


# A made-up commit id for repos indexed in tests.
FAKE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def make_chat_clone(repos_dir, key: str, files: dict[str, str]):
    """A finished chatbot clone of owner/name under repos_dir: a real git repo with one commit,
    so its commit id can be read."""
    import subprocess

    folder = make_repo(repos_dir / key, files)
    git = ["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "files"], check=True)
    (folder / ".git/setuppilot-clone-done").touch()
    return folder
