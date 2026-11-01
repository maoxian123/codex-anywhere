"""Async questions are durable messages; replies are ordinary native user input."""

import asyncio
import copy
import json
import sys
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Interaction, Item, db
from codex_anywhere.hub.transport import handle_node_message
from codex_anywhere.node.runtime import CodexRuntime
from codex_anywhere.node.service import NodeService


def emit(app, host, thread, method, params, *, rpc_id=None, event_id=None):
    frame = {
        "type": "event",
        "event_id": event_id or uuid4().hex,
        "runtime_id": host["runtime_id"],
        "seq": 1,
        "method": method,
        "params": {"threadId": thread["native_id"], **params},
    }
    if rpc_id is not None:
        frame["rpc_id"] = rpc_id
    handle_node_message(app, host["host"]["id"], frame)
    return frame


def settle(app, host, command, *, result=None, error=None, unknown=False):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": command["id"],
            **({"error": error} if error is not None else {"result": result or {}}),
            **({"unknown": True} if unknown else {}),
        },
    )


def begin(app, api, host, thread):
    response = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "开始工作", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 202, response.get_json()
    command = response.get_json()["command"]
    settle(app, host, command, result={"turn": {"id": "async-turn"}})
    emit(app, host, thread, "turn/started", {"turn": {"id": "async-turn"}})
    return command


def finish(app, host, thread):
    emit(
        app,
        host,
        thread,
        "turn/completed",
        {"turn": {"id": "async-turn", "status": "completed", "items": []}},
    )


def native_item(identifier="async-item"):
    return {
        "id": identifier,
        "type": "agentMessage",
        "text": "我有两个问题，可以继续工作时再回答。",
        "questions": [
            {"title": "选择哪一种方案？", "options": ["保留现状", "改进交互"]},
            {"title": "还有什么要求？", "options": []},
        ],
        "futureNativeField": {"preserve": True},
    }


def question_id(item_id, index):
    return json.dumps(
        ["request_user_input_async", item_id, index], ensure_ascii=False, separators=(",", ":")
    )


def reply(item_id="async-item"):
    return {
        "answers": {
            question_id(item_id, 0): {"answers": ["改进交互"]},
            question_id(item_id, 1): {"answers": ["保留中文\n和换行"]},
        }
    }


def envelope(item_id="async-item"):
    values = [
        {
            "questionItemId": question_id(item_id, 0),
            "question": "选择哪一种方案？",
            "answer": "改进交互",
        },
        {
            "questionItemId": question_id(item_id, 1),
            "question": "还有什么要求？",
            "answer": "保留中文\n和换行",
        },
    ]
    return (
        "<send_user_message_question_reply>\n"
        + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        + "\n</send_user_message_question_reply>"
    )


def ask(app, host, thread, identifier="async-item", *, method="item/completed"):
    return emit(
        app, host, thread, method, {"turnId": "async-turn", "item": native_item(identifier)}
    )


def reminders(api):
    response = api("GET", "/api/interactions")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def full(api, interaction_id):
    response = api("GET", f"/api/interactions/{interaction_id}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["interaction"]


def one_question(api):
    values = [
        row
        for row in reminders(api)["interactions"]
        if row["method"] == "anywhere/message_questions"
    ]
    assert len(values) == 1, values
    return values[0]


def answer(api, interaction_id, response=None):
    return api(
        "POST",
        f"/api/interactions/{interaction_id}/answer",
        json={"response": reply() if response is None else response},
    )


def wires(host, kind=None):
    return [
        frame["command"]
        for raw in host["socket"].messages
        if (frame := json.loads(raw)).get("type") == "command"
        and (kind is None or frame["command"]["kind"] == kind)
    ]


def thread_state(api, thread):
    return api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]


def test_async_question_survives_turn_end_and_runtime_change_without_blocking_work(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    pending = one_question(api)
    assert pending["blocking"] is False and pending["question_count"] == 2
    assert thread_state(api, thread)["status"] == "active"
    native = full(api, pending["id"])
    assert native["params"]["isBlocking"] is False
    assert native["params"]["sourceItemId"] == "async-item"
    assert [row["id"] for row in native["params"]["questions"]] == [
        question_id("async-item", 0),
        question_id("async-item", 1),
    ]
    finish(app, host, thread)
    assert one_question(api)["id"] == pending["id"]
    assert thread_state(api, thread)["status"] == "idle"
    handle_node_message(
        app, host["host"]["id"], {"type": "hello", "runtime_id": uuid4().hex, "version": "fake"}
    )
    assert one_question(api)["id"] == pending["id"]
    assert full(api, pending["id"])["status"] == "pending"


def test_async_item_start_completion_replay_and_backfill_create_only_one_request(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    frame = ask(app, host, thread, method="item/started")
    interaction_id = one_question(api)["id"]
    handle_node_message(app, host["host"]["id"], frame)
    ask(app, host, thread)
    assert one_question(api)["id"] == interaction_id
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    command = accepted.get_json()["command"]
    settle(app, host, command)
    assert full(api, interaction_id)["status"] == "resolved"
    ask(app, host, thread)
    assert reminders(api)["total"] == 0
    with app.app_context():
        assert (
            Interaction.query.filter_by(
                thread_id=thread["id"], method="anywhere/message_questions"
            ).count()
            == 1
        )
        saved = Item.query.filter_by(thread_id=thread["id"], native_id="async-item").one()
        assert saved.data["questions"] == native_item()["questions"]
        assert saved.data["futureNativeField"] == {"preserve": True}


def test_global_reminders_backfill_old_cached_agent_questions_without_native_rpc(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    with app.app_context():
        db.session.add(
            Item(
                thread_id=thread["id"],
                native_id="old-item",
                turn_id="finished-turn",
                type="agentMessage",
                data=native_item("old-item"),
                position=1,
            )
        )
        db.session.commit()
    before = len(wires(host))
    pending = one_question(api)
    assert pending["thread_id"] == thread["id"]
    assert full(api, pending["id"])["params"]["sourceItemId"] == "old-item"
    assert one_question(api)["id"] == pending["id"]
    assert len(wires(host)) == before


@pytest.mark.parametrize("active", [True, False])
def test_answer_uses_ordinary_input_with_exact_native_envelope_and_journals_before_delivery(
    app, api, paired_host, create_thread, active
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    if not active:
        finish(app, host, thread)
    sent = []
    original_send = host["socket"].send

    def verify_journal(raw):
        frame = json.loads(raw)
        if (
            frame.get("type") == "command"
            and frame["command"]["payload"].get("_async_question_id") == interaction_id
        ):
            with app.app_context():
                command = db.session.get(Command, frame["command"]["id"])
                assert command is not None
                assert db.session.get(Interaction, interaction_id).status == "answering"
                assert command.payload["_async_question_id"] == interaction_id
            sent.append(frame["command"])
        original_send(raw)

    host["socket"].send = verify_journal
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    assert len(sent) == 1
    command = sent[0]
    assert command["kind"] == ("turn.steer" if active else "turn.start")
    assert command["payload"]["input"] == [{"type": "text", "text": envelope()}]
    assert command["payload"]["native_thread_id"] == thread["native_id"]
    if active:
        assert command["payload"]["expected_turn_id"] == "async-turn"
    assert wires(host, "interaction.answer") == []
    assert full(api, interaction_id)["status"] == "answering"
    assert answer(api, interaction_id).status_code == 409
    settle(app, host, command, result={"turn": {"id": "reply-turn"}})
    assert full(api, interaction_id)["status"] == "resolved"
    assert answer(api, interaction_id).status_code == 409
    assert len(sent) == 1


def test_definitive_steer_failure_returns_pending_without_automatic_start_fallback(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    first = accepted.get_json()["command"]
    finish(app, host, thread)
    settle(app, host, first, error="Turn is no longer active")
    assert full(api, interaction_id)["status"] == "pending"
    assert one_question(api)["id"] == interaction_id
    assert len(wires(host, "turn.start")) == 1
    assert len(wires(host, "turn.steer")) == 1
    handle_node_message(app, host["host"]["id"], {"type": "ping"})
    assert len(wires(host, "turn.start")) == 1
    retry = answer(api, interaction_id)
    assert retry.status_code == 202, retry.get_json()
    assert retry.get_json()["command"]["kind"] == "turn.start"
    assert retry.get_json()["command"]["id"] != first["id"]


def test_unknown_answer_is_visible_as_uncertain_and_never_automatically_retried(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    command = accepted.get_json()["command"]
    settle(app, host, command, error="Delivery outcome unknown", unknown=True)
    assert full(api, interaction_id)["status"] == "answer_unknown"
    assert answer(api, interaction_id).status_code == 409
    before = len(wires(host))
    finish(app, host, thread)
    ask(app, host, thread)
    handle_node_message(app, host["host"]["id"], {"type": "ping"})
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "fake"},
    )
    api("GET", "/api/interactions")
    assert full(api, interaction_id)["status"] == "answer_unknown"
    assert len(wires(host)) == before


def test_runtime_restart_marks_inflight_async_reply_unknown_and_late_confirmation_can_settle_it(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    command = accepted.get_json()["command"]
    before = len(wires(host))
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": uuid4().hex, "version": "fake"},
    )
    assert api("GET", f"/api/commands/{command['id']}").get_json()["command"]["status"] == "unknown"
    assert full(api, interaction_id)["status"] == "answer_unknown"
    assert one_question(api)["id"] == interaction_id
    assert answer(api, interaction_id).status_code == 409
    assert len(wires(host)) == before
    # This is confirmation of the previous command, never a re-delivery.
    settle(app, host, command, result={"accepted": True})
    assert full(api, interaction_id)["status"] == "resolved"
    assert thread_state(api, thread)["status"] == "interrupted"
    assert len(wires(host)) == before


def test_unanswered_async_question_does_not_block_the_follow_up_fifo(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    queued = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "随后检查", "client_request_id": uuid4().hex},
    ).get_json()["command"]
    assert queued["kind"] == "turn.queue"
    finish(app, host, thread)
    api("GET", f"/api/threads/{thread['id']}/queue")
    assert [row["id"] for row in wires(host, "turn.start")][-1] == queued["id"]
    assert one_question(api)["id"] == interaction_id
    assert full(api, interaction_id)["status"] == "pending"


def test_answering_async_question_preserves_a_separate_native_approval(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    async_id = one_question(api)["id"]
    emit(
        app,
        host,
        thread,
        "item/commandExecution/requestApproval",
        {"turnId": "async-turn", "availableDecisions": ["accept", "decline"]},
        rpc_id=0,
    )
    approval_id = next(
        row["id"]
        for row in reminders(api)["interactions"]
        if row["method"].endswith("requestApproval")
    )
    accepted = answer(api, async_id)
    assert accepted.status_code == 202, accepted.get_json()
    assert accepted.get_json()["command"]["kind"] == "turn.steer"
    settle(app, host, accepted.get_json()["command"])
    assert full(api, async_id)["status"] == "resolved"
    assert full(api, approval_id)["status"] == "pending"
    assert thread_state(api, thread)["status"] == "waiting"
    assert wires(host, "interaction.answer") == []


@pytest.mark.parametrize("active", [True, False])
def test_unknown_prior_control_blocks_new_turn_but_a_known_active_turn_can_be_steered(
    app, api, paired_host, create_thread, active
):
    host = paired_host()
    thread = create_thread(host)
    first = begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    if not active:
        finish(app, host, thread)
    with app.app_context():
        command = db.session.get(Command, first["id"])
        command.status = "unknown"
        command.error = "Uncertain prior operation"
        db.session.commit()
    before = len(wires(host))
    response = answer(api, interaction_id)
    if active:
        assert response.status_code == 202, response.get_json()
        assert response.get_json()["command"]["kind"] == "turn.steer"
        assert full(api, interaction_id)["status"] == "answering"
        assert wires(host, "turn.steer")[-1]["payload"]["expected_turn_id"] == "async-turn"
        assert len(wires(host)) == before + 1
    else:
        assert response.status_code == 409, response.get_json()
        assert full(api, interaction_id)["status"] == "pending"
        assert len(wires(host)) == before


def test_explicitly_acknowledged_unknown_control_does_not_block_an_idle_async_reply(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    control = api(
        "POST",
        f"/api/threads/{thread['id']}/steer",
        json={"text": "补充要求", "client_request_id": uuid4().hex},
    ).get_json()["command"]
    queued = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "完成之后继续", "client_request_id": uuid4().hex},
    ).get_json()["command"]
    assert queued["kind"] == "turn.queue"
    settle(app, host, control, error="Steer outcome unknown", unknown=True)
    finish(app, host, thread)
    resumed = api("POST", f"/api/threads/{thread['id']}/queue/resume", json={})
    assert resumed.status_code == 202, resumed.get_json()
    resumed_command = resumed.get_json()["command"]
    assert resumed_command["id"] == queued["id"]
    settle(app, host, resumed_command, result={"turn": {"id": "continued-turn"}})
    emit(app, host, thread, "turn/started", {"turn": {"id": "continued-turn"}})
    emit(
        app,
        host,
        thread,
        "turn/completed",
        {"turn": {"id": "continued-turn", "status": "completed"}},
    )
    with app.app_context():
        acknowledged = db.session.get(Command, control["id"])
        assert acknowledged.status == "unknown"
        assert acknowledged.payload["_queue_resume_ack"] > 1
    assert thread_state(api, thread)["status"] == "idle"
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    assert accepted.get_json()["command"]["kind"] == "turn.start"


def test_async_reply_targets_the_correct_machine_even_for_identical_native_item_ids(
    app, api, paired_host, create_thread
):
    first_host = paired_host("first-machine")
    second_host = paired_host("second-machine")
    first = create_thread(first_host)
    second = create_thread(second_host)
    for host, thread in ((first_host, first), (second_host, second)):
        begin(app, api, host, thread)
        ask(app, host, thread)
    rows = reminders(api)["interactions"]
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
    selected = next(row for row in rows if row["thread_id"] == second["id"])
    before_first = len(wires(first_host))
    response = answer(api, selected["id"])
    assert response.status_code == 202, response.get_json()
    assert len(wires(first_host)) == before_first
    delivered = wires(second_host, "turn.steer")[-1]
    assert delivered["thread_id"] == second["id"]
    assert delivered["payload"]["native_thread_id"] == second["native_id"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"answers": {}},
        {"answers": {"unknown-id": {"answers": ["choice"]}}},
        {
            "answers": {
                question_id("async-item", 0): {"answers": [" "]},
                question_id("async-item", 1): {"answers": ["text"]},
            }
        },
    ],
)
def test_invalid_async_answers_remain_pending_and_never_send_native_input(
    app, api, paired_host, create_thread, invalid
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    before = len(wires(host))
    response = answer(api, interaction_id, copy.deepcopy(invalid))
    assert response.status_code == 400, response.get_json()
    assert full(api, interaction_id)["status"] == "pending"
    assert len(wires(host)) == before


FAKE_INPUT_PEER = r"""
import json, sys
calls = []
def emit(value):
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
    message = json.loads(line)
    method, rpc_id = message.get('method'), message.get('id')
    if method == 'initialize':
        emit({'id': rpc_id, 'result': {'userAgent': 'fake-async-question-peer'}})
    elif method == 'test/inspect':
        emit({'id': rpc_id, 'result': {'calls': calls}})
    elif method == 'turn/steer':
        calls.append(message)
        emit({'id': rpc_id, 'result': {'accepted': True}})
    elif rpc_id is not None:
        emit({'id': rpc_id, 'error': {'code': -32601, 'message': 'Unexpected method'}})
"""


@pytest.mark.parametrize("kind", ["userMessage", "steeringUserMessage"])
def test_native_client_reply_resolves_reminder_and_displays_readable_answer(
    app, api, paired_host, create_thread, kind
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    content = [{"type": "text", "text": envelope()}]
    data = {"id": "desktop-answer", "type": kind}
    if kind == "userMessage":
        data["content"] = content
    else:
        data.update(input=content, status="accepted")
    original = copy.deepcopy(data)
    emit(app, host, thread, "item/completed", {"turnId": "async-turn", "item": data})
    assert data == original
    assert full(api, interaction_id)["status"] == "resolved"
    assert api("GET", "/api/interactions").get_json()["total"] == 0
    items = api("GET", f"/api/threads/{thread['id']}").get_json()["items"]
    answer_item = next(item for item in items if item["native_id"] == "desktop-answer")
    assert answer_item["data"]["_displayText"]
    assert "<send_user_message_question_reply>" not in answer_item["data"]["_displayText"]
    assert answer_item["data"].get("content", answer_item["data"].get("input")) == content
    assert not wires(host, "interaction.answer")


def test_async_reply_reaches_a_fake_native_peer_as_user_input_and_not_an_rpc_answer(
    app, api, paired_host, create_thread, tmp_path
):
    host = paired_host()
    thread = create_thread(host)
    begin(app, api, host, thread)
    ask(app, host, thread)
    interaction_id = one_question(api)["id"]
    peer = tmp_path / "async_input_peer.py"
    peer.write_text(FAKE_INPUT_PEER, encoding="utf-8")
    accepted = answer(api, interaction_id)
    assert accepted.status_code == 202, accepted.get_json()
    command = wires(host, "turn.steer")[-1]

    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "fake-token"}, tmp_path / "node"
        )
        service.store.save_thread(thread["native_id"], host["root"])
        runtime = CodexRuntime([sys.executable, "-u", str(peer)], service._native_event, timeout=3)
        runtime.runtime_id = host["runtime_id"]
        service.runtime = runtime
        await runtime.start()
        try:
            result = await service.handle_command(command)
            assert "error" not in result, result
            handle_node_message(app, host["host"]["id"], result)
            calls = (await runtime.request("test/inspect", {}))["calls"]
            assert len(calls) == 1 and calls[0]["method"] == "turn/steer"
            assert calls[0]["params"] == {
                "threadId": thread["native_id"],
                "expectedTurnId": "async-turn",
                "input": [{"type": "text", "text": envelope()}],
            }
            assert full(api, interaction_id)["status"] == "resolved"
            assert runtime.server_requests == {}
            await service.handle_command(command)
            assert (await runtime.request("test/inspect", {}))["calls"] == calls
        finally:
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())
