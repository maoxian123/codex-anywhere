"""Persist runtime capabilities, execution settings and native history coverage."""

import sqlalchemy as sa
from alembic import op

revision = "0005_runtime_settings"
down_revision = "0004_automatic_sync"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("host", sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("host", sa.Column("runtime_capabilities", sa.JSON(), nullable=True))
    op.add_column("host", sa.Column("capabilities_updated_at", sa.Float(), nullable=True))
    op.add_column(
        "project", sa.Column("execution_settings", sa.JSON(), nullable=False, server_default="{}")
    )
    for name in ("execution_settings", "effective_settings"):
        op.add_column("thread", sa.Column(name, sa.JSON(), nullable=False, server_default="{}"))
    op.add_column(
        "thread", sa.Column("settings_revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("thread", sa.Column("effective_settings_revision", sa.Integer(), nullable=True))
    op.add_column("thread", sa.Column("history_cursor", sa.Text(), nullable=True))
    op.add_column("thread", sa.Column("history_generation", sa.String(64), nullable=True))
    op.add_column("thread", sa.Column("native_fingerprint", sa.String(64), nullable=True))
    op.add_column("thread", sa.Column("history_fingerprint", sa.String(64), nullable=True))
    op.add_column(
        "thread",
        sa.Column("history_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Previously imported histories and hub-created threads already have complete
    # coverage; unexpanded automatic catalog entries do not.
    op.execute(sa.text("UPDATE thread SET history_complete = history_loaded"))


def downgrade():
    for name in (
        "history_fingerprint",
        "native_fingerprint",
        "history_complete",
        "history_generation",
        "history_cursor",
        "effective_settings_revision",
        "settings_revision",
        "effective_settings",
        "execution_settings",
    ):
        op.drop_column("thread", name)
    op.drop_column("project", "execution_settings")
    for name in ("capabilities_updated_at", "runtime_capabilities", "capabilities"):
        op.drop_column("host", name)
