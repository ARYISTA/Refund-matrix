"""Persist reviewer decisions so concurrent retries are safe."""

import sqlalchemy as sa
from alembic import op

revision = "0008_review_decision_idempotency"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("review_action", sa.String(length=20), nullable=True))
    op.add_column("cases", sa.Column("reviewer_id", sa.String(length=40), nullable=True))
    op.add_column("cases", sa.Column("review_note", sa.String(length=500), nullable=True))
    op.add_column("cases", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "reviewed_at")
    op.drop_column("cases", "review_note")
    op.drop_column("cases", "reviewer_id")
    op.drop_column("cases", "review_action")
