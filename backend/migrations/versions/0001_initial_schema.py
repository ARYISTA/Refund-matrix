"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-23 04:57:01.689545
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector is needed from Phase 5 (KB embeddings). Enabling it here confirms at the very
    # first migration that the database image really ships the extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("bank_account_number", sa.String(length=18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("email", name=op.f("uq_customers_email")),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.String(length=40), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_paise", sa.BigInteger(), nullable=False),
        sa.Column("fraud_flag", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("total_paise > 0", name=op.f("ck_orders_total_positive")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False)
    op.create_table(
        "order_items",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("order_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_paise", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name=op.f("ck_order_items_quantity_positive")),
        sa.CheckConstraint("unit_price_paise >= 0", name=op.f("ck_order_items_unit_price_non_negative")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("order_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('created', 'authorized', 'captured', 'failed', 'refunded')",
            name=op.f("ck_payments_status_valid"),
        ),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("order_id", name=op.f("uq_payments_order_id")),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("order_id", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.String(length=40), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("reason_category", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "reason_category IN ('damaged', 'wrong_item', 'not_delivered', 'defective', 'other')",
            name=op.f("ck_refunds_reason_valid"),
        ),
        sa.CheckConstraint("status IN ('approved', 'paid')", name=op.f("ck_refunds_status_valid")),
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_refunds_amount_positive")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_refunds_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_refunds_order_id_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refunds")),
    )
    op.create_index(
        "ix_refunds_customer_id_created_at", "refunds", ["customer_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_refunds_order_id"), "refunds", ["order_id"], unique=False)


def downgrade() -> None:
    # The vector extension is intentionally left installed; other schemas may use it.
    op.drop_index(op.f("ix_refunds_order_id"), table_name="refunds")
    op.drop_index("ix_refunds_customer_id_created_at", table_name="refunds")
    op.drop_table("refunds")
    op.drop_table("payments")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_table("customers")
