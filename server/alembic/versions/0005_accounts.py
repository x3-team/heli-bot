"""Password accounts, recovery codes, and trial week."""

from alembic import op
import sqlalchemy as sa

revision = "0005_accounts"
down_revision = "0004_browser_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("login_codes", "invite_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "login_codes",
        sa.Column("purpose", sa.String(16), nullable=False, server_default="login"),
    )


def downgrade() -> None:
    op.drop_column("login_codes", "purpose")
    op.alter_column("login_codes", "invite_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("users", "trial_ends_at")
    op.drop_column("users", "password_hash")
