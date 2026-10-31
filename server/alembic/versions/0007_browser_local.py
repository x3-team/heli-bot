"""Local Chrome/Edge via CDP on the user's machine."""

from alembic import op
import sqlalchemy as sa

revision = "0007_browser_local"
down_revision = "0006_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "browser_slots",
        sa.Column("mode", sa.String(8), nullable=False, server_default="cloud"),
    )
    op.add_column(
        "browser_slots",
        sa.Column("local_browser", sa.String(16), nullable=True),
    )
    op.add_column(
        "browser_slots",
        sa.Column("local_port", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("browser_slots", "local_port")
    op.drop_column("browser_slots", "local_browser")
    op.drop_column("browser_slots", "mode")
