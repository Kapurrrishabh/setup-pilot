"""Cache answers by a hash of model + prompt, so repeated runs cost nothing."""

import hashlib
import json
from typing import Protocol

from setuppilot.config import GroqModel
from setuppilot.llm.transport import Messages, Reply


def cache_key(model: GroqModel, messages: Messages, max_tokens: int, is_json: bool) -> str:
    parts = [model.name, model.reasoning_effort, messages, max_tokens, is_json]
    payload = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class LLMCache(Protocol):
    def get(self, key: str) -> Reply | None: ...
    def put(self, key: str, reply: Reply) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._replies: dict[str, Reply] = {}

    def get(self, key: str) -> Reply | None:
        return self._replies.get(key)

    def put(self, key: str, reply: Reply) -> None:
        self._replies.setdefault(key, reply)
