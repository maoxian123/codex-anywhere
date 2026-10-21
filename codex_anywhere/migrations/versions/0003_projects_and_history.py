"""Group existing tasks into projects and support ordered imported history."""

import hashlib
import ntpath
import posixpath
import time
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0003_projects_and_history"
down_revision = "0002_thread_last_error"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    sqlite = connection.dialect.name == "sqlite"
    op.create_table(
        "project",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("host_id", sa.String(32), sa.ForeignKey("host.id"), nullable=False),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("cwd_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("host_id", "cwd_key"),
    )
    op.create_index("ix_project_host_id", "project", ["host_id"])
    if sqlite:
        # SQLite can add a nullable REFERENCES column directly. A batch table
        # rebuild would temporarily drop thread while commands/items reference it.
        op.execute("ALTER TABLE thread ADD COLUMN project_id VARCHAR(32) REFERENCES project (id)")
    else:
        op.add_column("thread", sa.Column("project_id", sa.String(32), nullable=True))
        op.create_foreign_key(
            "fk_thread_project_id_project", "thread", "project", ["project_id"], ["id"]
        )
    op.create_index("ix_thread_project_id", "thread", ["project_id"])
    op.add_column(
        "thread", sa.Column("imported", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "thread",
        sa.Column("history_loaded", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "item", sa.Column("position", sa.BigInteger(), nullable=False, server_default="0")
    )

    platforms = dict(connection.execute(sa.text("SELECT id, platform FROM host")).all())
    projects = {}
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, host_id, cwd, created_at, updated_at FROM thread ORDER BY created_at, id"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        cwd = row["cwd"]
        if not cwd:
            continue
        windows = platforms.get(row["host_id"], "").lower().startswith("win") or bool(
            ntpath.splitdrive(cwd)[0]
        )
        pathmod = ntpath if windows else posixpath
        normalized = pathmod.normpath(cwd)
        if windows:
            normalized = pathmod.normcase(normalized)
        key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identity = (row["host_id"], key)
        if identity not in projects:
            project_id = uuid.uuid4().hex
            name = ntpath.basename(cwd.rstrip("/\\")) or posixpath.basename(cwd.rstrip("/")) or cwd
            connection.execute(
                sa.text(
                    "INSERT INTO project (id, host_id, name, cwd, cwd_key, created_at, updated_at) "
                    "VALUES (:id, :host_id, :name, :cwd, :cwd_key, :created_at, :updated_at)"
                ),
                dict(
                    id=project_id,
                    host_id=row["host_id"],
                    name=name[:240],
                    cwd=cwd,
                    cwd_key=key,
                    created_at=row["created_at"] or time.time(),
                    updated_at=row["updated_at"] or time.time(),
                ),
            )
            projects[identity] = project_id
        connection.execute(
            sa.text("UPDATE thread SET project_id=:project_id WHERE id=:id"),
            dict(project_id=projects[identity], id=row["id"]),
        )
    positions = {}
    for row in (
        connection.execute(
            sa.text("SELECT id, thread_id FROM item ORDER BY thread_id, created_at, id")
        )
        .mappings()
        .all()
    ):
        position = positions.get(row["thread_id"], 0)
        connection.execute(
            sa.text("UPDATE item SET position=:position WHERE id=:id"),
            dict(position=position, id=row["id"]),
        )
        positions[row["thread_id"]] = position + 1
    op.drop_column("host", "roots")
    if not sqlite:
        op.alter_column("thread", "imported", server_default=None)
        op.alter_column("thread", "history_loaded", server_default=None)
        op.alter_column("item", "position", server_default=None)


def downgrade():
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.add_column("host", sa.Column("roots", sa.JSON(), nullable=False, server_default="[]"))
    op.drop_column("item", "position")
    op.drop_column("thread", "history_loaded")
    op.drop_column("thread", "imported")
    op.drop_index("ix_thread_project_id", table_name="thread")
    if not sqlite:
        op.drop_constraint("fk_thread_project_id_project", "thread", type_="foreignkey")
    op.drop_column("thread", "project_id")
    op.drop_index("ix_project_host_id", table_name="project")
    op.drop_table("project")
