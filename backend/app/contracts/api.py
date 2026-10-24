"""HTTP request/response models (Phase 8 implements the routes; see docs/api-spec.md).

Rules baked into the schemas:
- Request bodies use extra="forbid", so a client cannot smuggle in `amount`, `customer_id`
  or `role`. Identity comes from the JWT cookie; amounts come from the database.
- CaseOut is customer-safe and has no trace fields. ReviewerCaseOut adds the audit trail,
  trace id and handoff reason. Customers never receive a trace id or link.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    JsonValue,
)

from app.contracts.decisions import PolicyResult
from app.contracts.enums import (
    CaseStatus,
    NodeName,
    NodeStatus,
    ReasonCategory,
    ReviewAction,
    UserRole,
)
from app.contracts.money import Paise
from app.contracts.payout import PayoutResult
from app.contracts.state import MAX_MESSAGE_CHARS, ChatMessage


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- Errors (messages are plain human sentences) ----------


class ErrorResponse(_Response):
    code: str = Field(pattern=r"^[a-z_]+$")  # e.g. "case_not_pending"
    message: str


# ---- Auth ----------


class LoginRequest(_Request):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(_Response):
    id: str
    email: EmailStr
    role: UserRole


# ---- Chat ----------


class ChatRequest(_Request):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    case_id: UUID | None = None  # None => start a new case


class TimelineStep(_Response):
    node: NodeName
    status: NodeStatus
    at: AwareDatetime


# SSE: each event is sent as `event: <type>` + `data: <json of the model>`.
# Order for a normal turn: case, (node | token | policy)*, [interrupt], done.
class CaseEvent(_Response):
    type: Literal["case"] = "case"
    case_id: UUID  # first event, so the client can restore state on refresh


class NodeEvent(_Response):
    type: Literal["node"] = "node"
    node: NodeName
    status: NodeStatus


class TokenEvent(_Response):
    type: Literal["token"] = "token"
    text: str


class PolicyEvent(_Response):
    type: Literal["policy"] = "policy"
    result: PolicyResult


class InterruptEvent(_Response):
    type: Literal["interrupt"] = "interrupt"
    case_id: UUID
    reason: Literal["needs_approval"] = "needs_approval"


class DoneEvent(_Response):
    type: Literal["done"] = "done"
    case_id: UUID
    status: CaseStatus


class ErrorEvent(_Response):
    type: Literal["error"] = "error"
    message: str


SSEEvent = Annotated[
    CaseEvent | NodeEvent | TokenEvent | PolicyEvent | InterruptEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


# ---- Cases ----------


class CaseOut(_Response):
    """Customer-safe case view. No trace id, no audit details, no reviewer identity."""

    id: UUID
    thread_id: str
    status: CaseStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    messages: list[ChatMessage]
    timeline: list[TimelineStep]
    policy_result: PolicyResult | None = None
    amount_paise: Paise | None = None
    payout: PayoutResult | None = None
    awaiting_approval: bool = False


class CustomerCaseOut(_Response):
    """The intentionally narrow case contract returned to customers."""

    id: UUID
    thread_id: str
    status: Literal["processed", "under_review", "not_eligible", "rejected", "answered", "handed_off"]
    reply: str | None = None
    messages: list[ChatMessage]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    order_id: str | None = None
    amount_paise: Paise | None = None
    policy_result: PolicyResult | None = None
    timeline: list[TimelineStep] = Field(default_factory=list)
    reason: str | None = None


class AuditEventOut(_Response):
    id: UUID
    node: NodeName
    status: NodeStatus
    # PII-free summary of the transition (ids, decisions, versions, durations).
    detail: dict[str, JsonValue] = Field(default_factory=dict)
    at: AwareDatetime


class ReviewDecisionOut(_Response):
    action: ReviewAction
    reviewer_id: str
    note: str | None = None
    decided_at: AwareDatetime


class ReviewerCaseOut(CaseOut):
    """Reviewer view of a case. Only returned on reviewer-role routes."""

    customer_id: str
    audit_events: list[AuditEventOut]
    trace_id: str | None = None
    trace_url: str | None = None
    handoff_reason: str | None = None
    review: ReviewDecisionOut | None = None


# ---- Reviews ----------


class ReviewQueueItem(_Response):
    case_id: UUID
    customer_id: str
    status: CaseStatus  # pending_approval or handed_off
    amount_paise: Paise | None = None
    reason_category: ReasonCategory | None = None
    failed_rules: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class ReviewDecisionRequest(_Request):
    decision: ReviewAction
    note: str | None = Field(default=None, max_length=500)


class ReviewDecisionResponse(_Response):
    case_id: UUID
    status: CaseStatus
    # True when this exact decision was already recorded and nothing new happened.
    idempotent_replay: bool = False


class CompanySettingsOut(_Response):
    threshold_paise: Paise
    version: int
    updated_by: str
    updated_at: AwareDatetime


class CompanySettingsUpdate(_Request):
    threshold_paise: int = Field(ge=100)


class CompanyOverviewOut(_Response):
    pending_approval: int
    auto_processed: int
    approved: int
    denied: int
    rejected: int
    total_refunded_paise: Paise


class CompanyCaseOut(_Response):
    case_id: UUID
    customer: str
    order_id: str | None
    amount_paise: Paise | None
    reason: str | None
    status: CaseStatus
    decision_type: str | None
    created_at: AwareDatetime


class CustomerOrderItemOut(_Response):
    item_id: str
    name: str
    quantity: int
    unit_price_paise: int


class CustomerOrderOut(_Response):
    order_id: str
    ordered_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    total_paise: Paise
    payment_status: str
    items: list[CustomerOrderItemOut]
    can_refund: bool


class CustomerCaseSummaryOut(_Response):
    case_id: UUID
    order_id: str | None = None
    amount_paise: Paise | None = None
    status: Literal["processed", "under_review", "not_eligible", "rejected", "answered", "handed_off"]
    reply: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

