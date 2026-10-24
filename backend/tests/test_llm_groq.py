"""Phase 3: the Groq adapter. Error mapping uses the real groq SDK exception classes, and the
end-to-end tests drive the real ChatGroq client against a mocked HTTP transport (no network)."""

import json
from typing import Any

import groq
import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.contracts.agents import TriageOutput
from app.contracts.enums import Intent, ProviderName
from app.llm import get_provider
from app.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMInvalidOutputError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.groq_provider import GroqProvider
from app.llm.provider import ModelRole
from app.llm.retry import RetryPolicy
from tests.llm_helpers import GROQ_URL, Sleeps, groq_status_error

GOOD_TRIAGE = json.dumps({"intent": "order_status", "order_id": "ORD-1001", "confidence": 0.9})
POLICY = RetryPolicy(max_retries=2, timeout_seconds=5.0, max_wait_seconds=10.0, base_delay_seconds=1.0)


def bare_provider() -> GroqProvider:
    return GroqProvider(
        api_key="k", models={ModelRole.TRIAGE: "small", ModelRole.AGENT: "big"}, policy=POLICY
    )


# ---- error classification ---------------------------------------------------------------------

REQUEST = httpx.Request("POST", GROQ_URL)


@pytest.mark.parametrize(
    ("exc", "expected", "retryable", "retry_after"),
    [
        (
            groq_status_error(groq.RateLimitError, 429, headers={"retry-after": "7"}),
            LLMRateLimitError,
            True,
            7.0,
        ),
        (groq_status_error(groq.RateLimitError, 429), LLMRateLimitError, True, None),
        (groq_status_error(groq.InternalServerError, 500), LLMUnavailableError, True, None),
        (
            groq_status_error(groq.InternalServerError, 503, headers={"retry-after": "2"}),
            LLMUnavailableError,
            True,
            2.0,
        ),
        (groq_status_error(groq.AuthenticationError, 401), LLMAuthError, False, None),
        (groq_status_error(groq.PermissionDeniedError, 403), LLMAuthError, False, None),
        (
            groq_status_error(groq.NotFoundError, 404, body={"code": "model_not_found"}),
            LLMRequestError,
            False,
            None,
        ),
        (
            groq_status_error(groq.BadRequestError, 400, body={"code": "context_length_exceeded"}),
            LLMRequestError,
            False,
            None,
        ),
        (groq.APITimeoutError(request=REQUEST), LLMTimeoutError, True, None),
        (groq.APIConnectionError(request=REQUEST), LLMUnavailableError, True, None),
        (ValueError("anything else"), LLMRequestError, False, None),
    ],
)
def test_classification(exc: Exception, expected: type, retryable: bool, retry_after: float | None):
    result = bare_provider()._classify(exc)
    assert type(result) is expected
    assert result.retryable is retryable and result.retry_after == retry_after


@pytest.mark.parametrize(
    "body",
    [
        {"code": "json_validate_failed", "message": "x"},  # flat
        {"error": {"code": "json_validate_failed", "message": "x"}},  # nested
    ],
)
def test_json_validate_failed_maps_to_invalid_output(body: dict[str, Any]):
    result = bare_provider()._classify(groq_status_error(groq.BadRequestError, 400, body=body))
    assert isinstance(result, LLMInvalidOutputError) and not result.retryable


def test_provider_failures_share_a_base_class_for_the_fallback_chain():
    p = bare_provider()
    for exc in (
        groq_status_error(groq.RateLimitError, 429),
        groq_status_error(groq.InternalServerError, 500),
        groq_status_error(groq.AuthenticationError, 401),
        groq.APITimeoutError(request=REQUEST),
    ):
        assert isinstance(p._classify(exc), LLMProviderError)
    # a rejected request is our bug, and invalid output has its own path: neither is "provider down"
    assert not isinstance(p._classify(groq_status_error(groq.NotFoundError, 404)), LLMProviderError)
    assert not isinstance(
        p._classify(groq_status_error(groq.BadRequestError, 400, body={"code": "json_validate_failed"})),
        LLMProviderError,
    )


def test_error_messages_never_contain_the_provider_response():
    exc = groq_status_error(
        groq.BadRequestError,
        400,
        body={"code": "bad", "message": "echo: card 4111111111111111 jane@example.com"},
    )
    message = str(bare_provider()._classify(exc))
    assert "4111" not in message and "jane@example.com" not in message and "provider said no" not in message


# ---- construction and configuration -----------------------------------------------------------


def settings(**env: Any) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/db", **env)  # type: ignore[call-arg, arg-type]


def test_defaults():
    s = settings()
    assert s.llm_provider is ProviderName.GROQ
    assert (s.llm_timeout_seconds, s.llm_max_retries, s.llm_max_retry_wait_seconds) == (30.0, 3, 20.0)
    assert s.groq_api_key is None and s.groq_model_triage is None and s.groq_model_agent is None


def test_api_key_is_secret():
    s = settings(groq_api_key="gsk_super_secret")
    assert isinstance(s.groq_api_key, SecretStr)
    assert "gsk_super_secret" not in repr(s) and "gsk_super_secret" not in str(s)


@pytest.mark.parametrize(
    "bad",
    [
        {"llm_timeout_seconds": 0},
        {"llm_timeout_seconds": 9999},
        {"llm_max_retries": -1},
        {"llm_max_retries": 50},
        {"llm_provider": "openai"},
    ],
)
def test_settings_reject_bad_llm_values(bad: dict[str, Any]):
    with pytest.raises(ValueError):
        settings(**bad)


def test_get_provider_builds_groq_from_settings():
    s = settings(groq_api_key="k", groq_model_triage="small", groq_model_agent="big", llm_max_retries=1)
    provider = get_provider(s)
    assert isinstance(provider, GroqProvider) and provider.name is ProviderName.GROQ
    assert (
        provider._model_name(ModelRole.TRIAGE) == "small" and provider._model_name(ModelRole.AGENT) == "big"
    )
    assert provider._policy.max_retries == 1


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, ["GROQ_API_KEY", "GROQ_MODEL_TRIAGE", "GROQ_MODEL_AGENT"]),
        ({"groq_api_key": "k", "groq_model_triage": "small"}, ["GROQ_MODEL_AGENT"]),
        ({"groq_api_key": "", "groq_model_triage": "s", "groq_model_agent": "b"}, ["GROQ_API_KEY"]),
    ],
)
def test_missing_groq_config_names_the_variables(env: dict[str, Any], missing: list[str]):
    with pytest.raises(LLMConfigError) as info:
        get_provider(settings(**env))
    for name in missing:
        assert name in str(info.value)


def test_model_names_come_from_config_not_code():
    with pytest.raises(LLMConfigError, match="triage"):
        GroqProvider(api_key="k", models={ModelRole.AGENT: "big"}, policy=POLICY)


def test_ollama_is_not_available_until_phase_10():
    with pytest.raises(LLMConfigError, match="Phase 10"):
        get_provider(settings(llm_provider="ollama"))


# ---- end to end through the real ChatGroq client ------------------------------------------------


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "small",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def error_response(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"error": {"message": "nope", "type": "invalid_request_error", "code": code}}
    return httpx.Response(status, json=body, headers=headers or {})


def provider_over(
    responses: list[httpx.Response | Exception],
) -> tuple[GroqProvider, list[dict[str, Any]], Sleeps]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {"headers": dict(request.headers), "body": json.loads(request.content), "url": str(request.url)}
        )
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = Sleeps()
    provider = GroqProvider(
        api_key="test-key-not-real",
        models={ModelRole.TRIAGE: "small", ModelRole.AGENT: "big"},
        policy=POLICY,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleeps,
        jitter=lambda: 0.0,
    )
    return provider, seen, sleeps


async def ask(provider: GroqProvider) -> TriageOutput:
    return await provider.structured(
        role=ModelRole.TRIAGE, system="You triage.", schema=TriageOutput, untrusted_text="Where is ORD-1001?"
    )


async def test_e2e_request_shape():
    provider, seen, _ = provider_over([completion(GOOD_TRIAGE)])
    result = await ask(provider)
    assert result.intent is Intent.ORDER_STATUS
    request = seen[0]
    assert request["url"].endswith("/chat/completions")
    assert request["headers"]["authorization"] == "Bearer test-key-not-real"
    body = request["body"]
    # langchain-groq sends temperature 0 as 1e-8 (Groq does not accept an exact 0): still greedy.
    assert body["model"] == "small" and body["temperature"] < 1e-6
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "<customer_message>\nWhere is ORD-1001?\n</customer_message>" in body["messages"][1]["content"]


async def test_e2e_429_with_retry_after_is_waited_out_then_succeeds():
    provider, seen, sleeps = provider_over(
        [error_response(429, "rate_limit_exceeded", {"retry-after": "3"}), completion(GOOD_TRIAGE)]
    )
    assert (await ask(provider)).confidence == 0.9
    assert len(seen) == 2 and sleeps.delays == [3.0]  # the SDK did not also retry on its own


async def test_e2e_huge_retry_after_fails_fast_so_the_caller_can_fall_back():
    provider, seen, sleeps = provider_over(
        [error_response(429, "rate_limit_exceeded", {"retry-after": "7200"})]
    )
    with pytest.raises(LLMRateLimitError) as info:
        await ask(provider)
    assert info.value.retry_after == 7200.0 and len(seen) == 1 and sleeps.delays == []


async def test_e2e_server_errors_back_off_then_succeed():
    provider, seen, sleeps = provider_over(
        [error_response(503, "unavailable"), error_response(500, "boom"), completion(GOOD_TRIAGE)]
    )
    await ask(provider)
    assert len(seen) == 3 and sleeps.delays == [0.5, 1.0]


async def test_e2e_connection_error_is_retried():
    provider, seen, _ = provider_over([httpx.ConnectError("refused"), completion(GOOD_TRIAGE)])
    await ask(provider)
    assert len(seen) == 2


async def test_e2e_bad_key_is_not_retried():
    provider, seen, sleeps = provider_over([error_response(401, "invalid_api_key")])
    with pytest.raises(LLMAuthError):
        await ask(provider)
    assert len(seen) == 1 and sleeps.delays == []


async def test_e2e_json_validate_failed_triggers_the_corrective_retry():
    provider, seen, sleeps = provider_over(
        [error_response(400, "json_validate_failed"), completion(GOOD_TRIAGE)]
    )
    await ask(provider)
    assert len(seen) == 2 and sleeps.delays == []
    assert "rejected" in seen[1]["body"]["messages"][-1]["content"]


async def test_e2e_invalid_json_twice_hands_back_invalid_output():
    provider, seen, _ = provider_over([completion("Sure! It's a refund."), completion("Still not JSON")])
    with pytest.raises(LLMInvalidOutputError):
        await ask(provider)
    assert len(seen) == 2


async def test_e2e_tools_are_sent_as_function_definitions():
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class Args(BaseModel):
        order_id: str

    tool = StructuredTool.from_function(
        func=lambda order_id: "{}",
        name="lookup_order_status",
        description="Look up an order.",
        args_schema=Args,
    )
    final = completion("It was delivered.")
    provider, seen, _ = provider_over([final])
    result = await provider.run_tools(
        role=ModelRole.AGENT, system="Help.", tools=[tool], untrusted_text="status?"
    )
    assert result.text == "It was delivered."
    body = seen[0]["body"]
    assert body["model"] == "big"
    assert [t["function"]["name"] for t in body["tools"]] == ["lookup_order_status"]
