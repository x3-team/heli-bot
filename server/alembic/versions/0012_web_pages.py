"""Cache of public pages for hybrid search (web_pages + SQLite FTS5 index)."""

from alembic import op
import sqlalchemy as sa

revision = "0012_web_pages"
down_revision = "0011_learning_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("url", sa.String(1000), nullable=False, unique=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent"),
        sa.Column("etag", sa.String(200), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_web_pages_host", "web_pages", ["host"])
    op.create_index("ix_web_pages_fetched_at", "web_pages", ["fetched_at"])
    # The full-text index (SQLite FTS5) is created by app.search.cache on first use.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS web_pages_fts")
    op.drop_index("ix_web_pages_fetched_at", table_name="web_pages")
    op.drop_index("ix_web_pages_host", table_name="web_pages")
    op.drop_table("web_pages")
