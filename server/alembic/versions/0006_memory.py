"""Long-term memory facts."""

from alembic import op
import sqlalchemy as sa

revision = "0006_memory"
down_revision = "0005_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_facts_user_id", "memory_facts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_facts_user_id", table_name="memory_facts")
    op.drop_table("memory_facts")
