"""Deleting a machine removes only its hub projection, using fake nodes."""

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import event, func, text

from codex_anywhere.cli import upgrade_database
from codex_anywhere.hub import _identifier, _json, create_app
from codex_anywhere.hub.hosts import _references, register_host_routes
from codex_anywhere.hub.models import (
    Asset,
    Command,
    Event,
    Host,
    Interaction,
    Item,
    Project,
    Snapshot,
    Thread,
    db,
)
from codex_anywhere.hub.transport import emit, handle_node_message


@pytest.fixture(autouse=True)
def host_routes(app):
    # Keep the module independently testable while create_app integration lands.
    if "settings_hosts" not in app.view_functions:
        register_host_routes(app, _json, _identifier)


def asset(app, *, host_id=None, thread_id=None, name="image.png", size=7, path=None):
    directory = Path(app.config["ASSET_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    path = Path(path) if path is not None else directory / f"{uuid4().hex}.png"
    if not path.exists():
        path.write_bytes(b"picture")
    record = Asset(
        host_id=host_id, thread_id=thread_id, name=name, size=size, mime="image/png", path=str(path)
    )
    db.session.add(record)
    db.session.flush()
    return record


def reference_predicate(column):
    return (
        f"CASE WHEN json_valid({column}) THEN ("
        f"json_array_length({column}, '$._assets') > 0 OR "
        f"json_array_length({column}, '$._references') > 0) ELSE 1 END = 1"
    )


def test_host_settings_and_delete_require_authentication_and_csrf(
    app, client, logged_in, paired_host
):
    host = paired_host()
    host_id = host["host"]["id"]
    anonymous = app.test_client()
    assert anonymous.get("/api/settings/hosts").status_code == 401
    assert (
        anonymous.delete(f"/api/hosts/{host_id}", json={"confirm_name": "workstation"}).status_code
        == 401
    )
    assert (
        logged_in.delete(f"/api/hosts/{host_id}", json={"confirm_name": "workstation"}).status_code
        == 403
    )
    response = logged_in.get("/api/settings/hosts")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert host["token"] not in response.get_data(as_text=True)
    assert "token_hash" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "body",
    [{}, {"confirm_name": "wrong"}, {"confirm_name": "workstation "}, {"confirm_name": None}],
)
def test_delete_requires_the_exact_current_name(app, api, paired_host, body):
    host = paired_host()
    host_id = host["host"]["id"]
    response = api("DELETE", f"/api/hosts/{host_id}", json=body)
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(Host, host_id) is not None
    assert app.extensions["broker"].is_online(host_id)
    assert not host["socket"].closed


def test_statistics_include_unassociated_upload_references_without_counting_them_twice(
    app, api, paired_host, create_thread, completed_item
):
    host = paired_host()
    thread = create_thread(host)
    completed_item(host, thread)
    host_id = host["host"]["id"]
    with app.app_context():
        first = asset(app, host_id=host_id, thread_id=thread["id"], size=10)
        upload = asset(app, size=20)
        db.session.add(
            Command(
                host_id=host_id,
                thread_id=thread["id"],
                kind="turn.start",
                payload={
                    "_assets": [first.as_dict(), upload.as_dict()],
                    "input": [{"type": "image", "url": "data:image/png;base64," + "a" * 100000}],
                },
            )
        )
        item = Item.query.filter_by(thread_id=thread["id"]).one()
        item.data = {**item.data, "_assets": [upload.as_dict()]}
        db.session.commit()
    response = api("GET", "/api/settings/hosts")
    entry = next(value for value in response.get_json()["hosts"] if value["id"] == host_id)
    assert entry["online"] is True
    assert entry["data"] == {
        "projects": 1,
        "threads": 1,
        "messages": 1,
        "attachments": 2,
        "attachment_bytes": 30,
    }
    assert "data:image" not in response.get_data(as_text=True)


def test_delete_purges_the_host_isolates_another_host_and_never_touches_native_files(
    app, api, paired_host, create_thread, completed_item
):
    removed = paired_host("removed-machine")
    kept = paired_host("kept-machine")
    first = create_thread(removed)
    second = create_thread(kept)
    completed_item(removed, first, "Deleted native projection")
    completed_item(kept, second, "Keep this history")
    host_id, kept_id = removed["host"]["id"], kept["host"]["id"]
    native_file = Path(removed["root"]) / "original-rollout.jsonl"
    native_file.write_text("original Codex data", encoding="utf-8")
    with app.app_context():
        direct = asset(app, host_id=host_id, thread_id=first["id"])
        command_upload = asset(app)
        item_upload = asset(app)
        survivor = asset(app, host_id=kept_id, thread_id=second["id"])
        snapshots = [
            Snapshot(kind="task", title="Deleted source", thread_id=first["id"], excerpt="source"),
            Snapshot(kind="web", title="Deleted web reference", excerpt="web source"),
            Snapshot(
                kind="task", title="Keep reference", thread_id=second["id"], excerpt="keep source"
            ),
        ]
        db.session.add_all(snapshots)
        db.session.flush()
        deleted_snapshot_ids = [value.id for value in snapshots[:2]]
        kept_snapshot = snapshots[2].id
        db.session.add(
            Command(
                host_id=host_id,
                thread_id=first["id"],
                kind="turn.start",
                payload={
                    "_assets": [command_upload.as_dict()],
                    "_references": [snapshots[1].as_dict()],
                },
            )
        )
        item = Item.query.filter_by(thread_id=first["id"]).one()
        item.data = {**item.data, "_assets": [item_upload.as_dict()]}
        db.session.add(
            Interaction(
                host_id=host_id,
                thread_id=first["id"],
                runtime_id="native-epoch",
                rpc_id=42,
                method="approval",
                params={},
            )
        )
        db.session.add(
            Interaction(
                host_id=kept_id,
                thread_id=second["id"],
                runtime_id="other-epoch",
                rpc_id=43,
                method="approval",
                params={},
            )
        )
        db.session.add(
            Event(host_id=host_id, thread_id=first["id"], method="test/deleted-maximum", params={})
        )
        db.session.commit()
        removed_paths = [Path(value.path) for value in (direct, command_upload, item_upload)]
        kept_path = Path(survivor.path)
        prior_cursor = db.session.query(func.max(Event.id)).scalar()
    writers = app.extensions["writer_sessions"]
    writers.clients["browser"] = {
        (first["id"], "removed-tab"): {"expires_at": 1},
        (second["id"], "kept-tab"): {"expires_at": 1},
    }
    sent_count = len(removed["socket"].messages)
    response = api("DELETE", f"/api/hosts/{host_id}", json={"confirm_name": "removed-machine"})
    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["deleted"] is True and data["host_id"] == host_id
    assert data["cleanup"] == {"deleted": 3, "missing": 0, "retained": 0, "failed": 0}
    assert not app.extensions["broker"].is_attached(host_id, removed["socket"])
    assert app.extensions["broker"].is_attached(kept_id, kept["socket"])
    assert removed["socket"].closed is False  # Only its owner may perform socket I/O.
    assert len(removed["socket"].messages) == sent_count
    assert writers.clients["browser"] == {(second["id"], "kept-tab"): {"expires_at": 1}}
    assert native_file.read_text(encoding="utf-8") == "original Codex data"
    assert all(not path.exists() for path in removed_paths)
    assert kept_path.is_file()
    with app.app_context():
        for model in (Host, Project, Thread, Command, Interaction):
            query = (
                model.query.filter_by(id=host_id)
                if model is Host
                else model.query.filter_by(host_id=host_id)
            )
            assert query.count() == 0
        assert Item.query.filter_by(thread_id=first["id"]).count() == 0
        assert Asset.query.filter_by(host_id=host_id).count() == 0
        assert not Snapshot.query.filter(Snapshot.id.in_(deleted_snapshot_ids)).count()
        assert db.session.get(Snapshot, kept_snapshot) is not None
        assert Item.query.filter_by(thread_id=second["id"]).count() == 1
        assert Thread.query.filter_by(host_id=kept_id).count() == 1
        assert Interaction.query.filter_by(host_id=kept_id).count() == 1
        assert not Event.query.filter_by(host_id=host_id).count()
        marker = Event.query.filter_by(method="anywhere/host_deleted").one()
        assert marker.id > prior_cursor
        assert marker.host_id is None and marker.thread_id is None
        assert marker.params == {"host_id": host_id}
        previous = marker.id
        next_event = emit("test/after-deletion", {})
        db.session.commit()
        assert next_event.id > previous
        handle_node_message(app, host_id, {"type": "hello", "runtime_id": "late-epoch"})
        handle_node_message(
            app,
            host_id,
            {
                "type": "event",
                "event_id": "late-deleted-event",
                "runtime_id": "late-epoch",
                "seq": 1,
                "method": "thread/started",
                "params": {"thread": {"id": "late-thread", "cwd": removed["root"]}},
            },
        )
        assert db.session.get(Host, host_id) is None
        assert Thread.query.filter_by(host_id=host_id).count() == 0
    assert (
        app.test_client()
        .post("/api/node/assets", headers={"Authorization": "Bearer " + removed["token"]})
        .status_code
        == 401
    )
    assert (
        api("DELETE", f"/api/hosts/{host_id}", json={"confirm_name": "removed-machine"}).status_code
        == 404
    )


def test_shared_attachment_survives_and_deleted_host_associations_are_removed(
    app, api, paired_host, create_thread
):
    removed = paired_host("shared-source")
    kept = paired_host("shared-user")
    first, second = create_thread(removed), create_thread(kept)
    with app.app_context():
        shared = asset(app, host_id=removed["host"]["id"], thread_id=first["id"])
        snapshot = Snapshot(kind="web", title="Shared reference", excerpt="shared")
        db.session.add(snapshot)
        db.session.flush()
        db.session.add_all(
            [
                Command(
                    host_id=removed["host"]["id"],
                    thread_id=first["id"],
                    kind="turn.start",
                    payload={"_assets": [shared.as_dict()], "_references": [snapshot.as_dict()]},
                ),
                Command(
                    host_id=kept["host"]["id"],
                    thread_id=second["id"],
                    kind="turn.start",
                    payload={"_assets": [shared.as_dict()], "_references": [snapshot.as_dict()]},
                ),
            ]
        )
        db.session.commit()
        asset_id, path, snapshot_id = shared.id, Path(shared.path), snapshot.id
    response = api(
        "DELETE", f"/api/hosts/{removed['host']['id']}", json={"confirm_name": "shared-source"}
    )
    assert response.status_code == 200
    assert path.exists()
    with app.app_context():
        value = db.session.get(Asset, asset_id)
        assert value is not None and value.host_id is None and value.thread_id is None
        assert db.session.get(Snapshot, snapshot_id) is not None
        retained = Command.query.filter_by(host_id=kept["host"]["id"], kind="turn.start").one()
        assert retained.payload["_assets"][0]["id"] == asset_id
    assert api("GET", f"/api/assets/{asset_id}").status_code == 200


@pytest.mark.parametrize("kind", ["command", "item"])
def test_shared_references_in_legacy_nan_json_are_preserved_on_delete(
    app, api, paired_host, create_thread, kind
):
    removed = paired_host("legacy-source")
    kept = paired_host("legacy-reader")
    first, second = create_thread(removed), create_thread(kept)
    with app.app_context():
        shared = asset(app, host_id=removed["host"]["id"], thread_id=first["id"])
        snapshot = Snapshot(kind="web", title="Shared reference", excerpt="shared")
        db.session.add(snapshot)
        db.session.flush()
        references = {"_assets": [shared.as_dict()], "_references": [snapshot.as_dict()]}
        db.session.add(
            Command(
                host_id=removed["host"]["id"],
                thread_id=first["id"],
                kind="turn.start",
                payload=references,
            )
        )
        if kind == "command":
            db.session.add(
                Command(
                    host_id=kept["host"]["id"],
                    thread_id=second["id"],
                    kind="turn.start",
                    payload={**references, "legacy_number": float("nan")},
                )
            )
        else:
            db.session.add(
                Item(
                    thread_id=second["id"],
                    native_id="legacy-nan-item",
                    type="agentMessage",
                    data={**references, "legacy_number": float("nan")},
                )
            )
        db.session.commit()
        asset_id, path, snapshot_id = shared.id, Path(shared.path), snapshot.id
        table, column = ("command", "payload") if kind == "command" else ("item", "data")
        filter_kind = " AND kind = 'turn.start'" if kind == "command" else ""
        assert db.session.execute(
            text(f"SELECT json_valid({column}) FROM {table} WHERE thread_id = :thread{filter_kind}"),
            {"thread": second["id"]},
        ).scalar() == 0

    settings = api("GET", "/api/settings/hosts")
    assert settings.status_code == 200
    kept_data = next(row["data"] for row in settings.json["hosts"] if row["id"] == kept["host"]["id"])
    assert kept_data["attachments"] == 1
    response = api(
        "DELETE", f"/api/hosts/{removed['host']['id']}", json={"confirm_name": "legacy-source"}
    )
    assert response.status_code == 200
    assert path.exists()
    with app.app_context():
        value = db.session.get(Asset, asset_id)
        assert value is not None and value.host_id is None and value.thread_id is None
        assert db.session.get(Snapshot, snapshot_id) is not None


def test_deleting_without_attachment_candidates_does_not_parse_other_hosts_messages(
    app, api, paired_host, create_thread
):
    removed = paired_host("empty-source")
    kept = paired_host("unrelated-history")
    first, second = create_thread(removed), create_thread(kept)
    with app.app_context():
        kept_asset = asset(app, host_id=kept["host"]["id"], thread_id=second["id"])
        db.session.add_all(
            [
                Command(
                    host_id=kept["host"]["id"],
                    thread_id=second["id"],
                    kind="turn.start",
                    payload={
                        "_test_payload_owner": "kept",
                        "input": "unrelated history",
                        "_assets": [kept_asset.as_dict()],
                    },
                ),
                Item(
                    thread_id=second["id"],
                    native_id="unrelated-item",
                    type="agentMessage",
                    data={
                        "_test_payload_owner": "kept",
                        "text": "unrelated history",
                        "_assets": [kept_asset.as_dict()],
                    },
                ),
            ]
        )
        db.session.commit()
        raw_connection = db.engine.raw_connection()
        unrelated_reads = []

        def count_json_extractions(value, path):
            # The target has no metadata. Count extraction of unrelated rows;
            # those should never be evaluated for an empty candidate set.
            data = json.loads(value) if value is not None else None
            if isinstance(data, dict) and data.get("_test_payload_owner") == "kept":
                unrelated_reads.append(path)
            return None

        raw_connection.driver_connection.create_function("json_extract", 2, count_json_extractions)
        try:
            response = api(
                "DELETE",
                f"/api/hosts/{removed['host']['id']}",
                json={"confirm_name": "empty-source"},
            )
        finally:
            raw_connection.close()
        assert response.status_code == 200
        assert unrelated_reads == []
        assert db.session.get(Thread, first["id"]) is None
        assert db.session.get(Thread, second["id"]) is not None


def test_reference_indexes_upgrade_existing_rows_and_track_metadata_updates(tmp_path):
    app = create_app(
        {
            "SECRET_KEY": "test-migration-secret",
            "ADMIN_PASSWORD": "test-migration-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "references.db"),
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
            command.upgrade(config, "0007_thread_preferences")
        with app.app_context():
            host = Host(name="old-host", platform="test", token_hash="old-token")
            db.session.add(host)
            db.session.flush()
            thread = Thread(host_id=host.id, title="old thread", cwd="/tmp", status="idle")
            db.session.add(thread)
            db.session.flush()
            item = Item(
                thread_id=thread.id,
                native_id="old-item",
                type="agentMessage",
                data={"text": "old content"},
            )
            saved_command = Command(
                host_id=host.id, thread_id=thread.id, kind="turn.start", payload={"input": []}
            )
            db.session.add_all([item, saved_command])
            db.session.commit()
            item_id, command_id, host_id = item.id, saved_command.id, host.id

        upgrade_database(app)

        with app.app_context():
            for table, column, name in (
                ("item", "data", "ix_item_reference_metadata"),
                ("command", "payload", "ix_command_reference_metadata"),
            ):
                predicate = reference_predicate(column)
                sql = f"SELECT id FROM {table} WHERE {predicate}"
                plan = db.session.execute(text("EXPLAIN QUERY PLAN " + sql)).all()
                assert any(name in row[3] for row in plan)
                assert db.session.execute(text(sql)).all() == []

            item_join_plan = db.session.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT item.id FROM item JOIN thread "
                    "ON item.thread_id = thread.id WHERE " + reference_predicate("item.data")
                )
            ).all()
            assert any("ix_item_reference_metadata" in row[3] for row in item_join_plan)

            item = db.session.get(Item, item_id)
            saved_command = db.session.get(Command, command_id)
            assert item.data == {"text": "old content"}
            item.data = {"text": "old content", "_assets": [{"id": "asset"}]}
            saved_command.payload = {"_references": [{"snapshot_id": "snapshot"}]}
            db.session.commit()
            statements = []

            def capture_query(_conn, _cursor, statement, parameters, _context, _many):
                if "json_extract" in statement.lower():
                    statements.append((statement, parameters))

            event.listen(db.engine, "before_cursor_execute", capture_query)
            try:
                assert list(_references())
                assert list(_references(host_id))
            finally:
                event.remove(db.engine, "before_cursor_execute", capture_query)
            assert len(statements) == 4
            plans = [
                db.session.connection()
                .exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
                .all()
                for statement, parameters in statements
            ]
            assert any("ix_command_reference_metadata" in row[3] for row in plans[0])
            assert any("ix_item_reference_metadata" in row[3] for row in plans[1])
            assert any("ix_command_reference_metadata" in row[3] for row in plans[2])
            assert any("ix_item_reference_metadata" in row[3] for row in plans[3])
            assert db.session.execute(
                text("SELECT id FROM item WHERE " + reference_predicate("data"))
            ).scalars().all() == [item_id]
            assert db.session.execute(
                text("SELECT id FROM command WHERE " + reference_predicate("payload"))
            ).scalars().all() == [command_id]
            item.data = {"text": "old content"}
            saved_command.payload = {"input": []}
            db.session.commit()
            assert db.session.execute(
                text("SELECT id FROM item WHERE " + reference_predicate("data"))
            ).all() == []
            assert db.session.execute(
                text("SELECT id FROM command WHERE " + reference_predicate("payload"))
            ).all() == []
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize(
    "existing_index,expected_success",
    [
        (
            "CREATE INDEX ix_command_reference_metadata ON command (host_id, thread_id) "
            + "WHERE " + reference_predicate("payload"),
            True,
        ),
        (
            "CREATE INDEX ix_command_reference_metadata ON command (host_id, thread_id) "
            "WHERE json_array_length(payload, '$._assets') > 0",
            False,
        ),
        (
            "CREATE INDEX ix_command_reference_metadata ON command (thread_id) "
            + "WHERE " + reference_predicate("payload"),
            False,
        ),
        (
            "CREATE INDEX ix_command_reference_metadata ON item (thread_id) "
            + "WHERE " + reference_predicate("data"),
            False,
        ),
        (
            "CREATE INDEX ix_command_reference_metadata ON command (host_id, thread_id) "
            + "WHERE " + reference_predicate("payload") + ";"
            + "CREATE INDEX ix_item_reference_metadata ON item (thread_id) "
            + "WHERE " + reference_predicate("data").replace("> 0", "> 1", 1),
            False,
        ),
        (
            "CREATE INDEX ix_command_reference_metadata ON command (host_id, thread_id) "
            + "WHERE " + reference_predicate("payload") + ";"
            + "CREATE INDEX ix_item_reference_metadata ON item (thread_id) "
            + "WHERE " + reference_predicate("data"),
            True,
        ),
    ],
    ids=[
        "interrupted-after-first-index",
        "wrong-predicate",
        "wrong-columns",
        "wrong-table",
        "wrong-second-index",
        "interrupted-after-both-indexes",
    ],
)
def test_reference_index_migration_recovers_partial_creation_or_rejects_mismatch(
    tmp_path, existing_index, expected_success
):
    app = create_app(
        {
            "SECRET_KEY": "test-migration-secret",
            "ADMIN_PASSWORD": "test-migration-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "partial.db"),
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
            command.upgrade(config, "0007_thread_preferences")
            for statement in existing_index.split(";"):
                connection.exec_driver_sql(statement)
        if expected_success:
            upgrade_database(app)
        else:
            with pytest.raises(RuntimeError):
                upgrade_database(app)
        with app.app_context(), db.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if expected_success:
                assert revision == "0008_reference_indexes"
                indexes = connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name IN ('ix_command_reference_metadata', 'ix_item_reference_metadata')"
                    )
                ).scalars().all()
                assert len(indexes) == 2
            else:
                assert revision == "0007_thread_preferences"
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


def test_reference_index_migration_accepts_existing_nan_json(tmp_path):
    app = create_app(
        {
            "SECRET_KEY": "test-migration-secret",
            "ADMIN_PASSWORD": "test-migration-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "legacy-nan.db"),
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
            command.upgrade(config, "0007_thread_preferences")
        with app.app_context():
            host = Host(name="nan-host", platform="test", token_hash="nan-token")
            db.session.add(host)
            db.session.flush()
            thread = Thread(host_id=host.id, title="legacy", cwd="/tmp", status="idle")
            db.session.add(thread)
            db.session.flush()
            references = {"value": float("nan"), "_assets": [{"id": "legacy-asset"}]}
            item = Item(thread_id=thread.id, native_id="nan-item", type="native", data=references)
            saved = Command(host_id=host.id, thread_id=thread.id, kind="legacy", payload=references)
            db.session.add_all([item, saved])
            db.session.commit()
            item_id, command_id = item.id, saved.id

        upgrade_database(app)

        with app.app_context():
            for table, column, expected_id in (
                ("item", "data", item_id), ("command", "payload", command_id)
            ):
                query = f"SELECT id FROM {table} WHERE {reference_predicate(column)}"
                assert db.session.execute(text(query)).scalars().all() == [expected_id]
            item = db.session.get(Item, item_id)
            item.data = {**item.data, "other": "updated after migration"}
            db.session.commit()
            assert db.session.get(Item, item_id).data["_assets"][0]["id"] == "legacy-asset"
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize("kind", ["missing", "outside", "directory", "symlink"])
def test_cleanup_is_bounded_to_regular_files_inside_asset_directory(
    app, api, paired_host, tmp_path, kind, caplog
):
    host = paired_host("cleanup-host")
    directory = Path(app.config["ASSET_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-attachment.png"
    outside.write_bytes(b"do not delete")
    target = directory / "attachment.png"
    if kind == "outside":
        target = outside
    elif kind == "directory":
        target.mkdir()
        (target / "child").write_bytes(b"do not recursively delete")
    elif kind == "symlink":
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("Creating symlinks is not permitted on this Windows account")
    with app.app_context():
        value = Asset(
            host_id=host["host"]["id"],
            name="attachment.png",
            mime="image/png",
            size=7,
            path=str(target),
        )
        db.session.add(value)
        db.session.commit()
        asset_id = value.id
    response = api(
        "DELETE", f"/api/hosts/{host['host']['id']}", json={"confirm_name": "cleanup-host"}
    )
    assert response.status_code == 200
    result = response.get_json()
    if kind == "missing":
        assert result["cleanup"]["missing"] == 1
        assert result["cleanup"]["failed"] == 0
        assert "warning" not in result
    else:
        assert result["cleanup"]["failed"] == 1
        assert result["warning"]
        assert asset_id in caplog.text
        assert str(target) not in caplog.text + response.get_data(as_text=True)
    assert outside.read_bytes() == b"do not delete"
    if kind == "directory":
        assert (target / "child").exists()
    with app.app_context():
        assert db.session.get(Asset, asset_id) is None


def test_shared_physical_file_is_retained_for_another_asset_record(app, api, paired_host):
    removed, kept = paired_host("path-source"), paired_host("path-user")
    with app.app_context():
        first = asset(app, host_id=removed["host"]["id"])
        second = asset(app, host_id=kept["host"]["id"], path=first.path)
        db.session.commit()
        path, second_id = Path(first.path), second.id
    response = api(
        "DELETE", f"/api/hosts/{removed['host']['id']}", json={"confirm_name": "path-source"}
    )
    assert response.status_code == 200
    assert response.get_json()["cleanup"]["retained"] == 1
    assert path.exists()
    with app.app_context():
        assert db.session.get(Asset, second_id) is not None


def test_failed_database_commit_preserves_credentials_files_and_connection(
    app, api, paired_host, monkeypatch
):
    host = paired_host("rollback-host")
    host_id = host["host"]["id"]
    with app.app_context():
        record = asset(app, host_id=host_id)
        db.session.commit()
        asset_id, path = record.id, Path(record.path)
    with monkeypatch.context() as patch:

        def fail_commit():
            raise RuntimeError("simulated transaction failure")

        patch.setattr(db.session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated transaction failure"):
            api("DELETE", f"/api/hosts/{host_id}", json={"confirm_name": "rollback-host"})
    assert path.exists()
    assert app.extensions["broker"].is_attached(host_id, host["socket"])
    with app.app_context():
        assert db.session.get(Host, host_id) is not None
        assert db.session.get(Asset, asset_id) is not None
        assert not Event.query.filter_by(method="anywhere/host_deleted").count()


def test_unlink_failure_reports_incomplete_cleanup_without_exposing_paths(
    app, api, paired_host, monkeypatch, caplog
):
    host = paired_host("permission-host")
    with app.app_context():
        record = asset(app, host_id=host["host"]["id"])
        db.session.commit()
        path, asset_id = Path(record.path), record.id
    original = Path.unlink

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError(f"Secret filesystem detail: {path}")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    response = api(
        "DELETE", f"/api/hosts/{host['host']['id']}", json={"confirm_name": "permission-host"}
    )
    assert response.status_code == 200
    assert response.get_json()["cleanup"]["failed"] == 1
    assert response.get_json()["warning"]
    assert path.exists()
    assert asset_id in caplog.text
    assert str(path) not in caplog.text + response.get_data(as_text=True)


def test_a_node_upload_finishing_after_deletion_cannot_leave_an_orphan_file(
    app, client, api, paired_host, create_thread, monkeypatch
):
    host = paired_host("upload-race-host")
    thread = create_thread(host)
    directory = Path(app.config["ASSET_DIR"])
    original_write = Path.write_bytes
    writes = []

    def write_then_delete(candidate, data):
        result = original_write(candidate, data)
        if candidate.parent == directory:
            writes.append(candidate)
            deleted = api(
                "DELETE",
                f"/api/hosts/{host['host']['id']}",
                json={"confirm_name": "upload-race-host"},
            )
            assert deleted.status_code == 200
        return result

    monkeypatch.setattr(Path, "write_bytes", write_then_delete)
    image = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(image, format="PNG")
    image.seek(0)
    response = client.post(
        "/api/node/assets",
        data={"file": (image, "image.png"), "thread_id": thread["native_id"], "item_id": "output"},
        headers={"Authorization": "Bearer " + host["token"]},
    )
    assert response.status_code == 401
    assert len(writes) == 1 and not writes[0].exists()
    with app.app_context():
        assert Asset.query.count() == 0
        assert db.session.get(Host, host["host"]["id"]) is None
