"""Structured outputs the LLM agents must return. Every LLM response is validated against
these models; on failure the caller retries once, then hands the case to a human.

Both models use extra="forbid" on purpose: if the model invents an `amount` field, the
output is invalid. LLMs extract intent and fields; money comes from the database.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.contracts.enums import Intent, ReasonCategory

# Identifiers extracted from customer text. The strict pattern keeps injected prose,
# SQL fragments and whitespace tricks out of anything that later touches a query.
_ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
OrderId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=_ID_PATTERN)]
ItemId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=_ID_PATTERN)]


class TriageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    order_id: OrderId | None = None
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class RefundIntakeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: OrderId
    reason_category: ReasonCategory
    # Item ids the customer wants refunded. None => whole order. Matched against DB items
    # in load_facts; the refund amount is computed there, never here.
    items: list[ItemId] | None = Field(default=None, max_length=50)
    summary: str = Field(min_length=1, max_length=500)
