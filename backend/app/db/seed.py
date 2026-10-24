"""Demo seed data: 3 fake customers, 10 orders, payments, items and past refunds.

All people, emails, phone numbers and bank accounts are obviously fake. Dates are relative to
the moment you seed, so "delivered 5 days ago" stays true. Re-seed with --reset when the data
has aged. Run: `python -m app.db.seed [--reset]`.

Scenarios the data supports (decisions assume policy.yaml v1.0.0):
  ORD-1001  Asha    Rs 1,499 kettle, delivered 5d ago           -> AUTO_APPROVE (damaged)
  ORD-1002  Asha    Rs 89,999 laptop, delivered 3d ago          -> ESCALATE (above threshold)
  ORD-1003  Rahul   Rs 2,999 headphones, delivered 45d ago      -> REJECT (outside return window)
  ORD-1004  Rahul   Rs 3,499 shoes, payment only authorized     -> REJECT (payment not captured)
  ORD-1005  Asha    two items; the cables were already refunded -> Rs 2,500 left to refund (the
                    speaker); a whole-order claim is REJECTED as over the remaining amount
  ORD-1006..8 Priya three orders, each fully refunded recently  -> REJECT (already refunded)
  ORD-1009  Priya   Rs 999, delivered 2d ago                    -> ESCALATE (3 refunds in 30 days)
  ORD-1010  Rahul   Rs 1,299, delivered 4d ago, internal flag   -> ESCALATE (needs manual review)
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_policy, get_settings
from app.contracts.enums import PaymentStatus, ReasonCategory
from app.db.models import (
    AuditEvent,
    Case,
    Customer,
    Order,
    OrderItem,
    Payment,
    Payout,
    PolicySetting,
    Refund,
    RefundStatus,
    User,
)
from app.db.session import make_engine, session_scope
from app.logging_config import configure_logging
from app.security import hash_password

log = logging.getLogger(__name__)


def rs(rupees: int) -> int:
    """Whole rupees to paise."""
    return rupees * 100


@dataclass(frozen=True)
class SeedData:
    users: list[User]
    customers: list[Customer]
    orders: list[Order]
    items: list[OrderItem]
    payments: list[Payment]
    refunds: list[Refund]


def build_seed(now: datetime | None = None) -> SeedData:
    now = now or datetime.now(UTC)

    def ago(days: float) -> datetime:
        return now - timedelta(days=days)

    customers = [
        Customer(
            id="CUS-1001",
            name="Asha Verma",
            email="asha.verma@example.com",
            phone="9000000001",
            bank_account_number="111122223333",
        ),
        Customer(
            id="CUS-1002",
            name="Rahul Mehta",
            email="rahul.mehta@example.com",
            phone="9000000002",
            bank_account_number="444455556666",
        ),
        Customer(
            id="CUS-1003",
            name="Priya Nair",
            email="priya.nair@example.com",
            phone="9000000003",
            bank_account_number="777788889999",
        ),
    ]
    users = [
        User(
            id="USR-CUS-1001",
            email="asha.verma@example.com",
            password_hash=hash_password("demo-password"),
            role="customer",
            customer_id="CUS-1001",
        ),
        User(
            id="USR-CUS-1002",
            email="rahul.mehta@example.com",
            password_hash=hash_password("demo-password"),
            role="customer",
            customer_id="CUS-1002",
        ),
        User(
            id="USR-CUS-1003",
            email="priya.nair@example.com",
            password_hash=hash_password("demo-password"),
            role="customer",
            customer_id="CUS-1003",
        ),
        User(
            id="USR-REVIEWER-1001",
            email="reviewer@example.com",
            password_hash=hash_password("demo-password"),
            role="reviewer",
        ),
    ]

    # (order_id, customer_id, ordered_days_ago, delivered_days_ago, fraud_flag, payment_status,
    #  [(item_id, name, quantity, unit_price_rupees)])
    spec = [
        (
            "ORD-1001",
            "CUS-1001",
            8,
            5,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1001-1", "Electric kettle", 1, 1499)],
        ),
        (
            "ORD-1002",
            "CUS-1001",
            6,
            3,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1002-1", "14-inch laptop", 1, 89999)],
        ),
        (
            "ORD-1003",
            "CUS-1002",
            50,
            45,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1003-1", "Wireless headphones", 1, 2999)],
        ),
        (
            "ORD-1004",
            "CUS-1002",
            1,
            None,
            False,
            PaymentStatus.AUTHORIZED,
            [("ITM-1004-1", "Running shoes", 1, 3499)],
        ),
        (
            "ORD-1005",
            "CUS-1001",
            12,
            6,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1005-1", "USB-C cable", 2, 499), ("ITM-1005-2", "Bluetooth speaker", 1, 2500)],
        ),
        (
            "ORD-1006",
            "CUS-1003",
            30,
            25,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1006-1", "Hand mixer", 1, 1999)],
        ),
        (
            "ORD-1007",
            "CUS-1003",
            18,
            14,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1007-1", "Phone case", 1, 799)],
        ),
        ("ORD-1008", "CUS-1003", 9, 6, False, PaymentStatus.CAPTURED, [("ITM-1008-1", "Desk lamp", 1, 1299)]),
        (
            "ORD-1009",
            "CUS-1003",
            4,
            2,
            False,
            PaymentStatus.CAPTURED,
            [("ITM-1009-1", "Water bottle", 1, 999)],
        ),
        ("ORD-1010", "CUS-1002", 7, 4, True, PaymentStatus.CAPTURED, [("ITM-1010-1", "Backpack", 1, 1299)]),
    ]

    orders: list[Order] = []
    items: list[OrderItem] = []
    payments: list[Payment] = []
    for order_id, customer_id, ordered, delivered, fraud, pay_status, lines in spec:
        total = sum(rs(price) * qty for _, _, qty, price in lines)
        orders.append(
            Order(
                id=order_id,
                customer_id=customer_id,
                ordered_at=ago(ordered),
                delivered_at=ago(delivered) if delivered is not None else None,
                total_paise=total,
                fraud_flag=fraud,
            )
        )
        items += [
            OrderItem(id=item_id, order_id=order_id, name=name, quantity=qty, unit_price_paise=rs(price))
            for item_id, name, qty, price in lines
        ]
        payments.append(
            Payment(
                id=f"PAY-{order_id[4:]}",
                order_id=order_id,
                status=pay_status.value,
                amount_paise=total,
                captured_at=ago(ordered) if pay_status is PaymentStatus.CAPTURED else None,
            )
        )

    refunds = [
        # Asha: the cables from ORD-1005 (2 x Rs 499) were refunded 10 days ago.
        Refund(
            id="RFD-0001",
            order_id="ORD-1005",
            customer_id="CUS-1001",
            amount_paise=rs(998),
            reason_category=ReasonCategory.DAMAGED.value,
            status=RefundStatus.PAID.value,
            created_at=ago(10),
        ),
        # Priya: three fully refunded orders inside the last 30 days.
        Refund(
            id="RFD-0002",
            order_id="ORD-1006",
            customer_id="CUS-1003",
            amount_paise=rs(1999),
            reason_category=ReasonCategory.DEFECTIVE.value,
            status=RefundStatus.PAID.value,
            created_at=ago(20),
        ),
        Refund(
            id="RFD-0003",
            order_id="ORD-1007",
            customer_id="CUS-1003",
            amount_paise=rs(799),
            reason_category=ReasonCategory.WRONG_ITEM.value,
            status=RefundStatus.PAID.value,
            created_at=ago(12),
        ),
        Refund(
            id="RFD-0004",
            order_id="ORD-1008",
            customer_id="CUS-1003",
            amount_paise=rs(1299),
            reason_category=ReasonCategory.DAMAGED.value,
            status=RefundStatus.PAID.value,
            created_at=ago(4),
        ),
    ]
    return SeedData(users, customers, orders, items, payments, refunds)


def reset_tables(session: Session) -> None:
    """Delete all rows from the Phase 1 tables, children first."""
    for model in (
        AuditEvent,
        Payout,
        Case,
        PolicySetting,
        Refund,
        Payment,
        OrderItem,
        Order,
        User,
        Customer,
    ):
        session.execute(delete(model))


def build_company_seed(session: Session, now: datetime) -> SeedData:
    """Translate the supplied company fixture into the existing normalized tables."""
    payload = json.loads(
        (Path(__file__).with_name("demo_company_data.json")).read_text(encoding="utf-8")
    )
    customers_by_email = {
        customer.email: customer for customer in session.scalars(select(Customer)).all()
    }
    fixture_customers = {item["id"]: item for item in payload["customers"]}
    customer_ids = {
        fixture_id: customers_by_email[item["email"]].id
        for fixture_id, item in fixture_customers.items()
        if item["email"] in customers_by_email
    }
    if len(customer_ids) != len(fixture_customers):
        raise ValueError("demo company fixture does not match all seeded customer accounts")
    products = {item["id"]: item["name"] for item in payload["products"]}

    def relative_days(value: int | float | None, *, future: bool = False) -> datetime | None:
        if value is None:
            return None
        if future:
            return now + timedelta(days=value)
        return now - timedelta(days=value)

    orders: list[Order] = []
    items: list[OrderItem] = []
    payments: list[Payment] = []
    refunds: list[Refund] = []
    for order in payload["orders"]:
        customer_id = customer_ids[order["customer_id"]]
        lines = order["items"]
        subtotal = sum(line["qty"] * line["unit_price_paise"] for line in lines)
        total = subtotal + order.get("shipping_paise", 0)
        orders.append(
            Order(
                id=order["id"],
                customer_id=customer_id,
                ordered_at=relative_days(order["ordered_days_ago"]),
                delivered_at=relative_days(order["delivered_days_ago"]),
                expected_delivery_at=relative_days(
                    order.get("expected_delivery_days_from_now"), future=True
                ),
                total_paise=total,
            )
        )
        items.extend(
            OrderItem(
                id=f"{order['id']}-{index}",
                order_id=order["id"],
                name=products[line["product_id"]],
                quantity=line["qty"],
                unit_price_paise=line["unit_price_paise"],
            )
            for index, line in enumerate(lines, 1)
        )
        payment = order["payment"]
        payment_status = PaymentStatus(payment["status"])
        payments.append(
            Payment(
                id=f"PAY-{order['id']}",
                order_id=order["id"],
                status=payment_status.value,
                amount_paise=total,
                captured_at=relative_days(order["ordered_days_ago"])
                if payment_status is PaymentStatus.CAPTURED
                else None,
            )
        )
        refunds.extend(
            Refund(
                id=f"RFD-{order['id']}-{index}",
                order_id=order["id"],
                customer_id=customer_id,
                amount_paise=refund["amount_paise"],
                reason_category=refund["reason"],
                status=RefundStatus.PAID.value,
                created_at=relative_days(refund["days_ago"]),
            )
            for index, refund in enumerate(order.get("refunds", []), 1)
        )
    return SeedData([], [], orders, items, payments, refunds)


def seed(
    session: Session, now: datetime | None = None, *, company_fixture: bool = False
) -> int:
    """Insert seed rows that are missing. Existing rows are left untouched. Returns rows added."""
    now = now or datetime.now(UTC)
    base = build_seed(now)
    if company_fixture:
        data = SeedData(base.users, base.customers, [], [], [], [])
    else:
        data = base
    added = 0
    # Parents before children so foreign keys resolve; flush between groups.
    for group in (data.customers, data.users):
        for row in group:
            if session.get(type(row), row.id) is None:
                session.add(row)
                if not isinstance(row, User):
                    added += 1
        session.flush()
    if company_fixture:
        fixture = build_company_seed(session, now)
        data = SeedData(
            data.users,
            data.customers,
            fixture.orders,
            fixture.items,
            fixture.payments,
            fixture.refunds,
        )
    for group in (data.orders, data.items, data.payments, data.refunds):
        for row in group:
            if session.get(type(row), row.id) is None:
                session.add(row)
                added += 1
        session.flush()
    if session.scalar(select(PolicySetting).order_by(PolicySetting.version.desc())) is None:
        session.add(
            PolicySetting(
                threshold_paise=get_policy().auto_approve_below_paise,
                version=1,
                updated_by="seed",
            )
        )
        session.flush()
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo data.")
    parser.add_argument(
        "--reset", action="store_true", help="delete existing rows first (never in production)"
    )
    parser.add_argument(
        "--company-fixture",
        action="store_true",
        help="load the supplied 15-order company fixture instead of the legacy order scenarios",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.reset and settings.app_env == "production":
        raise SystemExit("Refusing to reset tables when APP_ENV=production.")

    engine = make_engine()
    with session_scope(engine) as session:
        if args.reset:
            reset_tables(session)
        added = seed(session, company_fixture=args.company_fixture)
    log.info(
        "seed complete",
        extra={"rows_added": added, "reset": args.reset, "company_fixture": args.company_fixture},
    )


if __name__ == "__main__":
    main()
