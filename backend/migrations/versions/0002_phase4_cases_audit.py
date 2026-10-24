"""phase 4 cases and audit events

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=100), nullable=False),
        sa.Column("customer_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("trace_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_cases_thread_id")),
    )
    op.create_index(op.f("ix_cases_customer_id"), "cases", ["customer_id"], unique=False)
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("node", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "node IN ('triage', 'resolution', 'refund_intake', 'load_facts', "
            "'policy_check', 'human_review', 'payout', 'responder')",
            name=op.f("ck_audit_events_node_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'interrupted', 'resumed')",
            name=op.f("ck_audit_events_status_valid"),
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_audit_events_case_id_cases")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_case_id"), "audit_events", ["case_id"], unique=False)
    op.create_index("ix_audit_events_case_id_at", "audit_events", ["case_id", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_events_case_id_at", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_case_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_cases_customer_id"), table_name="cases")
    op.drop_table("cases")
