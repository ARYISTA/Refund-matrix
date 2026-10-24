"""Phase 3: timeouts, backoff and Retry-After handling (provider-agnostic)."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.retry import RetryPolicy, call_with_retries, parse_retry_after
from tests.llm_helpers import Sleeps

POLICY = RetryPolicy(
    max_retries=3, timeout_seconds=1.0, max_wait_seconds=10.0, base_delay_seconds=1.0, max_delay_seconds=8.0
)


def classify(exc: Exception) -> LLMError:
    return LLMRequestError(f"unexpected {type(exc).__name__}")


class Flaky:
    """An operation that raises the given errors in order, then returns 'ok'."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def run(op: Flaky, policy: RetryPolicy = POLICY, jitter: float = 0.0) -> tuple[str, Sleeps]:
    sleeps = Sleeps()
    result = await call_with_retries(
        op, policy=policy, classify=classify, sleep=sleeps, jitter=lambda: jitter
    )
    return result, sleeps


# ---- Retry-After parsing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 2.5 ", 2.5),
        ("0", 0.0),
        ("-3", 0.0),
        (None, None),
        ("", None),
        ("soon", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_parse_retry_after_seconds(value: str | None, expected: float | None):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Fri, 23 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("Fri, 23 Oct 2026 11:00:00 GMT", now=now) == 0.0  # already past


# ---- retry behaviour --------------------------------------------------------------------------


async def test_success_first_try_never_sleeps():
    result, sleeps = await run(Flaky())
    assert result == "ok" and sleeps.delays == []


async def test_backoff_grows_exponentially_with_jitter():
    errors = [LLMUnavailableError("down") for _ in range(3)]
    # jitter=0 -> the low end of "equal jitter": half the ceiling (1, 2, 4 -> 0.5, 1, 2)
    result, sleeps = await run(Flaky(*errors), jitter=0.0)
    assert result == "ok" and sleeps.delays == [0.5, 1.0, 2.0]
    # jitter=1 -> the full ceiling
    _, sleeps = await run(Flaky(*[LLMUnavailableError("down") for _ in range(3)]), jitter=1.0)
    assert sleeps.delays == [1.0, 2.0, 4.0]


async def test_backoff_is_capped():
    policy = RetryPolicy(
        max_retries=6,
        timeout_seconds=1.0,
        max_wait_seconds=10.0,
        base_delay_seconds=1.0,
        max_delay_seconds=3.0,
    )
    _, sleeps = await run(Flaky(*[LLMUnavailableError("down") for _ in range(6)]), policy, jitter=1.0)
    assert max(sleeps.delays) == 3.0


async def test_retry_after_is_honoured_exactly():
    result, sleeps = await run(Flaky(LLMRateLimitError("slow down", retry_after=4.0)))
    assert result == "ok" and sleeps.delays == [4.0]


async def test_retry_after_zero_retries_immediately():
    result, sleeps = await run(Flaky(LLMRateLimitError("slow down", retry_after=0.0)))
    assert result == "ok" and sleeps.delays == [0.0]


async def test_retry_after_longer_than_cap_gives_up_without_waiting():
    op = Flaky(LLMRateLimitError("daily limit", retry_after=3600.0))
    sleeps = Sleeps()
    with pytest.raises(LLMRateLimitError) as info:
        await call_with_retries(op, policy=POLICY, classify=classify, sleep=sleeps)
    assert info.value.retry_after == 3600.0
    assert op.calls == 1 and sleeps.delays == []


async def test_gives_up_after_max_retries_with_the_last_error():
    op = Flaky(*[LLMUnavailableError(f"down {i}") for i in range(10)])
    with pytest.raises(LLMUnavailableError, match="down 3"):
        await run(op)
    assert op.calls == POLICY.max_retries + 1


async def test_zero_retries_means_one_attempt():
    policy = RetryPolicy(max_retries=0, timeout_seconds=1.0, max_wait_seconds=10.0)
    op = Flaky(LLMUnavailableError("down"))
    with pytest.raises(LLMUnavailableError):
        await run(op, policy)
    assert op.calls == 1


@pytest.mark.parametrize("error", [LLMAuthError("bad key"), LLMRequestError("bad request")])
async def test_non_retryable_errors_are_raised_immediately(error: LLMError):
    op = Flaky(error)
    with pytest.raises(type(error)):
        await run(op)
    assert op.calls == 1


async def test_unknown_exceptions_go_through_classify_and_are_not_retried():
    op = Flaky(ValueError("secret prompt text"))
    with pytest.raises(LLMRequestError) as info:
        await run(op)
    assert op.calls == 1
    assert "secret prompt text" not in str(info.value)  # classify decided the message
    assert isinstance(info.value.__cause__, ValueError)  # but the cause is kept for debugging


async def test_timeout_is_enforced_and_retried():
    attempts = 0

    async def hangs() -> str:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(5)
        return "never"

    policy = RetryPolicy(max_retries=1, timeout_seconds=0.05, max_wait_seconds=10.0)
    sleeps = Sleeps()
    with pytest.raises(LLMTimeoutError, match="timed out"):
        await call_with_retries(hangs, policy=policy, classify=classify, sleep=sleeps, jitter=lambda: 0.0)
    assert attempts == 2 and len(sleeps.delays) == 1


async def test_timeout_then_success():
    state = {"n": 0}

    async def slow_once() -> str:
        state["n"] += 1
        if state["n"] == 1:
            await asyncio.sleep(5)
        return "ok"

    policy = RetryPolicy(max_retries=2, timeout_seconds=0.05, max_wait_seconds=10.0)
    result = await call_with_retries(
        slow_once, policy=policy, classify=classify, sleep=Sleeps(), jitter=lambda: 0.0
    )
    assert result == "ok" and state["n"] == 2


async def test_cancellation_is_not_swallowed():
    async def forever() -> str:
        await asyncio.sleep(60)
        return "never"

    task = asyncio.create_task(
        call_with_retries(forever, policy=POLICY, classify=classify, sleep=Sleeps(), jitter=lambda: 0.0)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
