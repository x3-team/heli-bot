"""Bot plans and wallet: plan columns on users, wallet_tx ledger."""

from alembic import op
import sqlalchemy as sa

revision = "0013_billing"
down_revision = "0012_web_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("plan", sa.String(16), nullable=True))
        batch.add_column(sa.Column("plan_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("plan_ends_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("plan_auto_renew", sa.Boolean(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("wallet_rub", sa.Numeric(14, 2), nullable=False, server_default="0"))
        batch.add_column(sa.Column("overage_enabled", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("overage_cap_rub", sa.Numeric(14, 2), nullable=False, server_default="0"))
    op.create_table(
        "wallet_tx",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("balance_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref"),
    )
    op.create_index("ix_wallet_tx_user_id", "wallet_tx", ["user_id"])
    op.create_index("ix_wallet_tx_kind", "wallet_tx", ["kind"])
    op.create_index("ix_wallet_tx_created_at", "wallet_tx", ["created_at"])


def downgrade() -> None:
    op.drop_table("wallet_tx")
    with op.batch_alter_table("users") as batch:
        for column in ("overage_cap_rub", "overage_enabled", "wallet_rub", "plan_auto_renew", "plan_ends_at", "plan_started_at", "plan"):
            batch.drop_column(column)
