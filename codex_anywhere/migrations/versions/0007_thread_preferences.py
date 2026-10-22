"""Persist hub-only conversation display, pinning and archive preferences."""

import sqlalchemy as sa
from alembic import op

revision = "0007_thread_preferences"
down_revision = "0006_hub_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("thread", sa.Column("display_title", sa.String(240), nullable=True))
    for name in ("pinned", "archived"):
        op.add_column(
            "thread", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade():
    for name in ("archived", "pinned", "display_title"):
        op.drop_column("thread", name)
