import json
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Item, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def discover(app, host, **overrides):
    values = dict(
        host_id=host["host"]["id"],
        native_id="native-" + uuid4().hex,
        title="Discovered local conversation",
        cwd=host["root"],
        imported=True,
        history_loaded=False,
        history_stale=False,
        native_updated_at=100,
        status="idle",
    )
    values.update(overrides)
    with app.app_context():
        thread = Thread(**values)
        db.session.add(thread)
        db.session.commit()
        return thread.as_dict()


def import_wires(host):
    return [
        message["command"]
        for raw in host["socket"].messages
        if (message := json.loads(raw)).get("type") == "command"
        and message["command"]["kind"] == "thread.import"
    ]


@pytest.mark.parametrize("stale", [False, True])
def test_opening_discovered_or_stale_history_starts_one_durable_import(
    app, api, paired_host, stale
):
    host = paired_host()
    thread = discover(app, host, history_loaded=stale, history_stale=stale)
    endpoint = f"/api/threads/{thread['id']}/history"
    results = [api("POST", endpoint, json={}) for _ in range(5)]
    assert all(response.status_code == 202 for response in results)
    commands = [response.get_json()["command"] for response in results]
    assert len({command["id"] for command in commands}) == 1
    assert all(command["kind"] == "thread.import" for command in commands)
    state = results[-1].get_json()["thread"]
    assert state["status"] == "importing" and state["history_loaded"] is False
    assert state["history_stale"] is stale
    assert len(import_wires(host)) == 1
    assert import_wires(host)[0]["payload"] == {"native_thread_id": thread["native_id"]}
    with app.app_context():
        assert Command.query.filter_by(thread_id=thread["id"]).count() == 1
        db.session.remove()
        persisted = db.session.get(Thread, thread["id"])
        assert persisted.status == "importing" and persisted.history_loaded is False


def test_current_history_is_a_noop(app, api, paired_host):
    host = paired_host()
    thread = discover(app, host, history_loaded=True, history_native_updated_at=100)
    response = api("POST", f"/api/threads/{thread['id']}/history", json={})
    assert response.status_code == 200
    assert response.get_json()["command"] is None
    assert response.get_json()["thread"]["history_loaded"] is True
    assert import_wires(host) == []


@pytest.mark.parametrize("status", ["active", "waiting", "starting"])
def test_history_loading_does_not_interrupt_live_tasks(app, api, paired_host, status):
    host = paired_host()
    thread = discover(
        app,
        host,
        status=status,
        active_turn_id="live-turn",
        history_stale=True,
        last_error="existing detail",
    )
    response = api("POST", f"/api/threads/{thread['id']}/history", json={})
    assert response.status_code == 200
    assert response.get_json()["command"] is None
    state = response.get_json()["thread"]
    assert (state["status"], state["active_turn_id"], state["last_error"]) == (
        status,
        "live-turn",
        "existing detail",
    )
    assert import_wires(host) == []


def test_history_reuses_import_after_rpc_summary_until_completion(app, api, paired_host):
    host = paired_host()
    thread = discover(app, host)
    endpoint = f"/api/threads/{thread['id']}/history"
    first = api("POST", endpoint, json={}).get_json()
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {"type": "command_result", "id": first["command"]["id"], "result": {"itemCount": 0}},
        )
    response = api("POST", endpoint, json={})
    assert response.status_code == 202
    result = response.get_json()
    assert result["command"]["id"] == first["command"]["id"]
    assert result["command"]["status"] == "completed"
    assert result["thread"]["history_loaded"] is False
    assert len(import_wires(host)) == 1


@pytest.mark.parametrize("outcome", ["unknown", "failed"])
def test_unsuccessful_import_requires_explicit_retry(app, api, paired_host, outcome):
    host = paired_host()
    thread = discover(app, host, history_stale=True)
    endpoint = f"/api/threads/{thread['id']}/history"
    original = api("POST", endpoint, json={}).get_json()["command"]
    with app.app_context():
        command = db.session.get(Command, original["id"])
        command.status = outcome
        command.error = "native import detail"
        stored = db.session.get(Thread, thread["id"])
        stored.status = "error"
        stored.last_error = command.error
        db.session.commit()
    for _ in range(3):
        response = api("POST", endpoint, json={})
        assert response.status_code == 409
        assert response.get_json()["command"]["id"] == original["id"]
        assert "手动重试" in response.get_json()["error"]
    assert len(import_wires(host)) == 1
    retry = api(
        "POST",
        f"/api/hosts/{host['host']['id']}/imports",
        json={"native_thread_id": thread["native_id"]},
    )
    assert retry.status_code == 202
    assert retry.get_json()["command"]["id"] != original["id"]
    assert retry.get_json()["thread"]["last_error"] is None
    assert retry.get_json()["thread"]["history_stale"] is True
    assert len(import_wires(host)) == 2


def test_history_requires_online_host_even_if_cached(app, api, paired_host):
    host = paired_host()
    thread = discover(app, host, history_loaded=True)
    app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    response = api("POST", f"/api/threads/{thread['id']}/history", json={})
    assert response.status_code == 409
    assert "离线" in response.get_json()["error"]


def test_history_requires_native_identity(app, api, paired_host):
    host = paired_host()
    thread = discover(app, host, native_id=None)
    response = api("POST", f"/api/threads/{thread['id']}/history", json={})
    assert response.status_code == 409
    assert import_wires(host) == []


def test_history_load_validates_json_and_missing_thread(api):
    assert api("POST", "/api/threads/missing/history", json={}).status_code == 404
    assert api("POST", "/api/threads/missing/history", json=[]).status_code == 400


def add_history(app, thread, count):
    with app.app_context():
        for position in range(1, count + 1):
            db.session.add(
                Item(
                    thread_id=thread["id"],
                    native_id=f"item-{position}",
                    type="agentMessage",
                    data={"text": f"Message {position}"},
                    position=position * 10,
                    created_at=count - position,
                )
            )
        db.session.commit()


def test_history_paging_reads_latest_then_exclusive_older_pages(app, api, paired_host):
    thread = discover(app, paired_host())
    add_history(app, thread, 7)
    endpoint = f"/api/threads/{thread['id']}?item_limit=3"
    latest = api("GET", endpoint).get_json()
    assert [item["position"] for item in latest["items"]] == [50, 60, 70]
    assert latest["has_more_items"] is True and latest["next_before_position"] == 50
    older = api("GET", endpoint + "&before_position=50").get_json()
    assert [item["position"] for item in older["items"]] == [20, 30, 40]
    assert older["has_more_items"] is True and older["next_before_position"] == 20
    oldest = api("GET", endpoint + "&before_position=20").get_json()
    assert [item["position"] for item in oldest["items"]] == [10]
    assert oldest["has_more_items"] is False and oldest["next_before_position"] is None
    empty = api("GET", endpoint + "&before_position=0").get_json()
    assert empty["items"] == [] and empty["has_more_items"] is False
    assert empty["next_before_position"] is None


def test_paging_defaults_and_legacy_unbounded_response(app, api, paired_host):
    thread = discover(app, paired_host())
    add_history(app, thread, 205)
    endpoint = f"/api/threads/{thread['id']}"
    legacy = api("GET", endpoint).get_json()
    assert len(legacy["items"]) == 205
    assert "has_more_items" not in legacy and "next_before_position" not in legacy
    default_page = api("GET", endpoint + "?before_position=2060").get_json()
    assert len(default_page["items"]) == 100
    assert default_page["items"][0]["position"] == 1060
    assert default_page["next_before_position"] == 1060
    largest = api("GET", endpoint + "?item_limit=200").get_json()
    assert len(largest["items"]) == 200 and largest["has_more_items"] is True


@pytest.mark.parametrize(
    "query",
    [
        "item_limit=0",
        "item_limit=201",
        "item_limit=-1",
        "item_limit=1.5",
        "item_limit=true",
        "item_limit=",
        "before_position=-1",
        "before_position=1.5",
        "before_position=false",
        "before_position=",
        f"before_position={2**63}",
    ],
)
def test_history_paging_validation(app, api, paired_host, query):
    thread = discover(app, paired_host())
    response = api("GET", f"/api/threads/{thread['id']}?{query}")
    assert response.status_code == 400
    assert isinstance(response.get_json()["error"], str)
