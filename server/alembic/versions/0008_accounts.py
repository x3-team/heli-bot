"""Password accounts, recovery codes, and trial week."""

from alembic import op
import sqlalchemy as sa

revision = "0008_accounts"
down_revision = "0007_browser_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("login_codes") as batch:
        batch.alter_column("invite_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(
            sa.Column("purpose", sa.String(16), nullable=False, server_default="login"),
        )


def downgrade() -> None:
    with op.batch_alter_table("login_codes") as batch:
        batch.drop_column("purpose")
        batch.alter_column("invite_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("users", "trial_ends_at")
    op.drop_column("users", "password_hash")
