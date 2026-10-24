"""Store expected delivery dates for undelivered-order policy checks."""

import sqlalchemy as sa
from alembic import op

revision = "0009_order_delivery_metadata"
down_revision = "0008_review_decision_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("expected_delivery_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "expected_delivery_at")
