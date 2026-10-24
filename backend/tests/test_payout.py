"""Phase 7: payout request safety and mock gateway primitives."""

from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from app.contracts.payout import PayoutRequest
from app.payout import PayoutGatewayError, mask_account


def test_account_is_masked_without_leaking_digits():
    assert mask_account("111122223333") == "XXXXXXXX3333"


def test_invalid_account_is_rejected():
    with pytest.raises(PayoutGatewayError):
        mask_account("not-an-account")


def test_payout_request_rejects_non_positive_amount():
    with pytest.raises(ValidationError):
        PayoutRequest(
            case_id=uuid4(),
            idempotency_key="refund:case",
            amount_paise=0,
            beneficiary_account_number=SecretStr("111122223333"),
        )
