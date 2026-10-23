"""Queue behavior through browser APIs and fake native-node events."""

import io
import json
from uuid import uuid4

import pytest
from PIL import Image

from codex_anywhere.hub.models import Command, Item, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def endpoint(thread):
    return f"/api/threads/{thread['id']}"


def send(api, thread, text, **kwargs):
    result = api(
        "POST",
        endpoint(thread) + "/messages",
        json={"text": text, "client_request_id": uuid4().hex, **kwargs},
    )
    assert result.status_code == 202, result.get_json()
    return result.get_json()["command"]


def event(app, host, thread, method, params, *, runtime_id=None, event_id=None, rpc_id=None):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": event_id or uuid4().hex,
            "runtime_id": runtime_id or host["runtime_id"],
            "seq": 1,
            "method": method,
            **({"rpc_id": rpc_id} if rpc_id is not None else {}),
            "params": {"threadId": thread["native_id"], **params},
        },
    )


def result(app, host, command, native_result=None, *, error=None):
    frame = {"type": "command_result", "id": command["id"]}
    frame["error" if error is not None else "result"] = (
        error if error is not None else (native_result or {})
    )
    handle_node_message(app, host["host"]["id"], frame)


def start(app, api, host, thread, turn_id="first-turn"):
    command = send(api, thread, "First request")
    result(app, host, command, {"turn": {"id": turn_id}})
    event(app, host, thread, "turn/started", {"turn": {"id": turn_id}})
    return command


def complete(app, host, thread, turn_id="first-turn", status="completed", **kwargs):
    event(
        app,
        host,
        thread,
        "turn/completed",
        {"turn": {"id": turn_id, "status": status, "items": []}},
        **kwargs,
    )


def queue(api, thread):
    response = api("GET", endpoint(thread) + "/queue")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def wires(host, *, kind="turn.start"):
    return [
        frame["command"]
        for raw in host["socket"].messages
        if (frame := json.loads(raw)).get("type") == "command" and frame["command"]["kind"] == kind
    ]


def assert_pending(api, thread, *commands):
    state = queue(api, thread)
    assert [message["id"] for message in state["messages"]] == [
        command["id"] for command in commands
    ]
    return state


def test_busy_send_is_persisted_and_survives_browser_logout(
    app, api, paired_host, create_thread, logged_in
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Run the checks afterwards")
    assert (pending["kind"], pending["status"]) == ("turn.queue", "pending")
    assert len(wires(host)) == 1
    state = assert_pending(api, thread, pending)
    history = api("GET", endpoint(thread)).get_json()
    assert history["message_queue"] == state
    assert state["paused"] is False
    message = state["messages"][0]
    assert message["text"] == "Run the checks afterwards"
    assert message["revision"] == 0
    assert message["created_at"] and message["updated_at"]
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    complete(app, host, thread)
    # No connected or authenticated browser is required to dispatch saved work.
    for node_id, socket in app.extensions["_fake_nodes"].items():
        app.extensions["broker"].pump(node_id, socket, limit=128)
    assert [command["id"] for command in wires(host)][-1] == pending["id"]
    assert logged_in.get(endpoint(thread)).status_code == 401


def test_pending_start_rpc_also_queues_the_next_message(api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    first = send(api, thread, "Start work")
    second = send(api, thread, "Then summarize")
    assert_pending(api, thread, second)
    assert [command["id"] for command in wires(host)] == [first["id"]]


def test_new_message_after_completion_waits_for_previous_start_result_to_settle(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    first = send(api, thread, "First request")
    event(app, host, thread, "turn/started", {"turn": {"id": "first-turn"}})
    complete(app, host, thread)
    pending = send(api, thread, "Arrives before the first RPC result")
    assert (pending["kind"], pending["status"]) == ("turn.queue", "pending")
    assert_pending(api, thread, pending)
    assert [command["id"] for command in wires(host)] == [first["id"]]
    result(app, host, first, {"turn": {"id": "first-turn"}})
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)] == [first["id"], pending["id"]]


def test_completion_before_rpc_result_and_duplicate_events_dispatch_one_message(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    first = send(api, thread, "First request")
    event(app, host, thread, "turn/started", {"turn": {"id": "first-turn"}})
    second = send(api, thread, "Second request")
    third = send(api, thread, "Third request")
    completion_id = uuid4().hex
    complete(app, host, thread, event_id=completion_id)
    result(app, host, first, {"turn": {"id": "first-turn"}})
    assert_pending(api, thread, third)
    assert [command["id"] for command in wires(host)] == [first["id"], second["id"]]
    result(app, host, second, {"turn": {"id": "second-turn"}})
    event(app, host, thread, "turn/started", {"turn": {"id": "second-turn"}})
    complete(app, host, thread, event_id=completion_id)
    complete(app, host, thread)
    result(app, host, first, {"turn": {"id": "first-turn"}})
    assert_pending(api, thread, third)
    detail = api("GET", endpoint(thread)).get_json()["thread"]
    assert detail["active_turn_id"] == "second-turn"
    assert detail["status"] == "active"
    assert len(wires(host)) == 2
    complete(app, host, thread, "second-turn")
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)][-1] == third["id"]


def test_pending_and_answering_approval_block_dispatch_until_answer_settles(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    event(
        app,
        host,
        thread,
        "item/commandExecution/requestApproval",
        {"turnId": "first-turn", "availableDecisions": ["accept", "decline"]},
        rpc_id=31,
    )
    approval_id = api("GET", endpoint(thread)).get_json()["interactions"][0]["id"]
    second = send(api, thread, "After approval work is finished")
    assert_pending(api, thread, second)
    assert len(wires(host)) == 1
    response = api(
        "POST",
        f"/api/interactions/{approval_id}/answer",
        json={"response": {"decision": "accept"}},
    )
    assert response.status_code == 202, response.get_json()
    answer = response.get_json()["command"]
    complete(app, host, thread)
    assert_pending(api, thread, second)
    assert len(wires(host)) == 1
    result(app, host, answer, {})
    assert_pending(api, thread)
    assert len(wires(host)) == 2


@pytest.mark.parametrize("outcome", ["failed", "interrupted"])
def test_unsuccessful_turn_pauses_queue_and_resume_never_retries_old_command(
    app, api, paired_host, create_thread, outcome
):
    host = paired_host()
    thread = create_thread(host)
    original = start(app, api, host, thread)
    second = send(api, thread, "Only after I resume")
    complete(app, host, thread, status=outcome)
    state = assert_pending(api, thread, second)
    assert state["paused"] is True
    assert state.get("reason")
    assert len(wires(host)) == 1
    resumed = api("POST", endpoint(thread) + "/queue/resume", json={})
    assert resumed.status_code in (200, 202), resumed.get_json()
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)] == [original["id"], second["id"]]
    api("POST", endpoint(thread) + "/queue/resume", json={})
    assert len(wires(host)) == 2


def test_runtime_change_pauses_queue_and_old_completion_cannot_release_it(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    original = start(app, api, host, thread)
    second = send(api, thread, "Review after reconnect")
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": uuid4().hex, "version": "test"},
    )
    complete(app, host, thread)
    assert assert_pending(api, thread, second)["paused"] is True
    assert [command["id"] for command in wires(host)] == [original["id"]]


def test_unknown_execution_requires_resume_without_reissuing_uncertain_message(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    original = send(api, thread, "May have started")
    event(app, host, thread, "turn/started", {"turn": {"id": "first-turn"}})
    pending = send(api, thread, "Only continue after confirmation")
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": original["id"],
            "unknown": True,
            "error": "Native connection lost after delivery",
        },
    )
    complete(app, host, thread)
    assert assert_pending(api, thread, pending)["paused"] is True
    assert len(wires(host)) == 1
    resumed = api("POST", endpoint(thread) + "/queue/resume", json={})
    assert resumed.status_code in (200, 202), resumed.get_json()
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)] == [original["id"], pending["id"]]
    state = api("GET", f"/api/commands/{original['id']}").get_json()["command"]
    assert state["status"] == "unknown"


def test_same_runtime_reconnect_resumes_pending_work_on_each_idle_conversation(
    app, api, paired_host, create_thread
):
    host = paired_host()
    threads = [create_thread(host, f"Conversation {number}") for number in range(2)]
    pending = []
    for number, thread in enumerate(threads):
        start(app, api, host, thread, f"first-turn-{number}")
        pending.append(send(api, thread, f"Pending follow-up {number}"))
    app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    for number, thread in enumerate(threads):
        complete(app, host, thread, f"first-turn-{number}")
        assert_pending(api, thread, pending[number])
    assert len(wires(host)) == 2
    assert app.extensions["broker"].attach(host["host"]["id"], host["socket"], ready=True)
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "test"},
    )
    for thread in threads:
        assert_pending(api, thread)
    assert {command["id"] for command in wires(host)[-2:]} == {command["id"] for command in pending}


def test_queue_retries_edit_and_cancel_use_one_durable_command(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    request_id = uuid4().hex
    pending = send(api, thread, "Original wording", client_request_id=request_id)
    retried = send(api, thread, "Original wording", client_request_id=request_id)
    assert retried["id"] == pending["id"]
    path = endpoint(thread) + f"/queue/{pending['id']}"
    message = assert_pending(api, thread, pending)["messages"][0]
    changed = api(
        "PATCH", path, json={"text": "Corrected wording", "revision": message["revision"]}
    )
    assert changed.status_code == 200, changed.get_json()
    current = assert_pending(api, thread, pending)["messages"][0]
    assert current["text"] == "Corrected wording"
    assert current["revision"] > message["revision"]
    assert (
        api("PATCH", path, json={"text": "Stale edit", "revision": message["revision"]}).status_code
        == 409
    )
    assert api("DELETE", path, json={"revision": message["revision"]}).status_code == 409
    assert api("DELETE", path, json={"revision": current["revision"]}).status_code == 200
    assert_pending(api, thread)
    # Retrying the original HTTP request cannot resurrect cancelled work.
    retried = send(api, thread, "Original wording", client_request_id=request_id)
    assert retried["id"] == pending["id"]
    assert retried["status"] == "cancelled"
    complete(app, host, thread)
    assert_pending(api, thread)
    assert len(wires(host)) == 1


def test_dispatched_message_cannot_be_edited_or_cancelled(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Execute this later")
    revision = assert_pending(api, thread, pending)["messages"][0]["revision"]
    complete(app, host, thread)
    path = endpoint(thread) + f"/queue/{pending['id']}"
    assert api("PATCH", path, json={"text": "Too late", "revision": revision}).status_code == 409
    assert api("DELETE", path, json={"revision": revision}).status_code == 409
    assert len(wires(host)) == 2


def test_immediate_steer_keeps_pending_messages_in_place(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Later request")
    response = api(
        "POST",
        endpoint(thread) + "/steer",
        json={"text": "Adjust the current work now", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 202, response.get_json()
    assert [command["id"] for command in wires(host, kind="turn.steer")] == [
        response.get_json()["command"]["id"]
    ]
    assert_pending(api, thread, pending)
    assert len(wires(host)) == 1


def test_new_send_cannot_bypass_paused_fifo_even_if_thread_is_idle(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    second = send(api, thread, "Second request")
    complete(app, host, thread, status="failed")
    with app.app_context():
        db.session.get(Thread, thread["id"]).status = "idle"
        db.session.commit()
    third = send(api, thread, "Third request")
    assert assert_pending(api, thread, second, third)["paused"] is True
    assert len(wires(host)) == 1
    assert api("POST", endpoint(thread) + "/queue/resume", json={}).status_code in (200, 202)
    assert_pending(api, thread, third)
    assert [command["id"] for command in wires(host)][-1] == second["id"]


def test_new_native_turn_does_not_implicitly_resume_an_interrupted_queue(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Keep this paused until I resume")
    complete(app, host, thread, status="interrupted")
    assert assert_pending(api, thread, pending)["paused"] is True
    # A turn started from the native desktop may update the projection while the
    # browser queue is paused; that is not an instruction to resume saved work.
    event(app, host, thread, "turn/started", {"turn": {"id": "desktop-turn"}})
    complete(app, host, thread, "desktop-turn")
    assert assert_pending(api, thread, pending)["paused"] is True
    assert len(wires(host)) == 1
    resumed = api("POST", endpoint(thread) + "/queue/resume", json={})
    assert resumed.status_code in (200, 202), resumed.get_json()
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)][-1] == pending["id"]


def test_terminal_pause_is_committed_before_opportunistic_dispatch(
    app, api, paired_host, create_thread, monkeypatch
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Only execute after explicit resume")
    # A bounded host-wide scan may skip this thread. Its terminal event must
    # persist the pause atomically, independently of that optional scan.
    with monkeypatch.context() as patch:
        patch.setattr(
            "codex_anywhere.hub.message_queue.dispatch_ready_queues",
            lambda *_args, **_kwargs: [],
        )
        complete(app, host, thread, status="interrupted")
        event(app, host, thread, "turn/started", {"turn": {"id": "desktop-turn"}})
        complete(app, host, thread, "desktop-turn")
        assert assert_pending(api, thread, pending)["paused"] is True
    handle_node_message(app, host["host"]["id"], {"type": "ping"})
    assert assert_pending(api, thread, pending)["paused"] is True
    assert len(wires(host)) == 1
    resumed = api("POST", endpoint(thread) + "/queue/resume", json={})
    assert resumed.status_code in (200, 202), resumed.get_json()
    assert_pending(api, thread)
    assert [command["id"] for command in wires(host)][-1] == pending["id"]


def test_next_turn_settings_are_captured_at_dispatch_time(app, api, paired_host, create_thread):
    host = paired_host()
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "capabilities": ["runtimeSettings", "serviceTierWrite"],
        },
    )
    thread = create_thread(host)
    start(app, api, host, thread)
    pending = send(api, thread, "Use the chosen next turn settings")
    changed = api(
        "PATCH",
        endpoint(thread) + "/settings",
        json={"execution_settings": {"service_tier": "fast"}},
    )
    assert changed.status_code == 200
    complete(app, host, thread)
    queue(api, thread)
    dispatched = next(command for command in wires(host) if command["id"] == pending["id"])
    assert dispatched["payload"]["execution_settings"] == {"service_tier": "fast"}


@pytest.mark.parametrize("blocked", ["offline", "unloaded_history"])
def test_unavailable_node_or_history_does_not_accept_new_queued_work(
    app, api, paired_host, create_thread, blocked
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    if blocked == "offline":
        app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    else:
        with app.app_context():
            db.session.get(Thread, thread["id"]).history_loaded = False
            db.session.commit()
    response = api(
        "POST",
        endpoint(thread) + "/messages",
        json={"text": "Cannot queue", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 409, response.get_json()
    with app.app_context():
        assert Command.query.filter_by(thread_id=thread["id"], kind="turn.queue").count() == 0


def test_queued_assets_and_references_are_public_metadata_and_frozen_snapshots(
    app, api, paired_host, create_thread, completed_item
):
    host = paired_host()
    thread = create_thread(host)
    source = create_thread(host, "Source conversation")
    completed_item(host, source, "Original context before waiting")
    source_item = api("GET", endpoint(source)).get_json()["items"][0]
    image = io.BytesIO()
    Image.new("RGB", (2, 2), color="blue").save(image, "PNG")
    image.seek(0)
    uploaded = api(
        "POST",
        "/api/assets",
        data={"file": (image, "context.png", "image/png")},
        content_type="multipart/form-data",
    ).get_json()["asset"]
    start(app, api, host, thread)
    pending = send(
        api,
        thread,
        "Use this context later",
        asset_ids=[uploaded["id"]],
        references=[{"kind": "task", "thread_id": source["id"], "item_id": source_item["id"]}],
    )
    state = assert_pending(api, thread, pending)
    shown = state["messages"][0]
    assert shown["assets"][0]["id"] == uploaded["id"]
    assert shown["references"][0]["snapshot_id"]
    serialized = json.dumps(state)
    assert "data:image/" not in serialized and "base64," not in serialized
    with app.app_context():
        stored_source = db.session.get(Item, source_item["id"])
        stored_source.data = dict(stored_source.data, text="Changed source after queuing")
        db.session.commit()
    path = endpoint(thread) + f"/queue/{pending['id']}"
    assert (
        api(
            "PATCH", path, json={"text": "Use this frozen context", "revision": shown["revision"]}
        ).status_code
        == 200
    )
    complete(app, host, thread)
    queue(api, thread)
    dispatched = next(command for command in wires(host) if command["id"] == pending["id"])
    payload = dispatched["payload"]
    assert payload["_display_text"] == "Use this frozen context"
    assert "Original context before waiting" in str(payload["input"])
    assert "Changed source after queuing" not in str(payload["input"])
    assert uploaded["id"] in str(payload)
