"""Optional Langfuse tracing with a strict PII-safe event boundary.

Tracing never receives prompts, customer messages, model output, tool arguments, account
numbers, or email addresses. Only operational metadata and masked error class names leave
the process.
"""

import logging
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.config import Settings

log = logging.getLogger(__name__)
_SAFE_VALUE = re.compile(r"[^A-Za-z0-9_.:/-]")


def mask_trace_value(value: object) -> str:
    """Keep trace labels useful while preventing free-form text from becoming telemetry."""
    text = str(value)
    return _SAFE_VALUE.sub("_", text)[:120]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    observation_id: str


class Tracer:
    """Small adapter that makes tracing optional and testable."""

    def __init__(self, settings: Settings) -> None:
        self._client: Any | None = None
        self.enabled = False
        self._sampled = False
        if not settings.tracing_enabled:
            return
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            log.warning("tracing enabled but Langfuse credentials are missing")
            return
        if random.random() > settings.tracing_sample_rate:
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                host=settings.langfuse_host,
            )
            self.enabled = True
            self._sampled = True
        except ImportError:
            log.exception("tracing enabled but Langfuse is not installed")

    def start(
        self, *, name: str, trace_id: str | None = None, metadata: Mapping[str, object] | None = None
    ) -> TraceContext | None:
        if not self.enabled or self._client is None:
            return None
        safe_metadata = {
            mask_trace_value(key): mask_trace_value(value) for key, value in (metadata or {}).items()
        }
        trace = self._client.trace(
            id=trace_id or str(uuid4()),
            name=mask_trace_value(name),
            metadata=safe_metadata,
        )
        observation = trace.span(name=mask_trace_value(name), input={"event": "started"})
        return TraceContext(trace_id=str(trace.id), observation_id=str(observation.id))

    def end(
        self,
        context: TraceContext | None,
        *,
        output: Mapping[str, object] | None = None,
        error: type[BaseException] | None = None,
        started: float | None = None,
    ) -> None:
        if context is None or self._client is None:
            return
        safe_output = {
            mask_trace_value(key): mask_trace_value(value) for key, value in (output or {}).items()
        }
        observation = self._client.span(id=context.observation_id)
        observation.end(
            output=safe_output,
            level="ERROR" if error else "DEFAULT",
            status_message=mask_trace_value(error.__name__) if error else None,
            metadata={"latency_ms": round((time.perf_counter() - started) * 1000) if started else None},
        )
        self._client.flush()


def trace_id_from_context(context: TraceContext | None) -> str | None:
    return context.trace_id if context else None
