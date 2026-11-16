"""Store return package receipt timestamp for Amazon return policy verification.

Revision ID: 0010_order_return_received
Revises: 0009_order_delivery_metadata
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_order_return_received"
down_revision = "0009_order_delivery_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("return_received_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "return_received_at")
