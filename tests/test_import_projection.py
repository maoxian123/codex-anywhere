from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Event, Project, db
from codex_anywhere.hub.transport import handle_node_message


def begin_import(api, host, native="import-native"):
    response = api(
        "POST",
        f"/api/hosts/{host['host']['id']}/imports",
        json={"native_thread_id": native, "title": "Catalog hint", "cwd": "/catalog/hint"},
    )
    assert response.status_code == 202
    return response.get_json()


def project_event(app, host, imported, method, **params):
    event_id = uuid4().hex
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": event_id,
            "runtime_id": host["runtime_id"],
            "seq": 1,
            "method": method,
            "params": {
                "threadId": imported["thread"]["native_id"],
                "commandId": imported["command"]["id"],
                **params,
            },
        },
    )
    return event_id


def metadata(app, host, imported):
    project_event(
        app,
        host,
        imported,
        "anywhere/import_metadata",
        thread={
            "id": imported["thread"]["native_id"],
            "name": "Native history",
            "cwd": "/canonical/project",
            "createdAt": 1700000000,
            "updatedAt": 1700000100,
            "source": "cli",
            "turns": [{"not": "stored in command result"}],
        },
    )


def summary(app, host, imported, **outcome):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": imported["command"]["id"],
            **outcome,
        },
    )


def detail(api, imported):
    return api("GET", f"/api/threads/{imported['thread']['id']}").get_json()


def test_import_metadata_and_items_keep_native_payload_and_stable_order(app, api, paired_host):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    original = {}
    for position in (3, 1, 2):
        item = {
            "id": f"native-{position}",
            "type": "unknownNativeTool",
            "nested": {"opaque": [position, {"preserve": True}]},
        }
        original[item["id"]] = item
        project_event(
            app,
            host,
            imported,
            "anywhere/import_item",
            position=position,
            turnId=f"turn-{position}",
            item=item,
            createdAt=1700000000 + position,
        )
    project_event(app, host, imported, "anywhere/import_complete", itemCount=3)
    result = detail(api, imported)
    assert [item["position"] for item in result["items"]] == [1, 2, 3]
    assert all(item["data"] == original[item["native_id"]] for item in result["items"])
    state = result["thread"]
    assert state["title"] == "Native history" and state["cwd"] == "/canonical/project"
    assert state["history_loaded"] is True and state["imported"] is True
    assert state["status"] == "idle" and state["active_turn_id"] is None
    assert state["created_at"].startswith("2023-11-14")
    with app.app_context():
        project = db.session.get(Project, state["project_id"])
        assert project.cwd == "/canonical/project"
        saved_metadata = db.session.get(Command, imported["command"]["id"]).result["thread"]
        assert saved_metadata["source"] == "cli" and "turns" not in saved_metadata


def test_rpc_summary_does_not_make_history_ready_before_durable_completion(app, api, paired_host):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    summary(app, host, imported, result={"itemCount": 1})
    assert detail(api, imported)["thread"]["history_loaded"] is False
    response = api(
        "POST",
        f"/api/threads/{imported['thread']['id']}/messages",
        json={"text": "Still importing", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 409
    project_event(
        app,
        host,
        imported,
        "anywhere/import_item",
        position=1,
        turnId="native-turn",
        item={"id": "native-user", "type": "userMessage", "content": []},
    )
    project_event(app, host, imported, "anywhere/import_complete", itemCount=1)
    assert detail(api, imported)["thread"]["history_loaded"] is True
    accepted = api(
        "POST",
        f"/api/threads/{imported['thread']['id']}/messages",
        json={"text": "Now ready", "client_request_id": uuid4().hex},
    )
    assert accepted.status_code == 202
    with app.app_context():
        result = db.session.get(Command, imported["command"]["id"]).result
        assert result["thread"]["name"] == "Native history" and result["itemCount"] == 1


def test_late_import_events_do_not_overwrite_a_running_thread(app, api, paired_host):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    response = api(
        "POST",
        f"/api/threads/{imported['thread']['id']}/messages",
        json={"text": "New turn", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 202
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "seq": 2,
            "method": "turn/started",
            "params": {
                "threadId": imported["thread"]["native_id"],
                "turn": {"id": "new-live-turn"},
            },
        },
    )
    ignored = project_event(
        app,
        host,
        imported,
        "anywhere/import_metadata",
        thread={"id": imported["thread"]["native_id"], "name": "Stale name", "cwd": "/stale/path"},
    )
    project_event(
        app,
        host,
        imported,
        "anywhere/import_item",
        position=10,
        item={"id": "stale-item", "type": "agentMessage", "text": "Late item"},
    )
    project_event(app, host, imported, "anywhere/import_complete", itemCount=100)
    result = detail(api, imported)
    assert result["thread"]["status"] == "active"
    assert result["thread"]["active_turn_id"] == "new-live-turn"
    assert result["thread"]["title"] == "Native history"
    assert not result["items"]
    with app.app_context():
        assert Event.query.filter_by(event_key=f"{host['host']['id']}:{ignored}").count() == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_import_failure_stays_unloaded_and_a_manual_retry_reuses_the_thread(
    app, api, paired_host, unknown
):
    host = paired_host()
    imported = begin_import(api, host)
    summary(app, host, imported, error="Cannot read stored history", unknown=unknown)
    state = detail(api, imported)["thread"]
    assert state["status"] == "error"
    assert state["history_loaded"] is False
    assert state["last_error"] == "Cannot read stored history"
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    assert detail(api, imported)["thread"]["history_loaded"] is False
    retry = begin_import(api, host)
    assert retry["thread"]["id"] == imported["thread"]["id"]
    assert retry["command"]["id"] != imported["command"]["id"]


def test_project_open_maps_the_nodes_canonical_directory_to_a_hub_project(app, api, paired_host):
    host = paired_host()
    opened = api(
        "POST",
        "/api/projects",
        json={"host_id": host["host"]["id"], "cwd": "/requested/link", "name": "Requested"},
    ).get_json()["command"]
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": opened["id"],
            "result": {"project": {"cwd": "/resolved/real-project", "name": "Resolved"}},
        },
    )
    result = api("GET", f"/api/commands/{opened['id']}").get_json()["command"]
    project = result["result"]["project"]
    assert result["status"] == "completed"
    assert project["cwd"] == "/resolved/real-project" and project["name"] == "Resolved"
    task = api("POST", "/api/threads", json={"project_id": project["id"], "title": "Canonical"})
    assert task.status_code == 202
    assert task.get_json()["thread"]["cwd"] == "/resolved/real-project"
    assert task.get_json()["thread"]["project_id"] == project["id"]


@pytest.mark.parametrize("unknown", [False, True])
def test_late_import_command_failure_cannot_replace_a_new_active_turn(
    app, api, paired_host, unknown
):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    accepted = api(
        "POST",
        f"/api/threads/{imported['thread']['id']}/messages",
        json={"text": "Continue original conversation", "client_request_id": uuid4().hex},
    )
    assert accepted.status_code == 202
    project_event(app, host, imported, "turn/started", turn={"id": "current-turn"})
    summary(app, host, imported, error="Late import failure", unknown=unknown)
    state = detail(api, imported)["thread"]
    assert state["status"] == "active"
    assert state["active_turn_id"] == "current-turn"
    assert state["last_error"] is None


def test_reimport_reflects_native_rollback_only_when_new_snapshot_completes(app, api, paired_host):
    host = paired_host()
    original = begin_import(api, host)
    metadata(app, host, original)
    for position in (1, 2):
        project_event(
            app,
            host,
            original,
            "anywhere/import_item",
            position=position,
            item={"id": f"old-{position}", "type": "agentMessage", "text": str(position)},
        )
    project_event(app, host, original, "anywhere/import_complete", itemCount=2)
    summary(app, host, original, result={"itemCount": 2})
    fresh = begin_import(api, host)
    metadata(app, host, fresh)
    project_event(
        app,
        host,
        fresh,
        "anywhere/import_item",
        position=1,
        item={"id": "old-1", "type": "agentMessage", "text": "Revised"},
    )
    assert len(detail(api, fresh)["items"]) == 2
    project_event(app, host, fresh, "anywhere/import_complete", itemCount=1)
    items = detail(api, fresh)["items"]
    assert len(items) == 1 and items[0]["native_id"] == "old-1"
    assert items[0]["data"]["text"] == "Revised"
    with app.app_context():
        assert Event.query.filter_by(method="anywhere/import_item").count() == 3


def test_late_import_events_cannot_replace_a_ready_snapshot_even_after_a_new_turn_ends(
    app, api, paired_host
):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    project_event(
        app,
        host,
        imported,
        "turn/completed",
        turn={
            "id": "later-turn",
            "status": "completed",
            "items": [
                {"id": "later-answer", "type": "agentMessage", "text": "New answer"},
            ],
        },
    )
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    project_event(
        app,
        host,
        imported,
        "anywhere/import_metadata",
        thread={
            "id": imported["thread"]["native_id"],
            "cwd": "/stale",
            "name": "Late title",
        },
    )
    result = detail(api, imported)
    assert result["thread"]["title"] == "Native history"
    assert [item["native_id"] for item in result["items"]] == ["later-answer"]


def test_durable_import_completion_survives_a_lost_rpc_summary_and_runtime_restart(
    app, api, paired_host
):
    host = paired_host()
    imported = begin_import(api, host)
    metadata(app, host, imported)
    project_event(app, host, imported, "anywhere/import_complete", itemCount=0)
    command = api("GET", f"/api/commands/{imported['command']['id']}").get_json()["command"]
    assert command["status"] == "completed" and command["result"]["itemCount"] == 0
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": "restarted-runtime",
            "version": "test",
        },
    )
    state = detail(api, imported)["thread"]
    assert state["status"] == "idle" and state["history_loaded"] is True
