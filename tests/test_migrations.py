from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from codex_anywhere.cli import upgrade_database
from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import db


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
