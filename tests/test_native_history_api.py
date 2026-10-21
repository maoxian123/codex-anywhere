from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def discovered(app, paired_host):
    host = paired_host()
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "capabilities": ["catalogSync", "lazyHistory", "runtimeSettings"],
        },
    )
    with app.app_context():
        thread = Thread(
            host_id=host["host"]["id"],
            native_id="native-lazy",
            title="Local",
            cwd=host["root"],
            status="idle",
            imported=True,
            history_loaded=False,
        )
        db.session.add(thread)
        db.session.commit()
        return host, thread.id


def test_lazy_history_waits_for_durable_page_projection_and_reuses_command(app, api, paired_host):
    host, ident = discovered(app, paired_host)
    endpoint = f"/api/threads/{ident}/history"
    first = api("POST", endpoint, json={"limit": 2}).get_json()
    assert first["command"]["kind"] == "thread.history"
    assert first["thread"]["status"] == "importing"
    command_id = first["command"]["id"]
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": command_id,
            "result": {"itemCount": 1},
        },
    )
    pending = api("POST", endpoint, json={"limit": 2}).get_json()
    assert pending["command"]["id"] == command_id and not pending["thread"]["history_loaded"]
    generation = "a" * 64

    def event(suffix, values):
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "event",
                "event_id": uuid4().hex,
                "runtime_id": host["runtime_id"],
                "method": "anywhere/history_" + suffix,
                "params": {
                    "threadId": "native-lazy",
                    "commandId": command_id,
                    "generation": generation,
                    **values,
                },
            },
        )

    event(
        "metadata",
        {
            "thread": {"id": "native-lazy", "cwd": host["root"], "_history_fingerprint": "b" * 64},
            "snapshotUpdatedAt": 100,
        },
    )
    event(
        "item",
        {
            "item": {"id": "native-item", "type": "agentMessage", "text": "restored"},
            "turnId": "turn",
            "index": 0,
        },
    )
    event(
        "complete", {"itemCount": 1, "snapshotUpdatedAt": 100, "nextCursor": None, "hasMore": False}
    )
    detail = api("GET", f"/api/threads/{ident}?item_limit=2").get_json()
    assert detail["thread"]["history_loaded"] and detail["thread"]["history_complete"]
    assert detail["thread"]["status"] == "idle"
    assert detail["items"][0]["data"]["text"] == "restored"
    assert api("POST", endpoint, json={}).get_json()["command"] is None


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_native_page_retry_is_explicit(app, api, paired_host, status):
    _, ident = discovered(app, paired_host)
    endpoint = f"/api/threads/{ident}/history"
    first = api("POST", endpoint, json={}).get_json()["command"]
    with app.app_context():
        command = db.session.get(Command, first["id"])
        command.status = status
        db.session.get(Thread, ident).status = "error"
        db.session.commit()
    for _ in range(2):
        assert api("POST", endpoint, json={}).status_code == 409
    retry = api("POST", endpoint, json={"retry": True})
    assert retry.status_code == 202
    assert retry.get_json()["command"]["id"] != first["id"]


def test_invalid_native_cursor_does_not_issue_command(app, api, paired_host):
    _, ident = discovered(app, paired_host)
    with app.app_context():
        thread = db.session.get(Thread, ident)
        thread.history_loaded = True
        thread.history_cursor = "current"
        db.session.commit()
    assert (
        api("POST", f"/api/threads/{ident}/history", json={"cursor": "different"}).status_code
        == 409
    )
    with app.app_context():
        assert Command.query.filter_by(thread_id=ident).count() == 0


def test_new_turn_supersedes_pending_page_without_replaying_it(app, api, paired_host):
    _, ident = discovered(app, paired_host)
    with app.app_context():
        thread = db.session.get(Thread, ident)
        thread.history_loaded = True
        thread.history_stale = True
        db.session.commit()
    history = api("POST", f"/api/threads/{ident}/history", json={}).get_json()["command"]
    sent = api(
        "POST",
        f"/api/threads/{ident}/messages",
        json={"text": "continue", "client_request_id": "new-turn"},
    )
    assert sent.status_code == 202
    with app.app_context():
        command = db.session.get(Command, history["id"])
        assert command.status == "failed"
        assert "未应用" in command.error


def test_live_turn_invalidates_partial_native_cursor_before_next_catalog_scan(
    app, api, paired_host
):
    host, ident = discovered(app, paired_host)
    with app.app_context():
        thread = db.session.get(Thread, ident)
        thread.history_loaded = True
        thread.history_complete = False
        thread.history_cursor = "previous-native-snapshot"
        thread.history_stale = False
        thread.status = "active"
        thread.active_turn_id = "new-turn"
        db.session.commit()
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "method": "turn/completed",
            "params": {
                "threadId": "native-lazy",
                "turn": {
                    "id": "new-turn",
                    "status": "completed",
                    "items": [],
                },
            },
        },
    )
    result = api("GET", f"/api/threads/{ident}?item_limit=1").get_json()["thread"]
    assert result["history_stale"] is True and result["status"] == "idle"
    blocked = api(
        "POST", f"/api/threads/{ident}/history", json={"cursor": "previous-native-snapshot"}
    )
    assert blocked.status_code == 409
    refreshed = api("POST", f"/api/threads/{ident}/history", json={})
    assert refreshed.status_code == 202
    assert refreshed.get_json()["command"]["kind"] == "thread.history"
