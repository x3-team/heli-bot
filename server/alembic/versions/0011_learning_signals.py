"""Self-learning signals: run journal on tasks, richer feedback, implicit signals, knowledge candidates."""

from alembic import op
import sqlalchemy as sa

revision = "0011_learning_signals"
down_revision = "0010_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("stopped", sa.String(32), nullable=True))
        batch.add_column(sa.Column("duration_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("parent_task_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_tasks_parent_task_id", "tasks", ["parent_task_id"], ["id"])
        batch.create_index("ix_tasks_parent_task_id", ["parent_task_id"])
    with op.batch_alter_table("task_feedback") as batch:
        batch.add_column(sa.Column("reason_code", sa.String(32), nullable=True))
        batch.add_column(sa.Column("user_fixed", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("mail_disposition", sa.String(32), nullable=True))
        batch.add_column(sa.Column("judge", sa.JSON(), nullable=True))
    op.create_table(
        "task_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_signals_task_id", "task_signals", ["task_id"])
    op.create_index("ix_task_signals_user_id", "task_signals", ["user_id"])
    op.create_index("ix_task_signals_kind", "task_signals", ["kind"])
    op.create_index("ix_task_signals_created_at", "task_signals", ["created_at"])
    with op.batch_alter_table("knowledge_notes") as batch:
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reviewed_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_notes") as batch:
        batch.drop_column("reviewed_by")
        batch.drop_column("approved_at")
    op.drop_index("ix_task_signals_created_at", table_name="task_signals")
    op.drop_index("ix_task_signals_kind", table_name="task_signals")
    op.drop_index("ix_task_signals_user_id", table_name="task_signals")
    op.drop_index("ix_task_signals_task_id", table_name="task_signals")
    op.drop_table("task_signals")
    with op.batch_alter_table("task_feedback") as batch:
        batch.drop_column("judge")
        batch.drop_column("mail_disposition")
        batch.drop_column("user_fixed")
        batch.drop_column("reason_code")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_parent_task_id")
        batch.drop_constraint("fk_tasks_parent_task_id", type_="foreignkey")
        batch.drop_column("parent_task_id")
        batch.drop_column("duration_ms")
        batch.drop_column("stopped")
        batch.drop_column("finished_at")
