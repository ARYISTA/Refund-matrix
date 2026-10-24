"""Test doubles for the LLM layer. No network, no sleeping."""

import asyncio
from dataclasses import dataclass
from typing import Any

import groq
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.llm.groq_provider import GroqProvider
from app.llm.provider import ModelRole
from app.llm.retry import RetryPolicy


@dataclass
class Delay:
    """Script item: wait (really) before answering. Used to trigger timeouts."""

    seconds: float
    then: AIMessage


class ScriptedChatModel(BaseChatModel):
    """Plays back a script: an AIMessage is returned, an Exception is raised, a Delay waits."""

    script: list[Any]
    calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _next(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> Any:
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of scripted replies")
        return self.script.pop(0)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        item = self._next(messages, kwargs)
        if isinstance(item, Exception):
            raise item
        message = item.then if isinstance(item, Delay) else item
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        item = self._next(messages, kwargs)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Delay):
            await asyncio.sleep(item.seconds)
            item = item.then
        return ChatResult(generations=[ChatGeneration(message=item)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self.bind(tool_names=[t.name for t in tools], **kwargs)


def ai(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class Sleeps:
    """Records the delays the retry layer asked for, without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_provider(
    model: BaseChatModel | dict[ModelRole, BaseChatModel],
    *,
    max_retries: int = 2,
    timeout: float = 5.0,
    max_wait: float = 10.0,
) -> tuple[GroqProvider, Sleeps]:
    sleeps = Sleeps()
    models = model if isinstance(model, dict) else {ModelRole.TRIAGE: model, ModelRole.AGENT: model}
    provider = GroqProvider(
        api_key="test-key-not-real",
        models={ModelRole.TRIAGE: "small-model", ModelRole.AGENT: "big-model"},
        policy=RetryPolicy(
            max_retries=max_retries,
            timeout_seconds=timeout,
            max_wait_seconds=max_wait,
            base_delay_seconds=1.0,
            max_delay_seconds=8.0,
        ),
        model_factory=lambda role, _temperature: models[role],
        sleep=sleeps,
        jitter=lambda: 0.0,
    )
    return provider, sleeps


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_status_error(
    cls: type[groq.APIStatusError],
    status: int,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> groq.APIStatusError:
    request = httpx.Request("POST", GROQ_URL)
    response = httpx.Response(status, headers=headers or {}, request=request, json=body or {})
    return cls("provider said no", response=response, body=body)
