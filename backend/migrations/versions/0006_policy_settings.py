"""versioned editable policy threshold

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("threshold_paise", sa.BigInteger(), nullable=True))
    op.add_column("cases", sa.Column("policy_version", sa.String(length=50), nullable=True))
    op.create_table(
        "policy_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("threshold_paise", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("threshold_paise >= 100", name="ck_policy_settings_threshold_minimum"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_settings")),
    )
    op.create_table(
        "policy_setting_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("setting_version", sa.Integer(), nullable=False),
        sa.Column("old_threshold_paise", sa.BigInteger(), nullable=False),
        sa.Column("new_threshold_paise", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_setting_audits")),
    )


def downgrade() -> None:
    op.drop_table("policy_setting_audits")
    op.drop_table("policy_settings")
    op.drop_column("cases", "policy_version")
    op.drop_column("cases", "threshold_paise")
