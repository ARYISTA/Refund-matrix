"""Phase 2: policy engine. Table-driven, with the boundaries called out in the spec."""

import ast
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app.contracts.decisions import PolicyResult
from app.contracts.enums import PaymentStatus, PolicyDecision, ReasonCategory, RuleName
from app.contracts.facts import OrderFacts, OrderItem, RefundFacts
from app.db.seed import build_seed
from app.policy.config import DEFAULT_POLICY_PATH, PolicyConfig, load_policy
from app.policy.engine import evaluate_refund

AUTO = PolicyDecision.AUTO_APPROVE
REJECT = PolicyDecision.REJECT
ESCALATE = PolicyDecision.ESCALATE

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
BIG_TOTAL = 100_000_000  # Rs 10 lakh, so the order total never limits the amount under test
POLICY = load_policy()


def config(**overrides: object) -> PolicyConfig:
    data = yaml.safe_load(DEFAULT_POLICY_PATH.read_text())
    return PolicyConfig.model_validate({**data, **overrides})


def facts(
    amount: int = 100_000,
    *,
    total: int = BIG_TOTAL,
    refunded: int = 0,
    since_delivery: timedelta | None = timedelta(days=5),
    ordered_ago: timedelta = timedelta(days=8),
    payment: PaymentStatus = PaymentStatus.CAPTURED,
    owner: str = "CUS-1",
    requester: str = "CUS-1",
    fraud: bool = False,
    refunds_30d: int = 0,
    order_exists: bool = True,
    expected_delivery_in: timedelta | None = None,
) -> RefundFacts:
    order = None
    if order_exists:
        order = OrderFacts(
            order_id="ORD-1",
            customer_id=owner,
            payment_status=payment,
            ordered_at=NOW - ordered_ago,
            delivered_at=None if since_delivery is None else NOW - since_delivery,
            expected_delivery_at=(
                NOW + expected_delivery_in if expected_delivery_in is not None else None
            ),
            total_paise=total,
            refunded_paise=refunded,
            items=(OrderItem(item_id="ITM-1", name="Thing", quantity=1, unit_price_paise=total),),
            fraud_flag=fraud,
        )
    return RefundFacts(
        requesting_customer_id=requester,
        order=order,
        refund_amount_paise=amount,
        refunds_last_30d=refunds_30d,
        as_of=NOW,
    )


def decide(
    f: RefundFacts, reason: ReasonCategory = ReasonCategory.DAMAGED, cfg: PolicyConfig = POLICY
) -> PolicyResult:
    return evaluate_refund(f, reason, cfg)


def rule(result: PolicyResult, name: RuleName):
    return next(r for r in result.rules if r.rule is name)


def failed(result: PolicyResult) -> set[RuleName]:
    return {r.rule for r in result.rules if not r.passed}


# ---- spec boundary: auto-approve threshold (strictly less than Rs 5,000) --------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (1, AUTO),
        (499_999, AUTO),
        (500_000, ESCALATE),
        (500_001, ESCALATE),
        (8_999_900, ESCALATE),
    ],
)
def test_threshold_boundaries(amount: int, expected: PolicyDecision):
    result = decide(facts(amount))
    assert result.decision is expected
    assert result.amount_paise == amount
    if expected is AUTO:
        assert failed(result) == set()
    else:
        assert failed(result) == {RuleName.AUTO_APPROVE_ELIGIBLE}


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(499_999, AUTO), (500_000, REJECT), (500_001, REJECT)],
)
def test_threshold_boundaries_reject_when_escalation_is_off(amount: int, expected: PolicyDecision):
    cfg = config(escalate_at_or_above_threshold=False)
    assert decide(facts(amount), cfg=cfg).decision is expected


def test_threshold_comes_from_config_not_code():
    cfg = config(auto_approve_below_paise=100_000)
    assert decide(facts(99_999), cfg=cfg).decision is AUTO
    assert decide(facts(100_000), cfg=cfg).decision is ESCALATE
    assert decide(facts(499_999), cfg=cfg).decision is ESCALATE


# ---- spec boundary: return window (30 days) ---------------------------------------------------


@pytest.mark.parametrize(
    ("since_delivery", "expected"),
    [
        (timedelta(0), AUTO),
        (timedelta(days=29), AUTO),
        (timedelta(days=30), AUTO),  # day 30 is still inside the window
        (timedelta(days=30, hours=23, minutes=59), AUTO),  # still 30 whole days
        (timedelta(days=31), REJECT),  # day 31 is outside
        (timedelta(days=31, seconds=1), REJECT),
        (timedelta(days=45), REJECT),
    ],
)
def test_return_window_boundaries(since_delivery: timedelta, expected: PolicyDecision):
    result = decide(facts(since_delivery=since_delivery))
    assert result.decision is expected
    assert rule(result, RuleName.WITHIN_RETURN_WINDOW).passed is (expected is AUTO)
    if expected is REJECT:
        assert failed(result) == {RuleName.WITHIN_RETURN_WINDOW}


def test_return_window_comes_from_config():
    cfg = config(return_window_days=7)
    assert decide(facts(since_delivery=timedelta(days=7)), cfg=cfg).decision is AUTO
    assert decide(facts(since_delivery=timedelta(days=8)), cfg=cfg).decision is REJECT


def test_window_counts_from_delivery_not_order_date():
    # Ordered long ago but delivered yesterday: inside the window.
    assert decide(facts(since_delivery=timedelta(days=1), ordered_ago=timedelta(days=100))).decision is AUTO


def test_window_falls_back_to_order_date_when_never_delivered():
    inside = decide(facts(since_delivery=None, ordered_ago=timedelta(days=10)), ReasonCategory.NOT_DELIVERED)
    outside = decide(facts(since_delivery=None, ordered_ago=timedelta(days=31)), ReasonCategory.NOT_DELIVERED)
    assert inside.decision is AUTO
    assert outside.decision is REJECT
    assert "Ordered" in rule(outside, RuleName.WITHIN_RETURN_WINDOW).reason


def test_future_delivery_date_counts_as_day_zero():
    assert decide(facts(since_delivery=timedelta(days=-2))).decision is AUTO


def test_undelivered_order_is_rejected_before_expected_delivery():
    result = decide(
        facts(
            since_delivery=None,
            ordered_ago=timedelta(days=3),
            expected_delivery_in=timedelta(days=2),
        ),
        ReasonCategory.NOT_DELIVERED,
    )
    assert result.decision is REJECT
    assert rule(result, RuleName.WITHIN_RETURN_WINDOW).passed is False
    assert "expected delivery" in rule(result, RuleName.WITHIN_RETURN_WINDOW).reason


# ---- spec boundary: already-refunded orders ------------------------------------------------------


@pytest.mark.parametrize(
    ("refunded", "amount", "expected", "needle"),
    [
        (0, 1_000_000, AUTO, None),  # whole order, nothing refunded yet
        (1, 1_000_000, REJECT, "more than what is still refundable"),  # one paisa over
        (999_999, 1, AUTO, None),  # exactly what is left
        (999_999, 2, REJECT, "more than what is still refundable"),
        (1_000_000, 1, REJECT, "already been fully refunded"),
        (1_000_000, 1_000_000, REJECT, "already been fully refunded"),
        (1_500_000, 1, REJECT, "already been fully refunded"),  # bad data (over-refunded) fails closed
        (400_000, 600_000, AUTO, None),
        (400_000, 600_001, REJECT, "more than what is still refundable"),
    ],
)
def test_no_double_refunds(refunded: int, amount: int, expected: PolicyDecision, needle: str | None):
    # total Rs 10,000 so that partial refunds below the Rs 5,000 limit are exercised too
    result = decide(facts(amount, total=1_000_000, refunded=refunded))
    amount_rule = rule(result, RuleName.WITHIN_REFUNDABLE_AMOUNT)
    if expected is AUTO:
        assert amount_rule.passed
    else:
        assert result.decision is REJECT
        assert not amount_rule.passed
        assert needle is not None and needle in amount_rule.reason


def test_the_remaining_amount_boundary_exactly():
    base = {"total": 1_000_000, "refunded": 250_000}
    assert rule(decide(facts(750_000, **base)), RuleName.WITHIN_REFUNDABLE_AMOUNT).passed
    assert not rule(decide(facts(750_001, **base)), RuleName.WITHIN_REFUNDABLE_AMOUNT).passed


def test_zero_amount_is_rejected():
    result = decide(facts(0))
    assert result.decision is REJECT
    assert "nothing to refund" in rule(result, RuleName.WITHIN_REFUNDABLE_AMOUNT).reason


# ---- payment status ----------


@pytest.mark.parametrize("status", list(PaymentStatus))
def test_only_captured_payments_are_refundable(status: PaymentStatus):
    result = decide(facts(payment=status))
    if status is PaymentStatus.CAPTURED:
        assert result.decision is AUTO
    else:
        assert result.decision is REJECT
        assert failed(result) == {RuleName.PAYMENT_CAPTURED}


# ---- order existence and ownership ----------


def test_missing_order_is_rejected_with_a_single_rule():
    result = decide(facts(order_exists=False, amount=0))
    assert result.decision is REJECT
    assert [r.rule for r in result.rules] == [RuleName.ORDER_EXISTS_AND_OWNED]
    assert not result.rules[0].passed


def test_someone_elses_order_looks_the_same_as_a_missing_one():
    other = decide(facts(owner="CUS-2", requester="CUS-1"))
    missing = decide(facts(order_exists=False))
    assert other.decision is REJECT
    assert other.rules == missing.rules  # cannot probe which order ids exist
    assert other.amount_paise == facts().refund_amount_paise


# ---- reasons ----------


@pytest.mark.parametrize("reason", list(ReasonCategory))
def test_reason_eligibility_follows_policy(reason: ReasonCategory):
    result = decide(facts(), reason)
    if reason is ReasonCategory.OTHER:
        assert result.decision is REJECT
        assert failed(result) == {RuleName.REASON_ELIGIBLE}
    else:
        assert result.decision is AUTO


def test_eligible_reasons_come_from_config():
    cfg = config(eligible_reasons=["damaged"])
    assert decide(facts(), ReasonCategory.DAMAGED, cfg).decision is AUTO
    assert decide(facts(), ReasonCategory.WRONG_ITEM, cfg).decision is REJECT


# ---- refund velocity (max_refunds_per_customer_30d) ----------


@pytest.mark.parametrize(
    ("recent", "expected"),
    [(0, AUTO), (1, AUTO), (2, AUTO), (3, ESCALATE), (4, ESCALATE), (50, ESCALATE)],
)
def test_velocity_limit_of_three(recent: int, expected: PolicyDecision):
    result = decide(facts(refunds_30d=recent))
    assert result.decision is expected
    if expected is ESCALATE:
        assert failed(result) == {RuleName.REFUND_VELOCITY}


def test_velocity_limit_comes_from_config():
    assert decide(facts(refunds_30d=4), cfg=config(max_refunds_per_customer_30d=5)).decision is AUTO
    assert decide(facts(refunds_30d=5), cfg=config(max_refunds_per_customer_30d=5)).decision is ESCALATE
    assert decide(facts(refunds_30d=0), cfg=config(max_refunds_per_customer_30d=0)).decision is ESCALATE


# ---- fraud flag ----------


def test_flagged_order_escalates_even_for_a_tiny_amount():
    result = decide(facts(100, fraud=True))
    assert result.decision is ESCALATE
    assert failed(result) == {RuleName.AUTO_APPROVE_ELIGIBLE}


def test_flagged_order_still_escalates_when_threshold_escalation_is_off():
    assert (
        decide(facts(100, fraud=True), cfg=config(escalate_at_or_above_threshold=False)).decision is ESCALATE
    )


# ---- precedence: REJECT > ESCALATE > AUTO_APPROVE ----------


@pytest.mark.parametrize(
    ("kwargs", "reason", "cfg_overrides", "expected"),
    [
        # reject beats escalate
        ({"fraud": True}, ReasonCategory.OTHER, {}, REJECT),
        ({"refunds_30d": 9, "payment": PaymentStatus.AUTHORIZED}, ReasonCategory.DAMAGED, {}, REJECT),
        ({"amount": 9_000_000, "since_delivery": timedelta(days=60)}, ReasonCategory.DAMAGED, {}, REJECT),
        (
            {"fraud": True, "amount": 500_000},
            ReasonCategory.DAMAGED,
            {"escalate_at_or_above_threshold": False},
            REJECT,
        ),
        # escalate beats auto-approve
        ({"fraud": True, "refunds_30d": 3}, ReasonCategory.DAMAGED, {}, ESCALATE),
        ({"refunds_30d": 3, "amount": 600_000}, ReasonCategory.DAMAGED, {}, ESCALATE),
        # nothing failed
        ({}, ReasonCategory.DEFECTIVE, {}, AUTO),
    ],
)
def test_precedence(kwargs: dict, reason: ReasonCategory, cfg_overrides: dict, expected: PolicyDecision):
    amount = kwargs.pop("amount", 100_000)
    assert decide(facts(amount, **kwargs), reason, config(**cfg_overrides)).decision is expected


def test_every_failing_rule_is_reported_not_just_the_first():
    result = decide(
        facts(
            2_000_000,
            total=1_000_000,
            refunded=100_000,
            since_delivery=timedelta(days=40),
            payment=PaymentStatus.FAILED,
            fraud=True,
            refunds_30d=7,
        ),
        ReasonCategory.OTHER,
    )
    assert result.decision is REJECT
    assert failed(result) == set(RuleName) - {RuleName.ORDER_EXISTS_AND_OWNED}


# ---- result shape ----------


def test_rules_are_reported_in_spec_order_for_an_existing_order():
    result = decide(facts())
    assert [r.rule for r in result.rules] == list(RuleName)
    assert all(r.reason for r in result.rules)


def test_result_is_stamped_and_serializable():
    cfg = config(version="9.9.9")
    result = decide(facts(123_456), cfg=cfg)
    assert result.policy_version == "9.9.9"
    assert result.amount_paise == 123_456
    assert result.currency == "INR"
    assert PolicyResult.model_validate_json(result.model_dump_json()) == result


def test_engine_is_deterministic_and_does_not_touch_its_inputs():
    f = facts(499_999, fraud=True)
    snapshot = f.model_dump_json()
    assert decide(f) == decide(f)
    assert f.model_dump_json() == snapshot


def test_reasons_never_leak_internal_signals():
    scenarios = [
        facts(100, fraud=True),
        facts(9_000_000, fraud=True),
        facts(100, fraud=True, refunds_30d=5),
        facts(100, fraud=True, since_delivery=timedelta(days=90)),
        facts(order_exists=False),
        facts(owner="CUS-9"),
    ]
    banned = ("fraud", "flag", "suspic", "risk", "blacklist", "abuse")
    for scenario in scenarios:
        for cfg in (POLICY, config(escalate_at_or_above_threshold=False)):
            for r in decide(scenario, cfg=cfg).rules:
                assert not any(word in r.reason.lower() for word in banned), r.reason


# ---- property test: random scenarios against an independent oracle ----------


def _oracle(f: RefundFacts, reason: ReasonCategory, cfg: PolicyConfig) -> PolicyDecision:
    """Written as a flat decision table, deliberately not sharing code with the engine."""
    o = f.order
    if o is None or o.customer_id != f.requesting_customer_id:
        return REJECT
    ref = o.delivered_at or o.ordered_at
    days = max(0, (f.as_of - ref).days)
    remaining = max(o.total_paise - o.refunded_paise, 0)
    over = f.refund_amount_paise >= cfg.auto_approve_below_paise
    hard_fail = (
        o.payment_status is not PaymentStatus.CAPTURED
        or days > cfg.return_window_days
        or f.refund_amount_paise <= 0
        or f.refund_amount_paise > remaining
        or reason not in cfg.eligible_reasons
        or (over and not cfg.escalate_at_or_above_threshold)
    )
    if hard_fail:
        return REJECT
    if o.fraud_flag or over or f.refunds_last_30d >= cfg.max_refunds_per_customer_30d:
        return ESCALATE
    return AUTO


def test_random_scenarios_match_the_oracle_and_invariants():
    rng = random.Random(20261023)
    eligible = [r for r in ReasonCategory if r is not ReasonCategory.OTHER]
    statuses = list(PaymentStatus)
    counts = {AUTO: 0, REJECT: 0, ESCALATE: 0}
    for _ in range(4000):
        # Most draws are "healthy" so every outcome is well represented; the rest are hostile.
        total = rng.choice([1, 100, 499_999, 500_000, 1_000_000, 9_000_000])
        refunded = rng.choice([0, 0, 0, 0, 0, 0, rng.randint(0, total), total, total + 5])
        amount = rng.choice([0, 1, 499_998, 499_999, 500_000, 500_001, rng.randint(0, 10_000_000)])
        if rng.random() < 0.7:
            amount = min(amount, max(total - refunded, 1))
        f = facts(
            amount,
            total=total,
            refunded=refunded,
            since_delivery=rng.choice(
                [None, timedelta(days=rng.choice([-1, 0, 5, 29, 30, 30, 31, 90]), hours=rng.randint(0, 23))]
            ),
            ordered_ago=timedelta(days=rng.choice([1, 20, 31, 60])),
            payment=rng.choice(statuses if rng.random() < 0.1 else [PaymentStatus.CAPTURED]),
            owner="CUS-1" if rng.random() < 0.95 else "CUS-2",
            fraud=rng.random() < 0.1,
            refunds_30d=rng.choice([0, 0, 0, 1, 2, 3, 4]),
            order_exists=rng.random() < 0.97,
        )
        reason = rng.choice(eligible) if rng.random() < 0.9 else ReasonCategory.OTHER
        cfg = config(
            escalate_at_or_above_threshold=rng.random() < 0.7,
            max_refunds_per_customer_30d=rng.choice([0, 1, 3, 5]),
            return_window_days=rng.choice([7, 30, 60]),
        )
        result = evaluate_refund(f, reason, cfg)
        counts[result.decision] += 1
        assert result.decision is _oracle(f, reason, cfg)
        if result.decision is AUTO:  # money moves only when every condition holds
            o = f.order
            assert o is not None
            assert o.payment_status is PaymentStatus.CAPTURED
            assert 0 < f.refund_amount_paise < cfg.auto_approve_below_paise
            assert f.refund_amount_paise <= o.total_paise - o.refunded_paise
            assert not o.fraud_flag and o.customer_id == f.requesting_customer_id
    assert all(n > 300 for n in counts.values()), counts  # the sample actually covers every outcome


# ---- the engine is plain code ----------


def test_engine_has_no_io_clock_randomness_or_llm_imports():
    tree = ast.parse((Path(__file__).parents[1] / "app/policy/engine.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed_prefixes = ("dataclasses", "app.contracts", "app.policy.config")
    assert all(name.startswith(allowed_prefixes) for name in imported), imported
    # Calls that would make the result depend on anything but its inputs.
    called = {
        n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {"now", "utcnow", "today", "random", "choice", "request", "get", "post"}


# ---- seeded demo orders produce the intended outcomes ----------


def _seed_facts(order_id: str, items: list[str] | None = None, now: datetime = NOW) -> RefundFacts:
    """What load_facts (Phase 4) will do, using the in-memory seed instead of the database."""
    data = build_seed(now)
    row = next(o for o in data.orders if o.id == order_id)
    order_items = [i for i in data.items if i.order_id == order_id]
    refunded = sum(r.amount_paise for r in data.refunds if r.order_id == order_id)
    payment = next(p for p in data.payments if p.order_id == order_id)
    recent = sum(
        1
        for r in data.refunds
        if r.customer_id == row.customer_id and r.created_at >= now - timedelta(days=30)
    )
    chosen = [i for i in order_items if items is None or i.id in items]
    return RefundFacts(
        requesting_customer_id=row.customer_id,
        order=OrderFacts(
            order_id=row.id,
            customer_id=row.customer_id,
            payment_status=PaymentStatus(payment.status),
            ordered_at=row.ordered_at,
            delivered_at=row.delivered_at,
            total_paise=row.total_paise,
            refunded_paise=refunded,
            items=tuple(
                OrderItem(item_id=i.id, name=i.name, quantity=i.quantity, unit_price_paise=i.unit_price_paise)
                for i in order_items
            ),
            fraud_flag=row.fraud_flag,
        ),
        refund_amount_paise=sum(i.quantity * i.unit_price_paise for i in chosen),
        refunds_last_30d=recent,
        as_of=now,
    )


@pytest.mark.parametrize(
    ("order_id", "items", "reason", "expected", "failed_rules"),
    [
        ("ORD-1001", None, ReasonCategory.DAMAGED, AUTO, set()),
        ("ORD-1002", None, ReasonCategory.DEFECTIVE, ESCALATE, {RuleName.AUTO_APPROVE_ELIGIBLE}),
        ("ORD-1003", None, ReasonCategory.DAMAGED, REJECT, {RuleName.WITHIN_RETURN_WINDOW}),
        ("ORD-1004", None, ReasonCategory.NOT_DELIVERED, REJECT, {RuleName.PAYMENT_CAPTURED}),
        (
            "ORD-1005",
            ["ITM-1005-2"],
            ReasonCategory.DEFECTIVE,
            AUTO,
            set(),
        ),  # the speaker is still refundable
        (
            "ORD-1005",
            None,
            ReasonCategory.DAMAGED,
            REJECT,
            {RuleName.WITHIN_REFUNDABLE_AMOUNT},
        ),  # whole order is not
        (
            "ORD-1006",
            None,
            ReasonCategory.DAMAGED,
            REJECT,
            {RuleName.WITHIN_REFUNDABLE_AMOUNT, RuleName.REFUND_VELOCITY},
        ),
        ("ORD-1009", None, ReasonCategory.DAMAGED, ESCALATE, {RuleName.REFUND_VELOCITY}),
        ("ORD-1010", None, ReasonCategory.DAMAGED, ESCALATE, {RuleName.AUTO_APPROVE_ELIGIBLE}),
    ],
)
def test_seeded_demo_orders(order_id, items, reason, expected, failed_rules):
    result = decide(_seed_facts(order_id, items), reason)
    assert result.decision is expected
    assert failed(result) == failed_rules
