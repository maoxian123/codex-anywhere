import importlib
import io
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from codex_anywhere.cli import upgrade_database
from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import Thread, db


def test_fresh_and_repeat_migration_match_models(tmp_path):
    app = create_app(
        {
            "SECRET_KEY": "migration-test-secret",
            "ADMIN_PASSWORD": "migration-test-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "migration.db"),
            "ASSET_DIR": str(tmp_path / "assets"),
        }
    )
    try:
        upgrade_database(app)
        upgrade_database(app)
        with app.app_context(), db.engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), db.metadata)
            assert not diff
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


def test_thread_preferences_upgrade_retains_existing_native_data(tmp_path):
    app = create_app(
        {
            "SECRET_KEY": "migration-test-secret",
            "ADMIN_PASSWORD": "migration-test-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "old.db"),
            "ASSET_DIR": str(tmp_path / "assets"),
        }
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "codex_anywhere" / "migrations")
    )
    try:
        with app.app_context(), db.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0006_hub_settings")
            metadata = sa.MetaData()
            host = sa.Table("host", metadata, autoload_with=connection)
            thread = sa.Table("thread", metadata, autoload_with=connection)
            item = sa.Table("item", metadata, autoload_with=connection)
            connection.execute(
                host.insert().values(
                    id="host", name="Workstation", platform="test", token_hash="token"
                )
            )
            connection.execute(
                thread.insert().values(
                    id="thread",
                    host_id="host",
                    native_id="native-original",
                    title="Original",
                    cwd="/workspace",
                    status="active",
                    active_turn_id="turn",
                    created_at=100,
                    updated_at=200,
                )
            )
            connection.execute(
                item.insert().values(
                    id="item",
                    native_id="native-item",
                    thread_id="thread",
                    turn_id="turn",
                    type="unknownNativeType",
                    data={"unknown": [1, {"preserve": True}]},
                    created_at=150,
                )
            )
            command.upgrade(config, "head")
        with app.app_context():
            saved = db.session.get(Thread, "thread")
            assert saved.title == "Original" and saved.native_id == "native-original"
            assert saved.status == "active" and saved.active_turn_id == "turn"
            assert saved.display_title is None and saved.pinned is False and saved.archived is False
            assert (
                db.session.execute(sa.text("SELECT data FROM item WHERE id='item'")).scalar()
                == '{"unknown": [1, {"preserve": true}]}'
            )
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


def test_thread_preferences_migration_compiles_postgresql_defaults():
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    migration = importlib.import_module(
        "codex_anywhere.migrations.versions.0007_thread_preferences"
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN display_title VARCHAR(240)" in sql
    assert "ADD COLUMN pinned BOOLEAN DEFAULT false NOT NULL" in sql
    assert "ADD COLUMN archived BOOLEAN DEFAULT false NOT NULL" in sql
