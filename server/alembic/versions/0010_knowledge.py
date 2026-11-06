"""Heli's knowledge base: checked rules with a source and a date."""

from alembic import op
import sqlalchemy as sa

revision = "0010_knowledge"
down_revision = "0009_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_notes_topic", "knowledge_notes", ["topic"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_notes_topic", table_name="knowledge_notes")
    op.drop_table("knowledge_notes")
