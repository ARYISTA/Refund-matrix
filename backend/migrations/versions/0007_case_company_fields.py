"""case fields for company case search

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("order_id", sa.String(length=40), nullable=True))
    op.add_column("cases", sa.Column("amount_paise", sa.BigInteger(), nullable=True))
    op.add_column("cases", sa.Column("reason_category", sa.String(length=20), nullable=True))
    op.add_column("cases", sa.Column("decision_type", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "decision_type")
    op.drop_column("cases", "reason_category")
    op.drop_column("cases", "amount_paise")
    op.drop_column("cases", "order_id")
