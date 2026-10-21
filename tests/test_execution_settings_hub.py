from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Host, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def upgrade_host(app, host):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "capabilities": ["catalogSync", "runtimeSettings"],
        },
    )
    return host


def command_payload(app, command):
    with app.app_context():
        return db.session.get(Command, command["id"]).payload


def finish(app, host, command, result):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": command["id"],
            "result": result,
        },
    )


def test_capabilities_are_durable_scoped_and_invalidated_on_runtime_change(app, api, paired_host):
    host = upgrade_host(app, paired_host())
    path = f"/api/hosts/{host['host']['id']}/capabilities"
    first = api("POST", path, json={"cwd": host["root"]}).get_json()["command"]
    second = api("POST", path, json={"cwd": host["root"]}).get_json()["command"]
    assert second["id"] == first["id"]
    assert command_payload(app, first) == {"cwd": host["root"], "refresh": False}
    catalog = {
        "models": [{"id": "future", "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]}],
        "defaults": {"model": "future", "effort": "ultra"},
        "cwd": host["root"],
        "runtime_id": host["runtime_id"],
    }
    finish(app, host, first, catalog)
    assert (
        api("GET", path, query_string={"cwd": host["root"]}).get_json()["capabilities"] == catalog
    )
    assert (
        api("GET", path, query_string={"cwd": "/another/project"}).get_json()["capabilities"]
        is None
    )
    with app.app_context():
        db.session.remove()
        assert db.session.get(Host, host["host"]["id"]).runtime_capabilities == catalog
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": uuid4().hex,
            "capabilities": ["runtimeSettings"],
        },
    )
    assert api("GET", path).get_json()["capabilities"] is None


def test_project_defaults_thread_revision_and_command_snapshot_survive_reload(
    app, api, paired_host, create_thread
):
    host = upgrade_host(app, paired_host())
    initial = create_thread(host)
    project_path = f"/api/projects/{initial['project_id']}/settings"
    defaults = {
        "model": "native-model",
        "effort": "ultra",
        "approval_policy": "on-request",
        "sandbox_mode": "read-only",
    }
    response = api("PATCH", project_path, json={"execution_settings": defaults})
    assert response.status_code == 200
    created = api("POST", "/api/threads", json={"project_id": initial["project_id"]}).get_json()
    assert command_payload(app, created["command"])["execution_settings"] == defaults
    finish(
        app,
        host,
        created["command"],
        {
            "thread": {"id": "native-new", "cwd": host["root"]},
            "effective_settings": defaults,
            "settings_revision": 0,
        },
    )
    path = f"/api/threads/{created['thread']['id']}"
    changed = dict(defaults, effort="low")
    updated = api(
        "PATCH",
        path + "/settings",
        json={
            "execution_settings": changed,
            "settings_revision": 0,
        },
    ).get_json()["thread"]
    assert updated["settings_revision"] == 1
    assert updated["effective_settings"] == defaults
    assert updated["effective_settings_revision"] == 0
    conflict = api(
        "PATCH",
        path + "/settings",
        json={
            "execution_settings": defaults,
            "settings_revision": 0,
        },
    )
    assert conflict.status_code == 409
    message = {"text": "Reply briefly", "client_request_id": "stable-request"}
    command = api("POST", path + "/messages", json=message).get_json()["command"]
    assert command_payload(app, command)["execution_settings"] == changed
    assert command_payload(app, command)["settings_revision"] == 1
    # A retry after editing the next turn still refers to its original snapshot.
    api("PATCH", path + "/settings", json={"execution_settings": defaults, "settings_revision": 1})
    assert (
        api("POST", path + "/messages", json=message).get_json()["command"]["id"] == command["id"]
    )
    assert command_payload(app, command)["execution_settings"] == changed
    finish(
        app,
        host,
        command,
        {"turn": {"id": "turn"}, "effective_settings": changed, "settings_revision": 1},
    )
    with app.app_context():
        db.session.remove()
        thread = db.session.get(Thread, created["thread"]["id"])
        assert thread.execution_settings == defaults
        assert thread.effective_settings == changed
        assert (thread.settings_revision, thread.effective_settings_revision) == (2, 1)


def test_per_turn_override_participates_in_idempotency_and_does_not_change_defaults(
    app, api, paired_host, create_thread
):
    host = upgrade_host(app, paired_host())
    thread = create_thread(host)
    path = f"/api/threads/{thread['id']}"
    body = {"text": "hello", "client_request_id": "same", "execution_settings": {"effort": "ultra"}}
    first = api("POST", path + "/messages", json=body)
    assert first.status_code == 202
    assert command_payload(app, first.get_json()["command"])["execution_settings"] == {
        "effort": "ultra"
    }
    conflict = api(
        "POST", path + "/messages", json=dict(body, execution_settings={"effort": "low"})
    )
    assert conflict.status_code == 409
    assert api("GET", path).get_json()["thread"]["execution_settings"] == {}
    assert api("POST", path + "/steer", json=body).status_code == 400


def test_late_readback_cannot_replace_newer_applied_settings(app, api, paired_host, create_thread):
    host = upgrade_host(app, paired_host())
    thread = create_thread(host)
    with app.app_context():
        stored = db.session.get(Thread, thread["id"])
        stored.effective_settings = {"effort": "ultra"}
        stored.effective_settings_revision = 2
        db.session.commit()
    event = {
        "type": "event",
        "event_id": uuid4().hex,
        "runtime_id": host["runtime_id"],
        "method": "anywhere/execution_settings",
        "params": {
            "threadId": thread["native_id"],
            "commandId": thread.get("command_id", "missing"),
            "settings": {"effort": "low"},
            "settingsRevision": 0,
        },
    }
    handle_node_message(app, host["host"]["id"], event)
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"][
        "effective_settings"
    ] == {"effort": "ultra"}


@pytest.mark.parametrize(
    "settings",
    [
        [],
        {"unknown": True},
        {"network_access": "yes"},
        {"approval_policy": "on-failure"},
        {"model": ""},
    ],
)
def test_invalid_settings_do_not_create_commands(app, api, paired_host, create_thread, settings):
    host = upgrade_host(app, paired_host())
    thread = create_thread(host)
    with app.app_context():
        count = Command.query.count()
    response = api(
        "PATCH", f"/api/threads/{thread['id']}/settings", json={"execution_settings": settings}
    )
    assert response.status_code == 400
    with app.app_context():
        assert Command.query.count() == count


def test_old_nodes_keep_working_but_cannot_claim_runtime_settings(api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    response = api(
        "PATCH",
        f"/api/threads/{thread['id']}/settings",
        json={"execution_settings": {"model": "x"}},
    )
    assert response.status_code == 409
    assert api("POST", f"/api/hosts/{host['host']['id']}/capabilities", json={}).status_code == 409
    assert (
        api(
            "POST",
            f"/api/threads/{thread['id']}/messages",
            json={"text": "hello", "client_request_id": "old"},
        ).status_code
        == 202
    )


def test_downgraded_node_cannot_ignore_saved_thread_permissions(
    app, api, paired_host, create_thread
):
    host = upgrade_host(app, paired_host())
    thread = create_thread(host)
    path = f"/api/threads/{thread['id']}"
    assert (
        api(
            "PATCH",
            path + "/settings",
            json={
                "execution_settings": {
                    "approval_policy": "on-request",
                    "sandbox_mode": "read-only",
                }
            },
        ).status_code
        == 200
    )
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "capabilities": [],
        },
    )
    result = api(
        "POST", path + "/messages", json={"text": "continue", "client_request_id": "downgraded"}
    )
    assert result.status_code == 409
