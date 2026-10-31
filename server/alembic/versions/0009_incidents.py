"""User-submitted error reports with optional screenshot."""

from alembic import op
import sqlalchemy as sa

revision = "0009_incidents"
down_revision = "0008_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("log_excerpt", sa.Text(), nullable=False),
        sa.Column("screenshot", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incident_reports_user_id", "incident_reports", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_incident_reports_user_id", table_name="incident_reports")
    op.drop_table("incident_reports")
