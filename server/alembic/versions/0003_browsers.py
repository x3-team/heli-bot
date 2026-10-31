"""One cloud browser slot per user."""

from alembic import op
import sqlalchemy as sa

revision = "0003_browsers"
down_revision = "0002_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_slots",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("container_id", sa.String(128), nullable=True),
        sa.Column("novnc_url", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="stopped"),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("browser_slots")
