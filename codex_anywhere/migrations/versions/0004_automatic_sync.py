"""Track bounded automatic catalogs and lazy history snapshot versions."""

import sqlalchemy as sa
from alembic import op

revision = "0004_automatic_sync"
down_revision = "0003_projects_and_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("host", sa.Column("last_sync_at", sa.Float(), nullable=True))
    op.add_column("host", sa.Column("sync_error", sa.Text(), nullable=True))
    for name in ("native_updated_at", "history_native_updated_at", "history_synced_at"):
        op.add_column("thread", sa.Column(name, sa.Float(), nullable=True))
    op.add_column(
        "thread",
        sa.Column("history_stale", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_item_thread_position", "item", ["thread_id", "position", "id"])
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("thread", "history_stale", server_default=None)


def downgrade():
    op.drop_index("ix_item_thread_position", table_name="item")
    for name in (
        "history_stale",
        "history_synced_at",
        "history_native_updated_at",
        "native_updated_at",
    ):
        op.drop_column("thread", name)
    op.drop_column("host", "sync_error")
    op.drop_column("host", "last_sync_at")
