"""LangGraph state for one case. One thread_id per case; PostgresSaver persists this model.

Field ownership (who may write what) is documented in docs/state-model.md.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.contracts.agents import RefundIntakeOutput, TriageOutput
from app.contracts.decisions import PolicyResult
from app.contracts.enums import CaseStatus, NodeName, NodeStatus, ReviewAction
from app.contracts.facts import RefundFacts
from app.contracts.payout import PayoutResult

MAX_MESSAGE_CHARS = 2000  # hard cap on a single customer message


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["customer", "assistant"]
    # Customer text is untrusted. Prompts must wrap it in delimiters.
    content: str = Field(min_length=1, max_length=4000)


class ReviewDecision(BaseModel):
    """Resume value for human_review: Command(resume=ReviewDecision(...).model_dump(mode='json'))."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ReviewAction
    reviewer_id: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=500)
    decided_at: AwareDatetime


class CaseState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Identity. customer_id comes from the authenticated session, never from the body.
    case_id: UUID
    thread_id: str = Field(min_length=1, max_length=100)
    customer_id: str = Field(min_length=1, max_length=40)

    messages: list[ChatMessage] = Field(default_factory=list)

    # Progress
    status: CaseStatus = CaseStatus.OPEN
    current_node: NodeName | None = None
    last_node_status: NodeStatus | None = None

    # Agent outputs (validated models, never free text)
    triage: TriageOutput | None = None
    intake: RefundIntakeOutput | None = None

    # Deterministic results (plain code)
    facts: RefundFacts | None = None
    policy_result: PolicyResult | None = None
    review: ReviewDecision | None = None
    payout: PayoutResult | None = None

    # Final reply, written by the responder from the verified facts above
    resolution_text: str | None = Field(default=None, max_length=4000)
    reply: str | None = Field(default=None, max_length=4000)

    # Failure path: set when validation retry or provider fallback is exhausted
    handoff_reason: str | None = Field(default=None, max_length=500)

    # Versions and tracing, stamped for audit and reproducibility
    policy_version: str | None = None
    prompt_version: str | None = None
    trace_id: str | None = None

    created_at: datetime | None = None
