"""Deterministic refund policy engine. Plain code: no LLM, no database, no clock, no network.

This is the only place that returns AUTO_APPROVE / REJECT / ESCALATE. Everything it needs
arrives in `RefundFacts` (loaded from the database in code) plus the reason category and the
validated `PolicyConfig`. The same inputs always give the same result.

Every rule is evaluated and reported, so the customer and the reviewer see all the reasons.
The final decision is the strongest outcome among the failed rules, with the precedence
REJECT > ESCALATE > AUTO_APPROVE.

    rule                        on failure
    order_exists_and_owned      REJECT   (the only rule reported when the order is not found)
    payment_captured            REJECT
    within_return_window        REJECT
    within_refundable_amount    REJECT   (also fails when the amount is zero)
    reason_eligible             REJECT
    refund_velocity             ESCALATE (policy.yaml max_refunds_per_customer_30d)
    auto_approve_eligible       ESCALATE for fraud flag, or amount >= threshold;
                                REJECT instead when amount >= threshold and
                                escalate_at_or_above_threshold is false

Reason strings are shown to customers. They never mention the fraud flag or other internal
signals.
"""

from dataclasses import dataclass

from app.contracts.decisions import PolicyResult, RuleResult
from app.contracts.enums import PaymentStatus, PolicyDecision, ReasonCategory, RuleName
from app.contracts.facts import RefundFacts
from app.policy.config import PolicyConfig

_STRENGTH = {
    PolicyDecision.AUTO_APPROVE: 0,
    PolicyDecision.ESCALATE: 1,
    PolicyDecision.REJECT: 2,
}


@dataclass(frozen=True)
class _Check:
    result: RuleResult
    on_fail: PolicyDecision


def _check(
    rule: RuleName,
    passed: bool,
    pass_reason: str,
    fail_reason: str,
    on_fail: PolicyDecision,
) -> _Check:
    return _Check(
        RuleResult(rule=rule, passed=passed, reason=pass_reason if passed else fail_reason), on_fail
    )


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def evaluate_refund(facts: RefundFacts, reason: ReasonCategory, config: PolicyConfig) -> PolicyResult:
    """Decide a refund. See the module docstring for the rules and precedence."""
    order = facts.order
    amount = facts.refund_amount_paise

    # 1. The order must exist and belong to the requester. Not-found and not-yours get the
    #    same answer so order ids cannot be probed. Nothing else can be checked without it.
    if order is None or order.customer_id != facts.requesting_customer_id:
        checks = [
            _check(
                RuleName.ORDER_EXISTS_AND_OWNED,
                False,
                "",
                "We couldn't find that order on your account.",
                PolicyDecision.REJECT,
            )
        ]
        return _build(checks, config, amount)

    checks = [
        _check(
            RuleName.ORDER_EXISTS_AND_OWNED,
            True,
            "The order was found on your account.",
            "",
            PolicyDecision.REJECT,
        )
    ]

    # 2. Payment must be captured.
    paid = order.payment_status is PaymentStatus.CAPTURED
    if order.payment_status is PaymentStatus.REFUNDED:
        not_paid = "The payment for this order has already been refunded."
    else:
        not_paid = "The payment for this order hasn't been completed, so there is nothing to refund."
    checks.append(
        _check(
            RuleName.PAYMENT_CAPTURED,
            paid,
            "The payment for this order was completed.",
            not_paid,
            PolicyDecision.REJECT,
        )
    )

    # 3. Return window: whole elapsed days since delivery (or since the order if it was never
    #    delivered). Day 30 is still inside a 30-day window; day 31 is outside. A reference
    #    date in the future counts as day 0.
    delivered = order.delivered_at is not None
    reference = (
        order.delivered_at
        if order.delivered_at is not None
        else order.expected_delivery_at or order.ordered_at
    )
    elapsed = max(0, (facts.as_of - reference).days)
    not_yet_due = (
        not delivered
        and order.expected_delivery_at is not None
        and order.expected_delivery_at > facts.as_of
    )
    in_window = not not_yet_due and elapsed <= config.return_window_days
    since = "Delivered" if delivered else "Ordered"
    checks.append(
        _check(
            RuleName.WITHIN_RETURN_WINDOW,
            in_window,
            f"{since} {_days(elapsed)} ago, within the {_days(config.return_window_days)} return window.",
            (
                "The expected delivery date has not passed yet."
                if not_yet_due
                else (
                    f"{since} {_days(elapsed)} ago, outside the "
                    f"{_days(config.return_window_days)} return window."
                )
            ),
            PolicyDecision.REJECT,
        )
    )

    # 4. Amount must be positive and no more than what is still refundable.
    remaining = max(order.total_paise - order.refunded_paise, 0)
    if amount <= 0:
        amount_fail = "There is nothing to refund for this request."
    elif remaining == 0:
        amount_fail = "This order has already been fully refunded."
    else:
        amount_fail = "The requested amount is more than what is still refundable on this order."
    checks.append(
        _check(
            RuleName.WITHIN_REFUNDABLE_AMOUNT,
            0 < amount <= remaining,
            "The amount is within what is still refundable on this order.",
            amount_fail,
            PolicyDecision.REJECT,
        )
    )

    # 5. Reason must be one the policy covers.
    label = reason.value.replace("_", " ")
    checks.append(
        _check(
            RuleName.REASON_ELIGIBLE,
            reason in config.eligible_reasons,
            f"A refund for a {label} order is covered by the policy.",
            "This reason isn't covered by the refund policy.",
            PolicyDecision.REJECT,
        )
    )

    # 6. Refund velocity: this refund must not push the customer past the 30-day limit.
    checks.append(
        _check(
            RuleName.REFUND_VELOCITY,
            facts.refunds_last_30d < config.max_refunds_per_customer_30d,
            "There are no unusual recent refunds on this account.",
            "There have been several recent refunds on this account, so this one needs a manual review.",
            PolicyDecision.ESCALATE,
        )
    )

    # 7. Auto-approval: strictly below the threshold, and no internal flag on the order.
    over_limit = amount >= config.auto_approve_below_paise
    flagged = order.fraud_flag
    over_limit_outcome = (
        PolicyDecision.ESCALATE if config.escalate_at_or_above_threshold else PolicyDecision.REJECT
    )
    if over_limit and not config.escalate_at_or_above_threshold:
        eligibility_fail = "The amount is above the limit for automatic refunds."
    elif over_limit:
        eligibility_fail = "The amount is above the automatic approval limit, so a reviewer must approve it."
    else:
        eligibility_fail = "This refund needs a manual review."
    checks.append(
        _check(
            RuleName.AUTO_APPROVE_ELIGIBLE,
            not over_limit and not flagged,
            "The amount is below the automatic approval limit.",
            eligibility_fail,
            over_limit_outcome if over_limit else PolicyDecision.ESCALATE,
        )
    )

    return _build(checks, config, amount)


def _build(checks: list[_Check], config: PolicyConfig, amount: int) -> PolicyResult:
    failed = [c.on_fail for c in checks if not c.result.passed]
    decision = max(failed, key=_STRENGTH.__getitem__) if failed else PolicyDecision.AUTO_APPROVE
    return PolicyResult(
        decision=decision,
        rules=tuple(c.result for c in checks),
        policy_version=config.version,
        amount_paise=amount,
    )
