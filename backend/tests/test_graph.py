"""Phase 4: graph routing and human-review contracts."""

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.agents import TriageOutput
from app.contracts.decisions import PolicyResult, RuleResult
from app.contracts.enums import (
    CaseStatus,
    Intent,
    PolicyDecision,
    RuleName,
)
from app.contracts.state import ChatMessage
from app.graph.workflow import (
    GraphState,
    route_after_policy,
    route_after_review,
    route_after_triage,
)


def _state(**overrides: object) -> GraphState:
    return {
        "case_id": uuid4(),
        "thread_id": "thread-1",
        "customer_id": "CUS-1",
        "messages": [ChatMessage(role="customer", content="Please help")],
        **overrides,
    }


def _policy(decision: PolicyDecision) -> PolicyResult:
    return PolicyResult(
        decision=decision,
        rules=(
            RuleResult(
                rule=RuleName.AUTO_APPROVE_ELIGIBLE,
                passed=decision is PolicyDecision.AUTO_APPROVE,
                reason="The amount is within the automatic approval limit.",
            ),
        ),
        policy_version="1.0.0",
        amount_paise=100,
    )


def test_triage_routes_refunds_and_non_refunds():
    refund = TriageOutput(intent=Intent.REFUND_REQUEST, order_id="ORD-1", confidence=0.9)
    unclear = TriageOutput(intent=Intent.UNCLEAR, order_id=None, confidence=0.9)
    assert route_after_triage(_state(triage=refund)) == "refund_intake"
    assert route_after_triage(_state(triage=unclear)) == "resolution"


def test_policy_routes_to_terminal_or_review_paths():
    assert route_after_policy(_state(policy_result=_policy(PolicyDecision.REJECT))) == "responder"
    assert route_after_policy(_state(policy_result=_policy(PolicyDecision.ESCALATE))) == "human_review"
    assert route_after_policy(_state(policy_result=_policy(PolicyDecision.AUTO_APPROVE))) == "payout"


def test_review_approval_routes_to_payout_and_deny_to_responder():
    from app.contracts.enums import ReviewAction
    from app.contracts.state import ReviewDecision

    approved = ReviewDecision(
        action=ReviewAction.APPROVE,
        reviewer_id="reviewer-1",
        decided_at=datetime.now(UTC),
    )
    denied = approved.model_copy(update={"action": ReviewAction.DENY})
    assert route_after_review(_state(review=approved, status=CaseStatus.OPEN)) == "payout"
    assert route_after_review(_state(review=denied, status=CaseStatus.DENIED)) == "responder"
