"""User schedules."""

from alembic import op
import sqlalchemy as sa

revision = "0005_schedules"
down_revision = "0004_vault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("expression", sa.String(64), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedules_user_id", "schedules", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_schedules_user_id", table_name="schedules")
    op.drop_table("schedules")
