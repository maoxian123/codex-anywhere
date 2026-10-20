"""Persist the most recent task failure for history and reloads."""

import sqlalchemy as sa
from alembic import op

revision = "0002_thread_last_error"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("thread", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("thread", "last_error")
