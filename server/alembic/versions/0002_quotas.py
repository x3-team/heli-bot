"""Quota ledger, user limits, and threshold alerts."""

from alembic import op
import sqlalchemy as sa

revision = "0002_quotas"
down_revision = "0001_trajectories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("monthly_limit_rub", sa.Numeric(14, 6), nullable=False, server_default="1000"),
    )
    op.add_column(
        "users",
        sa.Column("weekly_limit_rub", sa.Numeric(14, 6), nullable=True),
    )
    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_rub", sa.Numeric(14, 6), nullable=False),
        sa.Column("reserved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_ledger_user_id", "usage_ledger", ["user_id"])
    op.create_index("ix_usage_ledger_created_at", "usage_ledger", ["created_at"])
    op.create_table(
        "quota_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("window", sa.String(8), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("period_key", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "window", "threshold", "period_key", name="uq_quota_alert"),
    )
    op.create_index("ix_quota_alerts_user_id", "quota_alerts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_quota_alerts_user_id", table_name="quota_alerts")
    op.drop_table("quota_alerts")
    op.drop_index("ix_usage_ledger_created_at", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_user_id", table_name="usage_ledger")
    op.drop_table("usage_ledger")
    op.drop_column("users", "weekly_limit_rub")
    op.drop_column("users", "monthly_limit_rub")
