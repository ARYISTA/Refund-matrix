"""phase 7 payout simulation

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payouts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("payout_id", sa.String(length=40), nullable=False),
        sa.Column("utr", sa.String(length=12), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processed"),
        sa.Column("account_masked", sa.String(length=22), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_payouts_amount_positive")),
        sa.CheckConstraint("status = 'processed'", name=op.f("ck_payouts_status_valid")),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_payouts_case_id_cases")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
        sa.UniqueConstraint("case_id", name=op.f("uq_payouts_case_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payouts_idempotency_key")),
        sa.UniqueConstraint("payout_id", name=op.f("uq_payouts_payout_id")),
        sa.UniqueConstraint("utr", name=op.f("uq_payouts_utr")),
    )
    op.create_index("ix_payouts_case_id", "payouts", ["case_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_payouts_case_id", table_name="payouts")
    op.drop_table("payouts")
