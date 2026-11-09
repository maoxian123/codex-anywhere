"""Index SQLite rows that can contain attachment or snapshot references."""

from alembic import op

revision = "0008_reference_indexes"
down_revision = "0007_thread_preferences"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    indexes = {
        "ix_command_reference_metadata": (
            "CREATE INDEX ix_command_reference_metadata ON command (host_id, thread_id) "
            "WHERE CASE WHEN json_valid(payload) THEN "
            "(json_array_length(payload, '$._assets') > 0 "
            "OR json_array_length(payload, '$._references') > 0) ELSE 1 END = 1"
        ),
        "ix_item_reference_metadata": (
            "CREATE INDEX ix_item_reference_metadata ON item (thread_id) "
            "WHERE CASE WHEN json_valid(data) THEN "
            "(json_array_length(data, '$._assets') > 0 "
            "OR json_array_length(data, '$._references') > 0) ELSE 1 END = 1"
        ),
    }
    for name, statement in indexes.items():
        # SQLite may persist DDL even when a later step fails. A restart must be
        # able to finish a partially completed migration without accepting an
        # unrelated same-name index as the required one.
        connection.exec_driver_sql(statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
        actual = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).scalar_one_or_none()
        if actual != statement:
            raise RuntimeError(f"Unexpected definition for SQLite index {name}")


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.drop_index("ix_item_reference_metadata", table_name="item")
        op.drop_index("ix_command_reference_metadata", table_name="command")
