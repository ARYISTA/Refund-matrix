"""Facts loaded from the database by the load_facts node and consumed by the policy engine.

Nothing in here comes from the client or an LLM. Datetimes must be timezone-aware so the
return-window boundary (day 30 vs day 31) can never be skewed by a naive/aware mix-up.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.contracts.enums import PaymentStatus
from app.contracts.money import Paise, PositivePaise


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, strict=True)
    unit_price_paise: Paise

    @property
    def line_total_paise(self) -> int:
        # Plain property, deliberately not a computed_field: it must not be serialized,
        # or the extra="forbid" round trip through checkpoints would fail.
        return self.quantity * self.unit_price_paise


class OrderFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str = Field(min_length=1, max_length=40)
    customer_id: str = Field(min_length=1, max_length=40)
    payment_status: PaymentStatus
    ordered_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    expected_delivery_at: AwareDatetime | None = None
    total_paise: PositivePaise
    refunded_paise: Paise  # sum of refunds already paid or approved for this order
    items: tuple[OrderItem, ...]
    fraud_flag: bool = False


class RefundFacts(BaseModel):
    """Everything the policy engine needs. `as_of` is injected so tests control the clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requesting_customer_id: str = Field(min_length=1, max_length=40)  # from the JWT
    order: OrderFacts | None  # None => no such order id in the database
    refund_amount_paise: Paise  # computed in code from DB items
    refunds_last_30d: int = Field(ge=0, strict=True)
    as_of: AwareDatetime
