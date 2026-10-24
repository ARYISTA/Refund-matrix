"""Phase 7 deterministic mock payout gateway.

The mock persists its result before returning it and enforces one payout per case. It never
logs or returns the beneficiary account number.
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.payout import PayoutRequest, PayoutResult
from app.contracts.state import CaseState
from app.db.models import Case, Customer, Payout


class PayoutGatewayError(RuntimeError):
    """Raised when a payout cannot be safely simulated."""


def mask_account(account: str) -> str:
    if not account.isascii() or not account.isdigit() or len(account) < 9 or len(account) > 18:
        raise PayoutGatewayError("beneficiary account is invalid")
    return "X" * (len(account) - 4) + account[-4:]


class MockPayoutGateway:
    """Callable gateway suitable for WorkflowDependencies.payout."""

    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now

    def __call__(self, state: CaseState, session: Session) -> PayoutResult:
        if state.facts is None or state.policy_result is None:
            raise PayoutGatewayError("verified facts and policy result are required")
        if state.policy_result.amount_paise != state.facts.refund_amount_paise:
            raise PayoutGatewayError("payout amount does not match the verified refund amount")
        amount = state.policy_result.amount_paise
        if amount <= 0:
            raise PayoutGatewayError("payout amount must be positive")

        case_id = str(state.case_id)
        key = f"refund:{case_id}"
        existing = session.scalar(select(Payout).where(Payout.idempotency_key == key))
        if existing is not None:
            return self._result(existing)

        case = session.get(Case, case_id)
        if case is None:
            raise PayoutGatewayError("case must be persisted before payout")
        customer = session.get(Customer, case.customer_id)
        if customer is None:
            raise PayoutGatewayError("beneficiary customer was not found")
        request = PayoutRequest(
            case_id=state.case_id,
            idempotency_key=key,
            amount_paise=amount,
            beneficiary_account_number=customer.bank_account_number,
        )
        payout_id = "pout_" + hashlib.sha256(key.encode()).hexdigest()[:20]
        utr = str(int(hashlib.sha256((key + ":utr").encode()).hexdigest()[:12], 16))[:12].zfill(12)
        row = Payout(
            id=str(uuid4()),
            case_id=case_id,
            idempotency_key=request.idempotency_key,
            payout_id=payout_id,
            utr=utr,
            amount_paise=request.amount_paise,
            currency=request.currency,
            status="processed",
            account_masked=mask_account(request.beneficiary_account_number.get_secret_value()),
            created_at=self._now(),
        )
        session.add(row)
        session.flush()
        return self._result(row)

    @staticmethod
    def _result(row: Payout) -> PayoutResult:
        return PayoutResult(
            payout_id=row.payout_id,
            utr=row.utr,
            amount_paise=row.amount_paise,
            currency=row.currency,
            idempotency_key=row.idempotency_key,
            account_masked=row.account_masked,
            created_at=row.created_at,
        )
