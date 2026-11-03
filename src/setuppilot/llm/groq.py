"""Talk to Groq through its OpenAI-compatible API."""

import os
import random
import time

import openai
from langsmith.utils import tracing_is_enabled
from langsmith.wrappers import wrap_openai

from setuppilot.config import GroqModel, Settings
from setuppilot.db.metrics import MetricsStore
from setuppilot.errors import LLMRequestRejected, LLMUnavailable
from setuppilot.llm.cache import LLMCache
from setuppilot.llm.client import LLMClient
from setuppilot.llm.transport import Messages, RateLimited, Reply, ServiceDown
from setuppilot.llm.usage import UsageStore

_REJECTED_MESSAGES = {
    401: "Groq did not accept your API key. Check GROQ_API_KEY in .env.",
    403: "Groq did not allow this request. Check GROQ_API_KEY and the models in config.yaml.",
    404: "Groq does not know this model. Check the model names in config.yaml.",
    413: "This request is too long for the model.",
}


class GroqTransport:
    def __init__(self, client: openai.OpenAI) -> None:
        self._client = client

    def send(self, model: GroqModel, messages: Messages, max_tokens: int, is_json: bool) -> Reply:
        options: dict = {}
        if model.reasoning_effort:
            options |= {
                "reasoning_effort": model.reasoning_effort,
                "extra_body": {"include_reasoning": False},
            }
        if is_json:
            options["response_format"] = {"type": "json_object"}
        try:
            response = self._client.chat.completions.create(
                model=model.name,
                messages=messages,
                max_completion_tokens=max_tokens,
                temperature=0,
                **options,
            )
        except openai.RateLimitError as error:
            retry_after = error.response.headers.get("retry-after")
            raise RateLimited(
                error.message,
                retry_after=float(retry_after) if retry_after else None,
                is_daily="per day" in error.message.lower(),
            ) from error
        except (openai.APIConnectionError, openai.InternalServerError) as error:
            raise ServiceDown(str(error)) from error
        except openai.APIStatusError as error:
            raise LLMRequestRejected(
                _REJECTED_MESSAGES.get(error.status_code, "Groq refused the request."),
                details=f"{model.name}: HTTP {error.status_code}: {error.message}",
            ) from error
        choice = response.choices[0]
        return Reply(
            text=choice.message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            is_truncated=choice.finish_reason == "length",
        )


def is_tracing_on() -> bool:
    """Optional LangSmith tracing (LANGSMITH_TRACING=true). LangSmith's own rule, so this
    always agrees with LangGraph's tracing."""
    return bool(tracing_is_enabled())


def build_llm_client(
    settings: Settings, usage: UsageStore, cache: LLMCache, metrics: MetricsStore
) -> LLMClient:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise LLMUnavailable(
            "No Groq API key found.",
            details="GROQ_API_KEY is not set. Copy .env.example to .env and add your free key "
            "from https://console.groq.com/keys",
        )
    # Retries are ours, not the SDK's, so the rate limiter sees every try.
    openai_client = openai.OpenAI(
        api_key=api_key,
        base_url=settings.groq.base_url,
        timeout=settings.groq.timeout_seconds,
        max_retries=0,
    )
    if is_tracing_on():
        openai_client = wrap_openai(openai_client)
    return LLMClient(
        settings.groq,
        settings.budget,
        GroqTransport(openai_client),
        usage,
        cache,
        metrics,
        clock=time.time,
        sleep=time.sleep,
        random=random.random,
    )
