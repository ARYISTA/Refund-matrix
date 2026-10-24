"""Timeouts and backoff for provider calls. Provider-agnostic: each provider supplies a
`classify` function that turns its own exceptions into `LLMError`s.

Rules:
  * Every attempt has a hard timeout.
  * Rate limits, timeouts and 5xx/connection errors are retried; everything else is not.
  * If the provider sent Retry-After, wait exactly that long (never less) ...
  * ... unless it is longer than `max_wait_seconds`. A customer should not sit through a
    two-minute wait, so we raise at once and the caller falls back or hands off to a human.
  * Without Retry-After: exponential backoff with jitter, capped.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.llm.errors import LLMError, LLMTimeoutError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int  # retries after the first attempt
    timeout_seconds: float  # per attempt
    max_wait_seconds: float  # longest Retry-After we are willing to sleep through
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either delay-seconds or an HTTP date. Returns seconds, or None."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, seconds)


async def call_with_retries[T](
    op: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    classify: Callable[[Exception], LLMError],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    context: dict[str, object] | None = None,
) -> T:
    """Run `op`, retrying transient failures. Raises an `LLMError` when it gives up."""
    attempt = 0
    while True:
        cause: Exception
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                return await op()
        except TimeoutError as exc:
            cause = exc
            error: LLMError = LLMTimeoutError(f"LLM call timed out after {policy.timeout_seconds:g}s")
        except LLMError as exc:
            cause = exc
            error = exc
        except Exception as exc:
            cause = exc
            error = classify(exc)

        if not error.retryable or attempt >= policy.max_retries:
            raise error from cause

        if error.retry_after is not None:
            if error.retry_after > policy.max_wait_seconds:
                log.warning(
                    "llm retry-after too long, giving up",
                    extra={**(context or {}), "retry_after": error.retry_after, "attempt": attempt + 1},
                )
                raise error from cause
            delay = error.retry_after
        else:
            ceiling = min(policy.max_delay_seconds, policy.base_delay_seconds * 2**attempt)
            delay = ceiling / 2 + (ceiling / 2) * jitter()  # "equal jitter": never near zero

        log.warning(
            "llm call failed, retrying",
            extra={
                **(context or {}),
                "attempt": attempt + 1,
                "error": type(error).__name__,
                "delay_s": round(delay, 3),
            },
        )
        await sleep(delay)
        attempt += 1
