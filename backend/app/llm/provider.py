"""The one LLM interface the agents use. Providers (Groq now, Ollama in Phase 10) only supply
four small hooks; prompt wrapping, retries, output validation and the tool loop live here so
every provider behaves the same.

What an agent can ask for:
  structured()     JSON that must validate against a Pydantic model (triage, intake)
  generate_text()  plain text (responder, query rewrite)
  run_tools()      text, after the model has called allowlisted read-only tools (resolution)

Rules enforced here, not left to prompt authors:
  * Customer text goes in `untrusted_text` and is always wrapped in delimiters.
  * Invalid structured output is retried once with the problems listed, then raises
    `LLMInvalidOutputError` so the case can go to a human.
  * Only the tools passed in can run; anything else the model asks for is refused.
  * Prompts, customer text and model output are never logged.
"""

import asyncio
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ValidationError

from app.contracts.enums import ProviderName
from app.llm.errors import LLMError, LLMInvalidOutputError, LLMRequestError, LLMToolLoopError
from app.llm.retry import RetryPolicy, call_with_retries
from app.llm.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.tracing import Tracer

log = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_ROUND = 4
_MAX_PROBLEMS = 5
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class ModelRole(StrEnum):
    """Which size of model a call needs. The provider maps a role to a model name from env."""

    TRIAGE = "triage"  # small, fast: triage
    AGENT = "agent"  # large: refund intake, resolution, responder


@dataclass(frozen=True)
class ToolCallRecord:
    """What happened to one tool call the model asked for. Audit material for Phase 4."""

    name: str
    outcome: Literal["ok", "unknown_tool", "invalid_args", "error", "skipped"]


@dataclass(frozen=True)
class ToolRunResult:
    text: str
    tool_calls: tuple[ToolCallRecord, ...]


def message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = [b if isinstance(b, str) else str(b.get("text", "")) for b in content if b]
    return "".join(parts)


def parse_model_output[T: BaseModel](raw: str, schema: type[T]) -> T:
    """Parse and validate model output. Raises `LLMInvalidOutputError` with safe problems."""
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise LLMInvalidOutputError(
            "model output is not valid JSON", problems=("The reply was not valid JSON.",)
        ) from None
    if not isinstance(data, dict):
        raise LLMInvalidOutputError(
            "model output is not a JSON object", problems=("The reply must be a single JSON object.",)
        )
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        problems = tuple(
            f"{'.'.join(str(p) for p in e['loc']) or 'reply'}: {e['msg']}"[:200]
            for e in exc.errors(include_url=False, include_context=False, include_input=False)
        )[:_MAX_PROBLEMS]
        raise LLMInvalidOutputError("model output failed schema validation", problems=problems) from None


def _correction(problems: tuple[str, ...]) -> str:
    listed = " ".join(problems) if problems else "It did not match the schema."
    return (
        f"Your previous reply was rejected. {listed} "
        "Reply again with only a single JSON object that matches the schema exactly, "
        "with no extra fields."
    )


class LLMProvider(ABC):
    name: ProviderName

    def __init__(
        self,
        *,
        policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        tracer: Tracer | None = None,
    ) -> None:
        self._policy = policy
        self._sleep = sleep
        self._jitter = jitter
        self._tracer = tracer

    # ---- hooks a provider implements -----------------------------------------------------

    @abstractmethod
    def _model(self, role: ModelRole, temperature: float) -> BaseChatModel: ...

    @abstractmethod
    def _model_name(self, role: ModelRole) -> str: ...

    @abstractmethod
    def _json_mode(self, model: BaseChatModel) -> Runnable[LanguageModelInput, BaseMessage]:
        """Return `model` set up so it answers with a JSON object."""

    @abstractmethod
    def _classify(self, exc: Exception) -> LLMError:
        """Map a provider exception to an `LLMError` (retryable or not)."""

    # ---- public API ----------------------------------------------------------------------

    async def structured[T: BaseModel](
        self,
        *,
        role: ModelRole,
        system: str,
        schema: type[T],
        untrusted_text: str | None = None,
        task: str = "",
        temperature: float = 0.0,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        system_text = (
            f"{system.strip()}\n\n{UNTRUSTED_NOTICE}\n\n"
            "Respond with a single JSON object and nothing else: no prose, no markdown. "
            f"It must match this JSON schema exactly, and extra fields are not allowed:\n{schema_json}"
        )
        base: list[BaseMessage] = [
            SystemMessage(content=system_text),
            HumanMessage(content=self._user_text(task, untrusted_text)),
        ]
        messages = list(base)
        problems: tuple[str, ...] = ()
        for _ in range(2):  # the first try, then one corrective retry
            try:
                ai = await self._invoke(role, temperature, messages, json_mode=True)
                return parse_model_output(message_text(ai), schema)
            except LLMInvalidOutputError as exc:
                problems = exc.problems
                messages = [*base, HumanMessage(content=_correction(problems))]
        log.warning("llm output invalid twice", extra={"provider": self.name.value, "role": role.value})
        raise LLMInvalidOutputError("model output failed validation twice", problems=problems)

    async def generate_text(
        self,
        *,
        role: ModelRole,
        system: str,
        untrusted_text: str | None = None,
        task: str = "",
        temperature: float = 0.0,
    ) -> str:
        messages: list[BaseMessage] = [
            SystemMessage(content=f"{system.strip()}\n\n{UNTRUSTED_NOTICE}"),
            HumanMessage(content=self._user_text(task, untrusted_text)),
        ]
        ai = await self._invoke(role, temperature, messages)
        text = message_text(ai).strip()
        if not text:
            raise LLMInvalidOutputError("model returned an empty reply")
        return text

    async def run_tools(
        self,
        *,
        role: ModelRole,
        system: str,
        tools: Sequence[BaseTool],
        untrusted_text: str | None = None,
        task: str = "",
        max_rounds: int = 3,
        temperature: float = 0.0,
    ) -> ToolRunResult:
        """Let the model call `tools` (and nothing else) for up to `max_rounds` rounds, then
        return its final text. Tool names and arguments come from the model and are untrusted."""
        if not tools:
            raise ValueError("run_tools needs at least one tool; use generate_text instead")
        by_name = {t.name: t for t in tools}
        messages: list[BaseMessage] = [
            SystemMessage(content=f"{system.strip()}\n\n{UNTRUSTED_NOTICE}"),
            HumanMessage(content=self._user_text(task, untrusted_text)),
        ]
        records: list[ToolCallRecord] = []
        for round_number in range(max_rounds + 1):
            ai = await self._invoke(role, temperature, messages, tools=tools)
            calls = ai.tool_calls
            if not calls:
                text = message_text(ai).strip()
                if not text:
                    raise LLMInvalidOutputError("model returned an empty reply")
                return ToolRunResult(text=text, tool_calls=tuple(records))
            if round_number == max_rounds:
                break
            messages.append(ai)
            for index, call in enumerate(calls):
                if index >= MAX_TOOL_CALLS_PER_ROUND:
                    records.append(ToolCallRecord(str(call.get("name", ""))[:60], "skipped"))
                    messages.append(
                        ToolMessage(
                            content="Error: too many tool calls at once.",
                            tool_call_id=call.get("id") or "",
                            status="error",
                        )
                    )
                    continue
                message, record = await self._run_one_tool(call, by_name)
                records.append(record)
                messages.append(message)
        log.warning("llm tool loop exhausted", extra={"provider": self.name.value, "role": role.value})
        raise LLMToolLoopError(f"model still wanted tools after {max_rounds} rounds")

    # ---- internals -----------------------------------------------------------------------

    @staticmethod
    def _user_text(task: str, untrusted_text: str | None) -> str:
        parts = [task.strip()] if task.strip() else []
        if untrusted_text is not None and untrusted_text.strip():
            parts.append(wrap_untrusted(untrusted_text))
        if not parts:
            raise ValueError("a call needs a task, untrusted_text, or both")
        return "\n\n".join(parts)

    async def _invoke(
        self,
        role: ModelRole,
        temperature: float,
        messages: list[BaseMessage],
        *,
        json_mode: bool = False,
        tools: Sequence[BaseTool] | None = None,
    ) -> AIMessage:
        model = self._model(role, temperature)
        runnable: Runnable[LanguageModelInput, BaseMessage]
        if json_mode:
            runnable = self._json_mode(model)
        elif tools:
            runnable = model.bind_tools(list(tools))
        else:
            runnable = model
        context: dict[str, object] = {
            "provider": self.name.value,
            "role": role.value,
            "model": self._model_name(role),
        }
        started = time.perf_counter()
        trace = (
            self._tracer.start(
                name="llm_call",
                metadata={"provider": self.name.value, "role": role.value, "model": self._model_name(role)},
            )
            if self._tracer
            else None
        )
        try:
            result = await call_with_retries(
                lambda: runnable.ainvoke(messages),
                policy=self._policy,
                classify=self._classify,
                sleep=self._sleep,
                jitter=self._jitter,
                context=context,
            )
        except Exception as exc:
            if self._tracer:
                self._tracer.end(trace, error=type(exc), started=started)
            raise
        if not isinstance(result, AIMessage):
            raise LLMRequestError("provider returned an unexpected response type")
        usage: Mapping[str, Any] = result.usage_metadata or {}
        log.info(
            "llm call",
            extra={
                **context,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                # A dict, so the log redactor does not mistake token counts for secrets.
                "usage": {"in": usage.get("input_tokens"), "out": usage.get("output_tokens")},
            },
        )
        if self._tracer:
            self._tracer.end(
                trace,
                output={
                    "status": "success",
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                },
                started=started,
            )
        return result

    @staticmethod
    async def _run_one_tool(call: Any, by_name: dict[str, BaseTool]) -> tuple[ToolMessage, ToolCallRecord]:
        name = str(call.get("name", ""))
        call_id = call.get("id") or ""

        def refuse(
            text: str, outcome: Literal["unknown_tool", "invalid_args", "error"]
        ) -> tuple[ToolMessage, ToolCallRecord]:
            return (
                ToolMessage(content=f"Error: {text}", tool_call_id=call_id, status="error"),
                ToolCallRecord(name[:60], outcome),
            )

        tool = by_name.get(name)
        if tool is None:
            return refuse("that tool is not available.", "unknown_tool")
        args = call.get("args")
        try:
            output = await tool.ainvoke(args if isinstance(args, dict) else {})
        except (ValidationError, ToolException):
            return refuse("the arguments were not valid.", "invalid_args")
        except Exception:
            log.exception("tool failed", extra={"tool": name})
            return refuse("the tool failed.", "error")
        content = output if isinstance(output, str) else json.dumps(output, default=str)
        return (
            ToolMessage(content=content, tool_call_id=call_id, status="success"),
            ToolCallRecord(name, "ok"),
        )
