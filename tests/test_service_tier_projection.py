"""Native service tiers are durable readbacks, never browser preferences."""

from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Event, Thread, db
from codex_anywhere.hub.transport import handle_node_message

OMITTED = object()
DESIRED = {"model": "chosen-model", "effort": "high", "sandbox_mode": "read-only"}
APPLIED = {"model": "native-model", "effort": "medium", "network_access": False}


def seed(app, thread, tier=OMITTED):
    with app.app_context():
        stored = db.session.get(Thread, thread["id"])
        stored.execution_settings = dict(DESIRED)
        stored.settings_revision = 7
        stored.effective_settings = {
            **APPLIED,
            **({} if tier is OMITTED else {"service_tier": tier}),
        }
        stored.effective_settings_revision = 6
        db.session.commit()


def read(api, thread):
    return api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]


def event(app, host, thread, settings, runtime_id=None, method="thread/settings/updated"):
    event_id = uuid4().hex
    params = {"threadId": thread["native_id"]}
    if method == "anywhere/service_tier":
        params.update(settings)
    else:
        params["threadSettings"] = settings
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": event_id,
            "runtime_id": runtime_id or host["runtime_id"],
            "method": method,
            "params": params,
        },
    )
    return event_id, params


def pending_command(app, host, thread, kind="turn.start", revision=6, runtime_id=None):
    with app.app_context():
        command = Command(
            host_id=host["host"]["id"],
            thread_id=thread["id"],
            kind=kind,
            runtime_id=runtime_id or host["runtime_id"],
            status="running",
            payload={"settings_revision": revision},
        )
        db.session.add(command)
        db.session.commit()
        return command.id


@pytest.mark.parametrize("tier", ["fast", "priority", "default", None, "custom-tier", ""])
def test_native_tier_event_is_durable_without_changing_browser_settings_or_revisions(
    app, api, paired_host, create_thread, tier
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread)
    event_id, params = event(app, host, thread, {"serviceTier": tier, "opaqueNativeSetting": 3})
    current = read(api, thread)
    assert current["effective_settings"] == {**APPLIED, "service_tier": tier}
    assert current["execution_settings"] == DESIRED
    assert (current["settings_revision"], current["effective_settings_revision"]) == (7, 6)
    with app.app_context():
        db.session.remove()
        assert db.session.get(Thread, thread["id"]).effective_settings["service_tier"] == tier
        recorded = Event.query.filter_by(event_key=f"{host['host']['id']}:{event_id}").one()
        assert recorded.params == params


@pytest.mark.parametrize(
    "native",
    [
        None,
        {},
        {"model": "ignored"},
        {"serviceTier": True},
        {"serviceTier": []},
        {"serviceTier": {"name": "fast"}},
    ],
)
def test_missing_or_malformed_native_event_does_not_change_confirmed_mode(
    app, api, paired_host, create_thread, native
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread, "fast")
    event(app, host, thread, native)
    assert read(api, thread)["effective_settings"] == {**APPLIED, "service_tier": "fast"}


def test_unknown_and_explicit_null_are_distinct_and_live_changes_are_immediate(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread)
    assert "service_tier" not in read(api, thread)["effective_settings"]
    for tier in ["fast", None, "priority", "default", "future-tier"]:
        event(app, host, thread, {"serviceTier": tier})
        assert read(api, thread)["effective_settings"]["service_tier"] == tier
    assert read(api, thread)["execution_settings"] == DESIRED


@pytest.mark.parametrize("tier", ["fast", None])
def test_old_node_readback_without_tier_keeps_native_event_mode(
    app, api, paired_host, create_thread, tier
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread, tier)
    command_id = pending_command(app, host, thread)
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "method": "anywhere/execution_settings",
            "params": {
                "threadId": thread["native_id"],
                "commandId": command_id,
                "settings": {"model": "applied-new-model", "effort": "low"},
                "settingsRevision": 6,
            },
        },
    )
    current = read(api, thread)
    assert current["effective_settings"] == {
        "model": "applied-new-model",
        "effort": "low",
        "service_tier": tier,
    }
    assert current["execution_settings"] == DESIRED
    assert current["effective_settings_revision"] == 6


@pytest.mark.parametrize("kind", ["thread.start", "turn.start"])
@pytest.mark.parametrize("tier", ["fast", None, "custom-tier"])
def test_old_node_top_level_native_result_projects_tier_without_writing_desired_settings(
    app, api, paired_host, create_thread, kind, tier
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread, "priority")
    command_id = pending_command(app, host, thread, kind)
    result = {"serviceTier": tier}
    if kind == "thread.start":
        result["thread"] = {"id": thread["native_id"], "cwd": host["root"]}
    else:
        result["turn"] = {"id": "native-turn"}
    handle_node_message(
        app, host["host"]["id"], {"type": "command_result", "id": command_id, "result": result}
    )
    current = read(api, thread)
    assert current["effective_settings"]["service_tier"] == tier
    assert current["execution_settings"] == DESIRED
    assert (current["settings_revision"], current["effective_settings_revision"]) == (7, 6)


def test_old_revision_and_old_runtime_results_cannot_replace_current_mode(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread, "fast")
    for revision, runtime in [(5, host["runtime_id"]), (6, "old-runtime")]:
        command_id = pending_command(app, host, thread, revision=revision, runtime_id=runtime)
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "command_result",
                "id": command_id,
                "result": {"turn": {"id": uuid4().hex}, "serviceTier": None},
            },
        )
        assert read(api, thread)["effective_settings"] == {**APPLIED, "service_tier": "fast"}
    event(app, host, thread, {"serviceTier": None}, runtime_id="old-runtime")
    assert read(api, thread)["effective_settings"]["service_tier"] == "fast"


def test_runtime_restart_clears_only_that_hosts_tier_and_same_epoch_reconnect_preserves_it(
    app, api, paired_host, create_thread
):
    host = paired_host()
    other = paired_host("other-workstation")
    thread, another = create_thread(host), create_thread(other)
    seed(app, thread, "fast")
    seed(app, another, "priority")
    handle_node_message(
        app, host["host"]["id"], {"type": "hello", "runtime_id": host["runtime_id"]}
    )
    assert read(api, thread)["effective_settings"]["service_tier"] == "fast"
    new_epoch = uuid4().hex
    handle_node_message(app, host["host"]["id"], {"type": "hello", "runtime_id": new_epoch})
    current = read(api, thread)
    assert current["effective_settings"] == APPLIED
    assert current["execution_settings"] == DESIRED
    assert current["settings_revision"] == 7
    assert current["effective_settings_revision"] == 6
    assert read(api, another)["effective_settings"]["service_tier"] == "priority"
    event(app, host, thread, {"serviceTier": "fast"})
    assert "service_tier" not in read(api, thread)["effective_settings"]
    event(app, host, thread, {"serviceTier": None}, runtime_id=new_epoch)
    assert read(api, thread)["effective_settings"]["service_tier"] is None


@pytest.mark.parametrize("tier", ["priority", "ultrafast", True])
def test_browser_cannot_set_unsupported_service_tier_on_any_execution_settings_endpoint(
    app, api, paired_host, create_thread, tier
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread, "priority")
    with app.app_context():
        before = Command.query.count()
    paths = [
        ("POST", "/api/threads", {"project_id": thread["project_id"]}),
        ("PATCH", f"/api/projects/{thread['project_id']}/settings", {}),
        ("PATCH", f"/api/threads/{thread['id']}/settings", {}),
        (
            "POST",
            f"/api/threads/{thread['id']}/messages",
            {"text": "hello", "client_request_id": uuid4().hex},
        ),
    ]
    for method, path, payload in paths:
        response = api(method, path, json={**payload, "execution_settings": {"service_tier": tier}})
        assert response.status_code == 400, response.get_json()
    with app.app_context():
        assert Command.query.count() == before
    assert read(api, thread)["effective_settings"]["service_tier"] == "priority"
    assert read(api, thread)["execution_settings"] == DESIRED


@pytest.mark.parametrize("tier", [None, "fast", "default", "custom-tier"])
def test_resume_mode_observation_is_durable_runtime_scoped_and_does_not_mark_settings_applied(
    app, api, paired_host, create_thread, tier
):
    host = paired_host()
    thread = create_thread(host)
    seed(app, thread)
    event_id, params = event(
        app, host, thread, {"serviceTier": tier}, method="anywhere/service_tier"
    )
    current = read(api, thread)
    assert current["effective_settings"] == {**APPLIED, "service_tier": tier}
    assert current["execution_settings"] == DESIRED
    assert (current["settings_revision"], current["effective_settings_revision"]) == (7, 6)
    with app.app_context():
        recorded = Event.query.filter_by(event_key=f"{host['host']['id']}:{event_id}").one()
        assert recorded.params == params
        assert recorded.method == "anywhere/service_tier"
    event(
        app,
        host,
        thread,
        {"serviceTier": "obsolete"},
        runtime_id="old-runtime",
        method="anywhere/service_tier",
    )
    assert read(api, thread)["effective_settings"]["service_tier"] == tier
    event(app, host, thread, {}, method="anywhere/service_tier")
    assert read(api, thread)["effective_settings"]["service_tier"] == tier
