"""Encrypted credentials and an access log."""

from alembic import op
import sqlalchemy as sa

revision = "0004_vault"
down_revision = "0003_browsers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("site", sa.String(256), nullable=False),
        sa.Column("username", sa.String(256), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credentials_user_id", "credentials", ["user_id"])
    op.create_table(
        "credential_access",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("credential_id", sa.Uuid(), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credential_access_user_id", "credential_access", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_credential_access_user_id", table_name="credential_access")
    op.drop_table("credential_access")
    op.drop_index("ix_credentials_user_id", table_name="credentials")
    op.drop_table("credentials")
