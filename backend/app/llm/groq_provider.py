"""Groq adapter. Uses langchain-groq's ChatGroq as the model wrapper so Phase 6 can trace every
call through the LangChain callback handler. The SDK's own retries are switched off
(`max_retries=0`); retries, Retry-After handling and timeouts are done once, in `retry.py`.

Structured output uses JSON mode plus our own Pydantic validation rather than Groq's
schema-enforced mode, which only some models support. That keeps behaviour identical across
models and lets the invalid-output retry live in one place.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import groq
import httpx
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_groq import ChatGroq
from pydantic import SecretStr

from app.config import Settings
from app.contracts.enums import ProviderName
from app.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.provider import LLMProvider, ModelRole
from app.llm.retry import RetryPolicy, parse_retry_after
from app.tracing import Tracer

log = logging.getLogger(__name__)

ModelFactory = Callable[[ModelRole, float], BaseChatModel]


def _error_code(exc: groq.APIStatusError) -> str | None:
    """Groq puts a short machine-readable code in the error body (e.g. json_validate_failed)."""
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    body = exc.body
    if isinstance(body, Mapping):
        inner = body.get("error", body)
        if isinstance(inner, Mapping) and isinstance(inner.get("code"), str):
            return str(inner["code"])
    return None


class GroqProvider(LLMProvider):
    name = ProviderName.GROQ

    def __init__(
        self,
        *,
        api_key: SecretStr | str,
        models: Mapping[ModelRole, str],
        policy: RetryPolicy,
        model_factory: ModelFactory | None = None,
        http_async_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        extra: dict[str, Any] = {}
        if sleep is not None:
            extra["sleep"] = sleep
        if jitter is not None:
            extra["jitter"] = jitter
        if tracer is not None:
            extra["tracer"] = tracer
        super().__init__(policy=policy, **extra)
        missing = [role for role in ModelRole if not models.get(role)]
        if missing:
            raise LLMConfigError(f"No Groq model configured for: {', '.join(r.value for r in missing)}")
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._models = dict(models)
        self._model_factory = model_factory
        self._http_async_client = http_async_client
        self._cache: dict[tuple[ModelRole, float], BaseChatModel] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "GroqProvider":
        missing = [
            env
            for env, value in (
                ("GROQ_API_KEY", settings.groq_api_key and settings.groq_api_key.get_secret_value()),
                ("GROQ_MODEL_TRIAGE", settings.groq_model_triage),
                ("GROQ_MODEL_AGENT", settings.groq_model_agent),
            )
            if not value
        ]
        if missing:
            raise LLMConfigError(f"Set these environment variables to use Groq: {', '.join(missing)}")
        assert settings.groq_api_key and settings.groq_model_triage and settings.groq_model_agent
        return cls(
            api_key=settings.groq_api_key,
            models={ModelRole.TRIAGE: settings.groq_model_triage, ModelRole.AGENT: settings.groq_model_agent},
            policy=RetryPolicy(
                max_retries=settings.llm_max_retries,
                timeout_seconds=settings.llm_timeout_seconds,
                max_wait_seconds=settings.llm_max_retry_wait_seconds,
            ),
            tracer=Tracer(settings),
        )

    # ---- hooks ---------------------------------------------------------------------------

    def _model_name(self, role: ModelRole) -> str:
        return self._models[role]

    def _model(self, role: ModelRole, temperature: float) -> BaseChatModel:
        key = (role, temperature)
        if key not in self._cache:
            if self._model_factory is not None:
                self._cache[key] = self._model_factory(role, temperature)
            else:
                kwargs: dict[str, Any] = {}
                if self._http_async_client is not None:
                    kwargs["http_async_client"] = self._http_async_client
                self._cache[key] = ChatGroq(
                    model=self._models[role],
                    api_key=self._api_key,
                    temperature=temperature,
                    # Our retry layer owns the timeout; the client's is a slightly longer backstop.
                    timeout=self._policy.timeout_seconds + 2,
                    max_retries=0,
                    **kwargs,
                )
        return self._cache[key]

    def _json_mode(self, model: BaseChatModel) -> Runnable[LanguageModelInput, BaseMessage]:
        return model.bind(response_format={"type": "json_object"})

    def _classify(self, exc: Exception) -> LLMError:
        # Messages carry only the status and Groq's short error code, never the request.
        if isinstance(exc, groq.APITimeoutError):  # a subclass of APIConnectionError: check first
            return LLMTimeoutError("Groq request timed out")
        if isinstance(exc, groq.APIConnectionError):
            return LLMUnavailableError("Could not reach Groq")
        if isinstance(exc, groq.APIStatusError):
            status = exc.status_code
            code = _error_code(exc)
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            if status == 429:
                return LLMRateLimitError("Groq rate limit (HTTP 429)", retry_after=retry_after)
            if status in (401, 403):
                return LLMAuthError(f"Groq rejected the credentials (HTTP {status})")
            if status == 400 and code == "json_validate_failed":
                return LLMInvalidOutputError(
                    "Groq could not produce valid JSON", problems=("The reply was not valid JSON.",)
                )
            if status == 408:
                return LLMTimeoutError("Groq request timed out (HTTP 408)")
            if status >= 500:
                return LLMUnavailableError(f"Groq unavailable (HTTP {status})", retry_after=retry_after)
            return LLMRequestError(f"Groq rejected the request (HTTP {status}, {code or 'no code'})")
        return LLMRequestError(f"Unexpected {type(exc).__name__} from the model client")
