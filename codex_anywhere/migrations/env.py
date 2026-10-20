"""Migrations use a connection supplied by the CLI, with no secret-bearing ini file."""

from alembic import context

from codex_anywhere.hub.models import db

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=db.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
