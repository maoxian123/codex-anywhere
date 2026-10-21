import io
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command as migration_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from PIL import Image

from codex_anywhere.hub.models import Event, Item, Pairing, Snapshot, db
from codex_anywhere.hub.transport import handle_node_message


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(output, format="PNG")
    output.seek(0)
    return output


def test_login_requires_csrf_and_bad_credentials_do_not_authenticate(client):
    credentials = {"username": "admin", "password": "test-password"}
    assert client.post("/api/auth/login", json=credentials).status_code == 403
    session = client.get("/api/auth/me").get_json()
    assert session["authenticated"] is False
    invalid = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong"},
        headers={"X-CSRF-Token": session["csrf_token"]},
    )
    assert invalid.status_code == 401
    assert client.get("/api/auth/me").get_json()["authenticated"] is False


def test_mutation_requires_csrf_and_logout_revokes_access(logged_in, api):
    assert logged_in.post("/api/pairings", json={}).status_code == 403
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    assert logged_in.get("/api/hosts").status_code == 401


def test_pairing_code_is_single_use(client, api, tmp_path):
    pairing = api("POST", "/api/pairings", json={}).get_json()
    payload = {
        "code": pairing["code"],
        "name": "pair-once",
        "platform": "test",
        "version": "test",
    }
    first = client.post("/api/hosts/pair", json=payload)
    assert first.status_code in (200, 201)
    assert first.get_json()["token"]
    assert client.post("/api/hosts/pair", json=payload).status_code == 400


def test_expired_pairing_is_rejected(app, client, api, tmp_path):
    code = api("POST", "/api/pairings", json={}).get_json()["code"]
    with app.app_context():
        pairing = db.session.execute(db.select(Pairing)).scalar_one()
        pairing.expires_at = time.time() - 1
        db.session.commit()
    response = client.post(
        "/api/hosts/pair",
        json={
            "code": code,
            "name": "expired",
            "platform": "test",
            "version": "test",
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("outside", ["../outside", "../workstation-evil"])
def test_task_cwd_can_use_any_absolute_directory(api, paired_host, outside):
    host = paired_host()
    response = api(
        "POST",
        "/api/threads",
        json={
            "host_id": host["host"]["id"],
            "title": "Outside root",
            "cwd": str(Path(host["root"]) / outside),
        },
    )
    assert response.status_code == 202


def test_relative_cwd_is_rejected(api, paired_host):
    host = paired_host()
    response = api(
        "POST",
        "/api/threads",
        json={"host_id": host["host"]["id"], "cwd": ".", "title": "Relative"},
    )
    assert response.status_code == 400


def test_assets_require_authentication_and_image_validation(app, api):
    invalid = api(
        "POST",
        "/api/assets",
        data={"file": (io.BytesIO(b"This is not a PNG"), "fake.png", "image/png")},
        content_type="multipart/form-data",
    )
    assert invalid.status_code == 400
    result = api(
        "POST",
        "/api/assets",
        data={"file": (png_bytes(), "screenshot.png", "image/png")},
        content_type="multipart/form-data",
    )
    assert result.status_code in (200, 201)
    asset = result.get_json()["asset"]
    assert asset["mime"] == "image/png"
    assert api("GET", asset["url"]).status_code == 200
    assert app.test_client().get(asset["url"]).status_code == 401


def test_node_cannot_attach_images_to_another_hosts_task(client, paired_host, create_thread):
    source = paired_host("source")
    other = paired_host("other")
    other_thread = create_thread(other)
    response = client.post(
        "/api/node/assets",
        headers={"Authorization": f"Bearer {source['token']}"},
        data={
            "file": (png_bytes(), "image.png", "image/png"),
            "thread_id": other_thread["native_id"],
            "item_id": "item-other",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code in (400, 404)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/plain,hi"])
def test_web_reference_rejects_non_http_schemes(api, url):
    response = api(
        "POST", "/api/references/resolve", json={"references": [{"kind": "web", "url": url}]}
    )
    assert response.status_code == 400


def test_web_reference_keeps_user_excerpt_and_snapshot(api):
    result = api(
        "POST",
        "/api/references/resolve",
        json={
            "references": [
                {
                    "kind": "web",
                    "url": "https://example.com/article",
                    "title": "Source title",
                    "excerpt": "User selected excerpt",
                }
            ]
        },
    )
    assert result.status_code == 200
    reference = result.get_json()["references"][0]
    assert reference["url"] == "https://example.com/article"
    assert reference["excerpt"] == "User selected excerpt"
    assert reference["snapshot_id"]


def test_cross_host_task_reference_reaches_model_input(
    api, paired_host, create_thread, completed_item
):
    source_host = paired_host("source")
    target_host = paired_host("target")
    source = create_thread(source_host, "Source task")
    target = create_thread(target_host, "Target task")
    completed_item(source_host, source, "The deployment uses port 8123.")
    history = api("GET", f"/api/threads/{source['id']}").get_json()
    item_id = history["items"][-1]["id"]
    response = api(
        "POST",
        f"/api/threads/{target['id']}/messages",
        json={
            "text": "Use the cited deployment settings.",
            "client_request_id": uuid4().hex,
            "references": [{"kind": "task", "thread_id": source["id"], "item_id": item_id}],
        },
    )
    assert response.status_code == 202, response.get_json()
    command_id = response.get_json()["command"]["id"]
    sent = [json.loads(message) for message in target_host["socket"].messages]
    command = next(
        message["command"]
        for message in reversed(sent)
        if message.get("type") == "command" and message["command"]["id"] == command_id
    )
    payload_text = str(command["payload"]["input"])
    assert "The deployment uses port 8123." in payload_text
    assert "Use the cited deployment settings." in payload_text


def test_message_client_request_id_deduplicates_commands(api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    message = {"text": "Inspect the project", "client_request_id": uuid4().hex}
    first = api("POST", f"/api/threads/{thread['id']}/messages", json=message)
    second = api("POST", f"/api/threads/{thread['id']}/messages", json=message)
    assert first.status_code == second.status_code == 202
    assert first.get_json()["command"]["id"] == second.get_json()["command"]["id"]


def test_unknown_thread_is_not_a_valid_task_reference(api):
    response = api(
        "POST",
        "/api/references/resolve",
        json={"references": [{"kind": "task", "thread_id": "missing-thread"}]},
    )
    assert response.status_code in (400, 404)


def test_idempotency_key_cannot_be_reused_for_different_message(api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    request_id = uuid4().hex
    first = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Inspect only", "client_request_id": request_id},
    )
    assert first.status_code == 202
    different = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Change the files", "client_request_id": request_id},
    )
    assert different.status_code == 409


def test_replayed_event_is_projected_once_and_acknowledged(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    event_id = str(uuid4())
    frame = {
        "type": "event",
        "event_id": event_id,
        "runtime_id": host["runtime_id"],
        "seq": 1,
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": thread["native_id"],
            "turnId": "turn-test",
            "itemId": "message-test",
            "delta": "A single answer.",
        },
    }
    handle_node_message(app, host["host"]["id"], frame)
    handle_node_message(app, host["host"]["id"], frame)
    history = api("GET", f"/api/threads/{thread['id']}").get_json()
    assert history["items"][0]["data"]["text"] == "A single answer."
    with app.app_context():
        assert Event.query.filter_by(event_key=f"{host['host']['id']}:{event_id}").count() == 1
    acks = [
        message
        for message in map(json.loads, host["socket"].messages)
        if message.get("type") == "event_ack" and message.get("event_id") == event_id
    ]
    assert len(acks) == 2


def test_task_snapshot_survives_source_message_change(
    app, api, paired_host, create_thread, completed_item
):
    host = paired_host()
    thread = create_thread(host)
    completed_item(host, thread, "Original recommendation")
    reference = api(
        "POST",
        "/api/references/resolve",
        json={"references": [{"kind": "task", "thread_id": thread["id"]}]},
    ).get_json()["references"][0]
    with app.app_context():
        item = Item.query.filter_by(thread_id=thread["id"]).one()
        item.data = dict(item.data, text="Changed recommendation")
        db.session.commit()
        snapshot = db.session.get(Snapshot, reference["snapshot_id"])
        assert "Original recommendation" in snapshot.excerpt
        assert "Changed recommendation" not in snapshot.excerpt


def test_delayed_old_runtime_approval_cannot_reopen_a_pending_interaction(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": uuid4().hex, "version": "test"},
    )
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": str(uuid4()),
            "runtime_id": host["runtime_id"],
            "seq": 1,
            "method": "item/commandExecution/requestApproval",
            "rpc_id": 17,
            "params": {
                "threadId": thread["native_id"],
                "turnId": "old-turn",
                "availableDecisions": ["accept", "decline"],
            },
        },
    )
    history = api("GET", f"/api/threads/{thread['id']}").get_json()
    assert history["thread"]["status"] != "waiting"
    assert history["interactions"][0]["status"] == "expired"
    interaction_id = history["interactions"][0]["id"]
    response = api(
        "POST",
        f"/api/interactions/{interaction_id}/answer",
        json={"response": {"decision": "accept"}},
    )
    assert response.status_code == 409


def test_definitive_late_result_resolves_an_unknown_command(app, api, paired_host):
    host = paired_host()
    created = api(
        "POST",
        "/api/threads",
        json={"host_id": host["host"]["id"], "cwd": host["root"], "title": "Late result"},
    )
    assert created.status_code == 202
    command_id = created.get_json()["command"]["id"]
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": uuid4().hex, "version": "test"},
    )
    assert api("GET", f"/api/commands/{command_id}").get_json()["command"]["status"] == "unknown"
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": command_id,
            "result": {"thread": {"id": "late-native-thread"}},
        },
    )
    assert api("GET", f"/api/commands/{command_id}").get_json()["command"]["status"] == "completed"
    thread_id = created.get_json()["thread"]["id"]
    resolved = api("GET", f"/api/threads/{thread_id}").get_json()["thread"]
    assert resolved["native_id"] == "late-native-thread"
    assert resolved["status"] == "idle"
    assert resolved["last_error"] is None


def test_approval_uses_original_rpc_id_and_only_advertised_decision(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": str(uuid4()),
            "runtime_id": host["runtime_id"],
            "seq": 1,
            "method": "item/commandExecution/requestApproval",
            "rpc_id": 24,
            "params": {"threadId": thread["native_id"], "availableDecisions": ["decline"]},
        },
    )
    history = api("GET", f"/api/threads/{thread['id']}").get_json()
    interaction_id = history["interactions"][0]["id"]
    endpoint = f"/api/interactions/{interaction_id}/answer"
    assert api("POST", endpoint, json={"response": {"decision": "accept"}}).status_code == 400
    response = api("POST", endpoint, json={"response": {"decision": "decline"}})
    assert response.status_code == 202
    wire = json.loads(host["socket"].messages[-1])["command"]
    assert wire["kind"] == "interaction.answer"
    assert wire["payload"]["rpc_id"] == 24
    assert wire["payload"]["runtime_id"] == host["runtime_id"]
    assert wire["payload"]["response"] == {"decision": "decline"}


def native_event(app, host, thread, method, params, runtime_id=None):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": str(uuid4()),
            "runtime_id": runtime_id or host["runtime_id"],
            "seq": 1,
            "method": method,
            "params": {"threadId": thread["native_id"], **params},
        },
    )


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        (
            "error",
            {"error": {"message": "Model quota exhausted", "additionalDetails": "Retry tomorrow"}},
            "Model quota exhausted\nRetry tomorrow",
        ),
        (
            "turn/completed",
            {
                "turn": {
                    "id": "failed-turn",
                    "status": "failed",
                    "error": {"message": "Tool denied"},
                }
            },
            "Tool denied",
        ),
    ],
)
def test_native_failure_detail_survives_history_reload(
    app, api, paired_host, create_thread, method, params, expected
):
    host = paired_host()
    thread = create_thread(host)
    native_event(app, host, thread, method, params)
    # Each request opens a fresh ORM scope; no WebSocket or in-memory UI state is required.
    for _ in range(2):
        history = api("GET", f"/api/threads/{thread['id']}").get_json()
        assert history["thread"]["status"] == "error"
        assert history["thread"]["last_error"] == expected
    listed = api("GET", "/api/threads").get_json()["threads"]
    assert next(row for row in listed if row["id"] == thread["id"])["last_error"] == expected


@pytest.mark.parametrize("kind", ["thread.start", "turn.start"])
def test_failed_command_detail_is_persisted_on_its_task(app, api, paired_host, create_thread, kind):
    host = paired_host()
    if kind == "thread.start":
        created = api(
            "POST",
            "/api/threads",
            json={"host_id": host["host"]["id"], "cwd": host["root"], "title": "Fail creation"},
        ).get_json()
        thread_id = created["thread"]["id"]
    else:
        thread = create_thread(host)
        thread_id = thread["id"]
        created = api(
            "POST",
            f"/api/threads/{thread_id}/messages",
            json={"text": "Run task", "client_request_id": uuid4().hex},
        ).get_json()
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "command_result", "id": created["command"]["id"], "error": "Native RPC rejected"},
    )
    result = api("GET", f"/api/threads/{thread_id}").get_json()["thread"]
    assert result["status"] == "error"
    assert result["last_error"] == "Native RPC rejected"


def test_new_native_turn_clears_old_error(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    native_event(app, host, thread, "error", {"error": {"message": "Old failure"}})
    native_event(app, host, thread, "turn/started", {"turn": {"id": "new-turn"}})
    result = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert result["status"] == "active"
    assert result["active_turn_id"] == "new-turn"
    assert result["last_error"] is None


def test_new_message_clears_old_error_before_the_native_turn_starts(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    native_event(app, host, thread, "error", {"error": {"message": "Previous failure"}})
    accepted = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Try again", "client_request_id": uuid4().hex},
    )
    assert accepted.status_code == 202
    result = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert result["status"] == "active"
    assert result["last_error"] is None


def test_old_epoch_cannot_overwrite_live_error_or_status(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    pending = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Old runtime command", "client_request_id": uuid4().hex},
    ).get_json()["command"]
    epoch = uuid4().hex
    handle_node_message(
        app, host["host"]["id"], {"type": "hello", "runtime_id": epoch, "version": "test"}
    )
    interrupted = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert interrupted["status"] == "interrupted"
    assert "未知" in interrupted["last_error"]
    native_event(app, host, thread, "error", {"error": {"message": "Current failure"}}, epoch)
    native_event(app, host, thread, "error", {"error": {"message": "Stale failure"}})
    native_event(app, host, thread, "turn/started", {"turn": {"id": "stale-turn"}})
    native_event(
        app,
        host,
        thread,
        "turn/completed",
        {"turn": {"id": "stale-turn", "status": "completed", "items": []}},
    )
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "command_result", "id": pending["id"], "result": {"turn": {"id": "old-turn"}}},
    )
    result = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert result["status"] == "error"
    assert result["last_error"] == "Current failure"
    assert result["active_turn_id"] is None
    outcome = api("GET", f"/api/commands/{pending['id']}").get_json()["command"]
    assert outcome["status"] == "completed"


def test_last_error_migration_preserves_existing_tasks_and_matches_models(app):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "codex_anywhere" / "migrations"),
    )
    with app.app_context():
        db.drop_all()
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            migration_command.upgrade(config, "0001_initial")
            connection.execute(
                sa.text(
                    "INSERT INTO host (id, name, platform, roots, token_hash) "
                    "VALUES ('host-old', 'Old host', 'test', '[]', 'old-token-hash')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO thread (id, host_id, title, cwd, status, created_at, updated_at) "
                    "VALUES ('task-old', 'host-old', 'Existing task', '/project', 'idle', 1, 1)"
                )
            )
            migration_command.upgrade(config, "head")
            row = connection.execute(
                sa.text("SELECT title, last_error FROM thread WHERE id = 'task-old'")
            ).one()
            assert row.title == "Existing task" and row.last_error is None
            assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
