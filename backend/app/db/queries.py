"""Read-only queries shared by the order tool (Phase 3) and, later, the load_facts node.

Every query that returns customer data takes the customer id and filters on it in SQL, so a
caller cannot read another customer's order even by passing the right order id.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import PaymentStatus
from app.contracts.facts import OrderFacts
from app.contracts.facts import OrderItem as OrderItemFacts
from app.db.models import Order, OrderItem, Refund


def get_order_facts(session: Session, order_id: str, customer_id: str) -> OrderFacts | None:
    """The order, if it exists AND belongs to `customer_id`. Otherwise None, with no way to tell
    "does not exist" from "belongs to someone else"."""
    order = session.execute(
        select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
    ).scalar_one_or_none()
    if order is None:
        return None

    items = session.execute(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
    ).scalars()
    refunded = session.execute(
        select(func.coalesce(func.sum(Refund.amount_paise), 0)).where(Refund.order_id == order.id)
    ).scalar_one()
    # An order with no payment row has not been paid for: treat it as "created", never captured.
    payment_status = PaymentStatus(order.payment.status) if order.payment else PaymentStatus.CREATED

    return OrderFacts(
        order_id=order.id,
        customer_id=order.customer_id,
        payment_status=payment_status,
        ordered_at=order.ordered_at,
        delivered_at=order.delivered_at,
        expected_delivery_at=order.expected_delivery_at,
        total_paise=order.total_paise,
        refunded_paise=int(refunded),
        items=tuple(
            OrderItemFacts(
                item_id=i.id, name=i.name, quantity=i.quantity, unit_price_paise=i.unit_price_paise
            )
            for i in items
        ),
        fraud_flag=order.fraud_flag,
    )
