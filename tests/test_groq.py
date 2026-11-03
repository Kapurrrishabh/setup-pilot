import json

import httpx2
import openai
import pytest

from setuppilot.config import GroqModel
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.errors import LLMRequestRejected, LLMUnavailable
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.groq import GroqTransport, build_llm_client
from setuppilot.llm.transport import RateLimited, ServiceDown
from setuppilot.llm.usage import InMemoryUsageStore
from tests.fakes import LIMITS, make_client
from tests.test_config import REAL_CONFIG

REASONING_MODEL = GroqModel(name="openai/gpt-oss-120b", reasoning_effort="low", limits=LIMITS)
PLAIN_MODEL = GroqModel(name="plain-model", reasoning_effort=None, limits=LIMITS)
QUESTION = [{"role": "user", "content": "hi"}]


def transport_for(handler) -> GroqTransport:
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    client = openai.OpenAI(
        api_key="test", base_url="https://groq.test/openai/v1", max_retries=0, http_client=http
    )
    return GroqTransport(client)


def completion(content="hello", finish_reason="stop", prompt_tokens=9, completion_tokens=4):
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def rate_limit_response(message, retry_after="7"):
    body = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
    return httpx2.Response(429, headers={"retry-after": retry_after}, json=body)


def test_reply_has_text_and_real_token_counts():
    out = transport_for(lambda r: httpx2.Response(200, json=completion())).send(
        REASONING_MODEL, QUESTION, 50, False
    )
    assert (out.text, out.input_tokens, out.output_tokens, out.is_truncated) == (
        "hello",
        9,
        4,
        False,
    )


def test_request_sends_model_settings_and_hides_reasoning_text():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=completion())

    transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False)
    body = sent[0]
    assert (body["model"], body["max_completion_tokens"], body["temperature"]) == (
        "openai/gpt-oss-120b",
        50,
        0,
    )
    assert (body["reasoning_effort"], body["include_reasoning"]) == ("low", False)


def test_model_without_reasoning_gets_no_reasoning_settings():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=completion())

    transport_for(handler).send(PLAIN_MODEL, QUESTION, 50, False)
    assert "reasoning_effort" not in sent[0] and "include_reasoning" not in sent[0]


def test_answer_cut_off_at_max_tokens_is_flagged():
    handler = lambda r: httpx2.Response(200, json=completion(finish_reason="length"))  # noqa: E731
    assert transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False).is_truncated


def test_daily_429_is_marked_daily_with_retry_after():
    message = "Rate limit reached on requests per day (RPD): Limit 1000, Used 1000, Requested 1."
    with pytest.raises(RateLimited) as error:
        transport_for(lambda r: rate_limit_response(message)).send(
            REASONING_MODEL, QUESTION, 50, False
        )
    assert (error.value.is_daily, error.value.retry_after) == (True, 7.0)


def test_per_minute_429_is_not_marked_daily():
    message = "Rate limit reached on tokens per minute (TPM): Limit 8000, Used 7900, Requested 500."
    with pytest.raises(RateLimited) as error:
        transport_for(lambda r: rate_limit_response(message, "2.5")).send(
            REASONING_MODEL, QUESTION, 50, False
        )
    assert (error.value.is_daily, error.value.retry_after) == (False, 2.5)


def test_server_error_becomes_service_down():
    handler = lambda r: httpx2.Response(503, json={"error": {"message": "over capacity"}})  # noqa: E731
    with pytest.raises(ServiceDown):
        transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False)


def test_network_error_becomes_service_down():
    def handler(request):
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(ServiceDown):
        transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False)


def test_bad_api_key_is_rejected_with_a_hint_about_env():
    handler = lambda r: httpx2.Response(401, json={"error": {"message": "Invalid API Key"}})  # noqa: E731
    with pytest.raises(LLMRequestRejected, match="GROQ_API_KEY"):
        transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False)


def test_request_too_large_is_rejected_as_too_long():
    handler = lambda r: httpx2.Response(413, json={"error": {"message": "Request too large"}})  # noqa: E731
    with pytest.raises(LLMRequestRejected, match="too long"):
        transport_for(handler).send(REASONING_MODEL, QUESTION, 50, False)


def test_missing_api_key_says_how_to_fix_it(monkeypatch):
    from setuppilot.config import load_settings

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable) as error:
        build_llm_client(
            load_settings(REAL_CONFIG),
            InMemoryUsageStore(),
            InMemoryCache(),
            InMemoryMetricsStore(),
        )
    assert ".env" in error.value.details


def test_json_mode_asks_groq_for_a_json_object():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(content='{"ok": true}'))

    transport_for(handler).send(PLAIN_MODEL, QUESTION, 50, True)
    assert sent[0]["response_format"] == {"type": "json_object"}


def test_tracing_wraps_the_groq_client(monkeypatch):
    from setuppilot.config import load_settings
    from setuppilot.llm import groq

    wrapped = []
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real_key_123456")
    monkeypatch.setattr(groq, "tracing_is_enabled", lambda: True)
    monkeypatch.setattr(groq, "wrap_openai", lambda client: wrapped.append(client) or client)
    groq.build_llm_client(
        load_settings(REAL_CONFIG), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore()
    )
    assert len(wrapped) == 1


def test_without_tracing_the_groq_client_is_not_wrapped(monkeypatch):
    from setuppilot.config import load_settings
    from setuppilot.llm import groq

    wrapped = []
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real_key_123456")
    monkeypatch.setattr(groq, "tracing_is_enabled", lambda: False)
    monkeypatch.setattr(groq, "wrap_openai", lambda client: wrapped.append(client) or client)
    groq.build_llm_client(
        load_settings(REAL_CONFIG), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore()
    )
    assert wrapped == []


def test_bad_json_from_the_big_model_is_answered_by_the_small_model():
    def handler(request):
        if json.loads(request.content)["model"] == "big-model":
            error = {"message": "Failed to generate JSON.", "code": "json_validate_failed"}
            return httpx2.Response(400, json={"error": error})
        return httpx2.Response(200, json=completion(content='{"ok": true}'))

    result = make_client(transport_for(handler)).complete(
        QUESTION, choice="big_then_small", max_tokens=50, is_json=True, run_id=None
    )
    assert result.model == "small-model"
