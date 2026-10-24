"""Shared enums. Values are the wire format (JSON, DB, prompts), so treat them as stable."""

from enum import StrEnum


class Intent(StrEnum):
    """Output of the triage agent."""

    REFUND_REQUEST = "refund_request"
    ORDER_STATUS = "order_status"
    POLICY_QUESTION = "policy_question"
    GENERAL = "general"
    UNCLEAR = "unclear"


class ReasonCategory(StrEnum):
    """Refund reason extracted by the intake agent.

    OTHER means "the customer's reason matches no eligible category". It exists so the
    LLM never has to force a bad fit; the policy engine rejects it because it is never
    listed in policy.yaml `eligible_reasons`.
    """

    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    NOT_DELIVERED = "not_delivered"
    DEFECTIVE = "defective"
    OTHER = "other"


class PolicyDecision(StrEnum):
    """The only values policy/engine.py may return. No LLM produces these."""

    AUTO_APPROVE = "AUTO_APPROVE"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


class RuleName(StrEnum):
    """One member per rule in the spec, in evaluation order."""

    ORDER_EXISTS_AND_OWNED = "order_exists_and_owned"
    PAYMENT_CAPTURED = "payment_captured"
    WITHIN_RETURN_WINDOW = "within_return_window"
    WITHIN_REFUNDABLE_AMOUNT = "within_refundable_amount"
    REASON_ELIGIBLE = "reason_eligible"
    # Uses policy.yaml max_refunds_per_customer_30d. Failing it escalates, never rejects.
    REFUND_VELOCITY = "refund_velocity"
    AUTO_APPROVE_ELIGIBLE = "auto_approve_eligible"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class CaseStatus(StrEnum):
    OPEN = "open"
    PENDING_APPROVAL = "pending_approval"  # graph paused at human_review
    PAID = "paid"  # payout done (auto-approved or reviewer-approved)
    DENIED = "denied"  # reviewer denied
    REJECTED = "rejected"  # policy engine rejected
    ANSWERED = "answered"  # non-refund question answered
    HANDED_OFF = "handed_off"  # LLM/provider failure; a human must follow up


class NodeName(StrEnum):
    TRIAGE = "triage"
    RESOLUTION = "resolution"
    REFUND_INTAKE = "refund_intake"
    LOAD_FACTS = "load_facts"
    POLICY_CHECK = "policy_check"
    HUMAN_REVIEW = "human_review"
    PAYOUT = "payout"
    RESPONDER = "responder"


class NodeStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # human_review paused the graph
    RESUMED = "resumed"


class ReviewAction(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class UserRole(StrEnum):
    CUSTOMER = "customer"
    REVIEWER = "reviewer"


class ProviderName(StrEnum):
    GROQ = "groq"
    OLLAMA = "ollama"
