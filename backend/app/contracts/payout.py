"""PayoutGateway contract (implemented in Phase 7). Only the payout node, which is plain
code, ever builds a PayoutRequest. No agent or tool can reach it.

Units: `amount_paise` is already in paise and is passed through to the gateway unchanged.
Razorpay-style APIs take paise, so there is no `* 100` anywhere.
"""

from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from app.contracts.money import CURRENCY, Currency, PositivePaise


class PayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=100)  # one per case
    amount_paise: PositivePaise
    currency: Currency = CURRENCY
    beneficiary_account_number: SecretStr  # never logged; repr shows '**********'

    @field_validator("beneficiary_account_number")
    @classmethod
    def _validate_account(cls, v: SecretStr) -> SecretStr:
        digits = v.get_secret_value()
        if not (digits.isascii() and digits.isdigit() and 9 <= len(digits) <= 18):
            raise ValueError("account number must be 9 to 18 digits")
        return v


class PayoutResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payout_id: str = Field(pattern=r"^pout_[A-Za-z0-9]{14,}$")
    utr: str = Field(pattern=r"^\d{12}$")
    mode: Literal["IMPS"] = "IMPS"
    status: Literal["processed"] = "processed"
    amount_paise: PositivePaise
    currency: Currency = CURRENCY
    idempotency_key: str = Field(min_length=1, max_length=100)
    account_masked: str = Field(pattern=r"^X+\d{4}$")  # e.g. XXXXXXXX3456
    created_at: AwareDatetime
