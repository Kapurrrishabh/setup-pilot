"""What the LLM client needs from a chat service. Groq and the test fake both provide it."""

from dataclasses import dataclass
from typing import Protocol

from setuppilot.config import GroqModel

Messages = list[dict[str, str]]


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    is_truncated: bool


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float | None, is_daily: bool) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.is_daily = is_daily


class ServiceDown(Exception):
    """A network error, timeout or server error. Worth trying again."""


class ChatTransport(Protocol):
    def send(
        self, model: GroqModel, messages: Messages, max_tokens: int, is_json: bool
    ) -> Reply: ...
