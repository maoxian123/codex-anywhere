import json
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Item, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def event(app, host, method="anywhere/catalog_sync", epoch=None, **params):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": epoch or host["runtime_id"],
            "method": method,
            "params": params,
        },
    )


def metadata(native_id="local-session", version=1700000100, cwd="C:\\Projects\\Auto"):
    return {
        "id": native_id,
        "cwd": cwd,
        "name": "Local conversation",
        "createdAt": 1700000000,
        "updatedAt": version,
        "preview": "Only metadata",
        "turns": [],
    }


def test_hello_negotiates_auto_sync_only_with_a_capable_node(app, paired_host, pump_nodes):
    host = paired_host()
    initial = [
        json.loads(raw) for raw in host["socket"].messages if json.loads(raw)["type"] == "welcome"
    ][-1]
    assert "capabilities" not in initial
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "version": "test",
            "capabilities": ["catalogSync"],
        },
    )
    pump_nodes()
    welcome = [
        json.loads(raw) for raw in host["socket"].messages if json.loads(raw)["type"] == "welcome"
    ][-1]
    assert welcome["capabilities"]["catalogSync"] == {"recentLimit": 50, "intervalSeconds": 60}


def test_discovery_creates_bounded_summaries_and_projects_without_history_or_commands(
    app, api, paired_host
):
    host = paired_host()
    entries = [
        metadata(f"native-{index}", cwd=f"C:\\Projects\\Folder{index % 2}") for index in range(70)
    ]
    event(app, host, threads=entries, limit=50, scanned_count=50)
    threads = api("GET", "/api/threads").get_json()["threads"]
    assert len(threads) == 50
    assert all(
        t["history_loaded"] is False and t["status"] == "idle" and t["project_id"] for t in threads
    )
    assert len(api("GET", "/api/projects").get_json()["projects"]) == 2
    with app.app_context():
        assert Item.query.count() == 0 and Command.query.count() == 0


def test_delta_and_reconnect_catalogs_upsert_without_deleting_cached_conversations(
    app, api, paired_host
):
    host = paired_host()
    event(app, host, threads=[metadata(), metadata("old-session")])
    first = api("GET", "/api/threads").get_json()["threads"]
    event(app, host, threads=[metadata(version=1700000200, cwd="C:\\Another")])
    event(app, host, threads=[])
    second = api("GET", "/api/threads").get_json()["threads"]
    assert {t["id"] for t in second} == {t["id"] for t in first}
    updated = next(t for t in second if t["native_id"] == "local-session")
    assert updated["cwd"] == "C:\\Another" and updated["native_updated_at"] == 1700000200
    host_info = api("GET", "/api/hosts").get_json()["hosts"][0]
    assert host_info["last_sync_at"] and host_info["sync_error"] is None


def test_unchanged_and_older_metadata_do_not_invalidate_a_current_snapshot(app, api, paired_host):
    host = paired_host()
    event(app, host, threads=[metadata()])
    with app.app_context():
        thread = Thread.query.one()
        thread.history_loaded = True
        thread.history_native_updated_at = 1700000100
        db.session.commit()
    event(app, host, threads=[metadata()])
    event(app, host, threads=[metadata(version=1700000050, cwd="C:\\Stale")])
    thread = api("GET", "/api/threads").get_json()["threads"][0]
    assert not thread["history_stale"] and thread["cwd"] == "C:\\Projects\\Auto"
    event(app, host, threads=[metadata(version=1700000200)])
    assert api("GET", "/api/threads").get_json()["threads"][0]["history_stale"] is True


@pytest.mark.parametrize("status", ["active", "waiting", "starting", "importing"])
def test_auto_catalog_preserves_live_task_state_and_approvals(
    app, api, paired_host, create_thread, status
):
    host = paired_host()
    created = create_thread(host, title="Running title")
    with app.app_context():
        thread = db.session.get(Thread, created["id"])
        thread.status = status
        thread.active_turn_id = "live-turn"
        thread.last_error = "Current diagnostic"
        db.session.commit()
    event(app, host, threads=[metadata(created["native_id"], cwd="C:\\Changed")])
    thread = api("GET", f"/api/threads/{created['id']}").get_json()["thread"]
    assert thread["status"] == status and thread["active_turn_id"] == "live-turn"
    assert thread["last_error"] == "Current diagnostic" and thread["title"] == "Running title"
    assert thread["cwd"] == created["cwd"]


def test_old_runtime_catalog_is_retained_as_evidence_without_projecting_it(app, api, paired_host):
    host = paired_host()
    event(app, host, epoch="dead-runtime", threads=[metadata()])
    assert not api("GET", "/api/threads").get_json()["threads"]
    event(app, host, method="anywhere/catalog_sync_error", error="Codex temporarily unavailable")
    assert (
        api("GET", "/api/hosts").get_json()["hosts"][0]["sync_error"]
        == "Codex temporarily unavailable"
    )
    event(app, host, threads=[])
    assert api("GET", "/api/hosts").get_json()["hosts"][0]["sync_error"] is None


def test_bad_catalog_entries_do_not_block_valid_projects(app, api, paired_host):
    host = paired_host()
    event(app, host, threads=[{"id": "bad", "cwd": "relative"}, metadata(), {"id": []}])
    assert len(api("GET", "/api/threads").get_json()["threads"]) == 1
    assert "Skipped 2" in api("GET", "/api/hosts").get_json()["hosts"][0]["sync_error"]


def test_a_new_catalog_version_during_import_remains_stale_until_the_next_snapshot(
    app, api, paired_host
):
    host = paired_host()
    event(app, host, threads=[metadata()])
    thread = api("GET", "/api/threads").get_json()["threads"][0]
    loading = api("POST", f"/api/threads/{thread['id']}/history", json={}).get_json()
    params = {"threadId": thread["native_id"], "commandId": loading["command"]["id"]}
    event(app, host, "anywhere/import_metadata", **params, thread=metadata())
    event(app, host, threads=[metadata(version=1700000200)])
    event(app, host, "anywhere/import_complete", **params, itemCount=0)
    state = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert state["history_loaded"] and state["history_stale"]
    assert state["history_native_updated_at"] == 1700000100
    assert state["native_updated_at"] == 1700000200 and state["history_synced_at"]
    refreshed = api("POST", f"/api/threads/{thread['id']}/history", json={}).get_json()
    assert refreshed["command"]["id"] != loading["command"]["id"]
    params["commandId"] = refreshed["command"]["id"]
    event(app, host, "anywhere/import_metadata", **params, thread=metadata(version=1700000200))
    event(app, host, "anywhere/import_complete", **params, itemCount=0)
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["history_stale"] is False


def test_live_turn_projection_does_not_download_its_own_history_again(
    app, api, paired_host, create_thread
):
    host = paired_host()
    created = create_thread(host)
    event(
        app,
        host,
        "turn/completed",
        threadId=created["native_id"],
        turn={
            "id": "fresh-turn",
            "status": "completed",
            "completedAt": 1700000300,
            "items": [],
        },
    )
    event(app, host, threads=[metadata(created["native_id"], version=1700000300)])
    state = api("GET", f"/api/threads/{created['id']}").get_json()["thread"]
    assert state["history_native_updated_at"] == 1700000300 and not state["history_stale"]
    response = api("POST", f"/api/threads/{created['id']}/history", json={})
    assert response.status_code == 200 and response.get_json()["command"] is None
