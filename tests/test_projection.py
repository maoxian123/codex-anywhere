"""Regressions for late results, concurrent approvals and visible user input."""

import json
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def event(app, host, thread, method, params, rpc_id=None):
    frame = dict(
        type="event",
        event_id=uuid4().hex,
        runtime_id=host["runtime_id"],
        seq=1,
        method=method,
        params=dict(threadId=thread["native_id"], **params),
    )
    if rpc_id is not None:
        frame["rpc_id"] = rpc_id
    handle_node_message(app, host["host"]["id"], frame)


def start(api, thread, text):
    response = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json=dict(text=text, client_request_id=uuid4().hex),
    )
    assert response.status_code == 202
    return response.get_json()["command"]


def history(api, thread):
    return api("GET", f"/api/threads/{thread['id']}").get_json()


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("new_started", [False, True])
def test_old_same_runtime_command_result_cannot_replace_new_turn(
    app, api, paired_host, create_thread, failed, new_started
):
    host = paired_host()
    thread = create_thread(host)
    old = start(api, thread, "First turn")
    event(app, host, thread, "turn/started", {"turn": {"id": "old"}})
    event(app, host, thread, "turn/completed", {"turn": {"id": "old", "status": "completed"}})
    with app.app_context():
        # The current API waits for the old start RPC before delivering another
        # turn. Preserve coverage of legacy, already-delivered concurrent state.
        previous = db.session.get(Command, old["id"])
        db.session.add(
            Command(
                host_id=host["host"]["id"],
                thread_id=thread["id"],
                runtime_id=host["runtime_id"],
                kind="turn.start",
                status="sent",
                payload={
                    "native_thread_id": thread["native_id"],
                    "input": [{"type": "text", "text": "Second turn"}],
                },
                created_at=previous.created_at + 0.001,
            )
        )
        stored = db.session.get(Thread, thread["id"])
        stored.status = "active"
        db.session.commit()
    if new_started:
        event(app, host, thread, "turn/started", {"turn": {"id": "new"}})
    result = {"error": "Late rejection"} if failed else {"result": {"turn": {"id": "old"}}}
    handle_node_message(
        app, host["host"]["id"], dict(type="command_result", id=old["id"], **result)
    )
    state = history(api, thread)["thread"]
    assert state["status"] == "active"
    assert state["active_turn_id"] == ("new" if new_started else None)
    assert state["last_error"] is None


@pytest.mark.parametrize("new_started", [False, True])
def test_late_completion_preserves_history_without_finishing_the_current_turn(
    app, api, paired_host, create_thread, new_started
):
    host = paired_host()
    thread = create_thread(host)
    start(api, thread, "Current turn")
    if new_started:
        event(app, host, thread, "turn/started", {"turn": {"id": "new"}})
    event(
        app,
        host,
        thread,
        "turn/completed",
        {
            "turn": {
                "id": "old",
                "status": "failed",
                "error": {"message": "Old failure"},
                "items": [{"id": "old-answer", "type": "agentMessage", "text": "Old answer"}],
            }
        },
    )
    result = history(api, thread)
    assert result["thread"]["active_turn_id"] == ("new" if new_started else None)
    assert result["thread"]["status"] == "active"
    assert any(item["native_id"] == "old-answer" for item in result["items"])


def test_retrying_error_does_not_unlock_a_second_turn(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    event(app, host, thread, "turn/started", {"turn": {"id": "retrying"}})
    event(
        app,
        host,
        thread,
        "error",
        {"turnId": "retrying", "willRetry": True, "error": {"message": "Transient stream failure"}},
    )
    state = history(api, thread)["thread"]
    assert state["status"] == "active"
    assert state["last_error"] is None
    blocked = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json=dict(text="Do not execute concurrently", client_request_id=uuid4().hex),
    )
    assert blocked.status_code == 202
    queued = blocked.get_json()["command"]
    assert queued["kind"] == "turn.queue"
    assert queued["status"] == "pending"
    assert not any(
        frame.get("type") == "command" and frame["command"]["id"] == queued["id"]
        for frame in map(json.loads, host["socket"].messages)
    )


def test_old_completion_cannot_clear_the_current_turn_failure(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    event(app, host, thread, "turn/started", {"turn": {"id": "new"}})
    event(
        app,
        host,
        thread,
        "error",
        {"turnId": "new", "willRetry": False, "error": {"message": "Current turn failed"}},
    )
    event(app, host, thread, "turn/completed", {"turn": {"id": "old", "status": "completed"}})
    state = history(api, thread)["thread"]
    assert state["status"] == "error"
    assert state["active_turn_id"] == "new"
    assert state["last_error"] == "Current turn failed"


def test_resolving_one_of_two_approvals_keeps_task_waiting(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    event(app, host, thread, "turn/started", {"turn": {"id": "turn"}})
    for rpc_id in (11, 12):
        event(
            app,
            host,
            thread,
            "item/commandExecution/requestApproval",
            dict(turnId="turn", availableDecisions=["accept", "decline"]),
            rpc_id,
        )
    event(app, host, thread, "serverRequest/resolved", {"requestId": 11})
    result = history(api, thread)
    assert result["thread"]["status"] == "waiting"
    pending = next(item for item in result["interactions"] if item["status"] == "pending")
    response = api(
        "POST",
        f"/api/interactions/{pending['id']}/answer",
        json={"response": {"decision": "decline"}},
    )
    assert response.status_code == 202
    # Native cleanup can arrive before the answer command's result.
    event(app, host, thread, "serverRequest/resolved", {"requestId": 12})
    result = history(api, thread)
    assert result["thread"]["status"] == "active"
    assert {item["status"] for item in result["interactions"]} == {"resolved"}


def test_completed_answer_does_not_hide_another_pending_approval(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    for rpc_id in (21, 22):
        event(
            app,
            host,
            thread,
            "item/commandExecution/requestApproval",
            dict(turnId="turn", availableDecisions=["accept", "decline"]),
            rpc_id,
        )
    interaction = history(api, thread)["interactions"][0]
    command = api(
        "POST",
        f"/api/interactions/{interaction['id']}/answer",
        json={"response": {"decision": "decline"}},
    ).get_json()["command"]
    handle_node_message(
        app,
        host["host"]["id"],
        dict(type="command_result", id=command["id"], result={"answered": True}),
    )
    assert history(api, thread)["thread"]["status"] == "waiting"


@pytest.mark.parametrize("ended", ["turn/completed", "anywhere/runtime_stopped", "new-epoch"])
def test_answer_in_flight_expires_when_its_turn_or_process_ends(
    app, api, paired_host, create_thread, ended
):
    host = paired_host()
    thread = create_thread(host)
    event(app, host, thread, "turn/started", {"turn": {"id": "turn"}})
    event(
        app,
        host,
        thread,
        "item/commandExecution/requestApproval",
        dict(turnId="turn", availableDecisions=["decline"]),
        31,
    )
    interaction = history(api, thread)["interactions"][0]
    api(
        "POST",
        f"/api/interactions/{interaction['id']}/answer",
        json={"response": {"decision": "decline"}},
    )
    if ended == "new-epoch":
        handle_node_message(app, host["host"]["id"], dict(type="hello", runtime_id=uuid4().hex))
    else:
        event(app, host, thread, ended, {"turn": {"id": "turn", "status": "interrupted"}})
    assert history(api, thread)["interactions"][0]["status"] == "expired"


def test_steer_user_item_uses_its_own_text_and_reference_metadata(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    initial = start(api, thread, "Original goal")
    event(app, host, thread, "turn/started", {"turn": {"id": "turn"}})
    event(
        app,
        host,
        thread,
        "item/completed",
        dict(
            turnId="turn",
            item={
                "id": "original-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "Original goal"}],
            },
        ),
    )
    steer = api(
        "POST",
        f"/api/threads/{thread['id']}/steer",
        json=dict(
            text="Use this source",
            client_request_id=uuid4().hex,
            references=[
                dict(
                    kind="web", url="https://example.com/", title="Source", excerpt="Quoted context"
                )
            ],
        ),
    ).get_json()["command"]
    with app.app_context():
        payload = db.session.get(Command, steer["id"]).payload
    event(
        app,
        host,
        thread,
        "item/completed",
        dict(
            turnId="turn",
            item={"id": "steer-user", "type": "userMessage", "content": payload["input"]},
        ),
    )
    items = history(api, thread)["items"]
    visible = next(item["data"] for item in items if item["native_id"] == "steer-user")
    assert visible["_displayText"] == "Use this source"
    assert visible["_references"][0]["title"] == "Source"
    with app.app_context():
        assert db.session.get(Command, initial["id"]).payload["_attached_item"] == "original-user"


def test_user_metadata_never_binds_a_different_message(app, api, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    start(api, thread, "Managed input")
    event(
        app,
        host,
        thread,
        "item/completed",
        dict(
            turnId="turn",
            item={
                "id": "unrelated-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "Different input"}],
            },
        ),
    )
    visible = history(api, thread)["items"][0]["data"]
    assert "_displayText" not in visible
