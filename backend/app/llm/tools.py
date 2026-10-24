"""Read-only tools the LLM agents may call, and the per-agent allowlist.

Design rules:
  * The model chooses only the tool and its arguments. Who the customer is comes from the
    authenticated session and is bound into the tool here; it is not an argument, so a
    prompt-injected "look up customer X's order" has nothing to attach to.
  * Tools read through a READ ONLY database transaction and return a small view of the data:
    no names, emails, phone numbers, bank details, or internal flags.
  * An order that does not exist and an order that belongs to someone else look identical.
  * Allowlists default to deny. Nodes that are plain code (load_facts, policy_check,
    human_review, payout) and agents without tools get nothing. No tool can move money:
    only the payout node (Phase 7) will ever call the payout gateway.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.contracts.agents import OrderId
from app.contracts.enums import NodeName, PaymentStatus
from app.contracts.facts import OrderFacts
from app.db.queries import get_order_facts

NOT_FOUND_MESSAGE = "We couldn't find that order on your account."  # same wording as the policy engine


@dataclass(frozen=True)
class ToolContext:
    """Built once per request, by trusted code."""

    customer_id: str  # from the JWT, never from the model
    session_factory: Callable[[], AbstractContextManager[Session]]  # yields a read-only session


class LookupOrderStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: OrderId = Field(description="The order id the customer mentioned, for example ORD-1001.")


class OrderStatusItem(BaseModel):
    item_id: str
    name: str
    quantity: int
    unit_price_paise: int


class OrderStatusView(BaseModel):
    found: Literal[True] = True
    order_id: str
    payment_status: PaymentStatus
    delivery: Literal["delivered", "not_delivered_yet"]
    ordered_at: datetime
    delivered_at: datetime | None
    total_paise: int  # integer paise, INR
    refunded_paise: int
    items: list[OrderStatusItem]


class OrderNotFoundView(BaseModel):
    found: Literal[False] = False
    message: str = NOT_FOUND_MESSAGE


def to_status_view(facts: OrderFacts) -> OrderStatusView:
    return OrderStatusView(
        order_id=facts.order_id,
        payment_status=facts.payment_status,
        delivery="delivered" if facts.delivered_at else "not_delivered_yet",
        ordered_at=facts.ordered_at,
        delivered_at=facts.delivered_at,
        total_paise=facts.total_paise,
        refunded_paise=facts.refunded_paise,
        items=[
            OrderStatusItem(
                item_id=i.item_id, name=i.name, quantity=i.quantity, unit_price_paise=i.unit_price_paise
            )
            for i in facts.items
        ],
    )


def make_lookup_order_status(ctx: ToolContext) -> BaseTool:
    def lookup_order_status(order_id: str) -> str:
        with ctx.session_factory() as session:
            facts = get_order_facts(session, order_id, ctx.customer_id)
        view = to_status_view(facts) if facts else OrderNotFoundView()
        return view.model_dump_json()

    return StructuredTool.from_function(
        func=lookup_order_status,
        name="lookup_order_status",
        description=(
            "Look up one of the signed-in customer's orders by order id. Returns payment status, "
            "whether it was delivered, dates, items, the total and the amount already refunded "
            "(all money in integer paise, INR). Read-only."
        ),
        args_schema=LookupOrderStatusArgs,
        metadata={"read_only": True},
    )


# search_policy (RAG) joins the resolution agent's list in Phase 5.
_TOOL_FACTORIES: Mapping[str, Callable[[ToolContext], BaseTool]] = MappingProxyType(
    {"lookup_order_status": make_lookup_order_status}
)

TOOL_ALLOWLIST: Mapping[NodeName, frozenset[str]] = MappingProxyType(
    {
        NodeName.TRIAGE: frozenset(),
        NodeName.REFUND_INTAKE: frozenset(),
        NodeName.RESPONDER: frozenset(),
        NodeName.RESOLUTION: frozenset({"lookup_order_status"}),
    }
)


def tools_for(node: NodeName, ctx: ToolContext) -> list[BaseTool]:
    """The tools `node` may use. Any node not listed gets none."""
    return [_TOOL_FACTORIES[name](ctx) for name in sorted(TOOL_ALLOWLIST.get(node, frozenset()))]
