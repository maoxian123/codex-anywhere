"""Persist extensible hub configuration, including administrator credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0006_hub_settings"
down_revision = "0005_runtime_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hub_setting",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table("hub_setting")
