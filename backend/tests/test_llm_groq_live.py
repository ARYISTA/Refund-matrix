"""Optional smoke test against the real Groq API. Skipped unless GROQ_API_KEY, GROQ_MODEL_TRIAGE and
GROQ_MODEL_AGENT are all set in the environment. Costs a few tokens. Run it yourself:

    GROQ_API_KEY=... GROQ_MODEL_TRIAGE=... GROQ_MODEL_AGENT=... pytest tests/test_llm_groq_live.py
"""

import os

import pytest

from app.contracts.agents import TriageOutput
from app.contracts.enums import Intent
from app.llm.groq_provider import GroqProvider
from app.llm.provider import ModelRole
from app.llm.retry import RetryPolicy

_NEEDED = ("GROQ_API_KEY", "GROQ_MODEL_TRIAGE", "GROQ_MODEL_AGENT")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in _NEEDED), reason="GROQ_API_KEY and model names not set"
)


def live_provider() -> GroqProvider:
    return GroqProvider(
        api_key=os.environ["GROQ_API_KEY"],
        models={
            ModelRole.TRIAGE: os.environ["GROQ_MODEL_TRIAGE"],
            ModelRole.AGENT: os.environ["GROQ_MODEL_AGENT"],
        },
        policy=RetryPolicy(max_retries=2, timeout_seconds=30, max_wait_seconds=20),
    )


async def test_live_structured_triage():
    result = await live_provider().structured(
        role=ModelRole.TRIAGE,
        system=(
            "Classify the customer message. intent is one of: refund_request, order_status, "
            "policy_question, general, unclear."
        ),
        schema=TriageOutput,
        untrusted_text="My kettle from order ORD-1001 arrived broken. I want my money back.",
    )
    assert isinstance(result.intent, Intent)
    assert 0 <= result.confidence <= 1


async def test_live_plain_text():
    text = await live_provider().generate_text(
        role=ModelRole.AGENT, system="Reply in one short sentence.", task="Say hello to a customer."
    )
    assert text
