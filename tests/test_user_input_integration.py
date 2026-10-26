"""Native model questions round-trip through the node and browser APIs."""

import asyncio
import copy
import json
import sys
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Interaction, db
from codex_anywhere.hub.transport import handle_node_message
from codex_anywhere.node.runtime import CodexRuntime
from codex_anywhere.node.service import NodeService

FAKE_QUESTION_PEER = r"""
import json, sys
responses = []
requests = {}
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rpc_id = message.get("id")
    if method == "initialize":
        emit({"id": rpc_id, "result": {"userAgent": "fake-question-peer"}})
    elif method == "test/ask":
        request = message["params"]
        requests[json.dumps(request["id"])] = request["params"]
        emit(request)
        emit({"id": rpc_id, "result": {"asked": True}})
    elif method == "test/inspect":
        emit({"id": rpc_id, "result": {"responses": responses}})
    elif method is None and "id" in message:
        responses.append(message)
        params = requests.pop(json.dumps(rpc_id))
        emit({"method": "serverRequest/resolved", "params": {
            "threadId": params["threadId"], "requestId": rpc_id}})
    elif rpc_id is not None:
        emit({"id": rpc_id, "result": {}})
"""


def questions(thread, *, blocking=True):
    return {
        "threadId": thread["native_id"],
        "turnId": "question-turn",
        "itemId": "question-item",
        "isBlocking": blocking,
        "questions": [
            {
                "id": "approach",
                "header": "方案",
                "question": "请选择实现方案",
                "options": [
                    {"label": "保留现有方案", "description": "继续现有行为"},
                    {"label": "优化交互", "description": "调整交互流程"},
                ],
                "isOther": False,
            },
            {
                "id": "notes",
                "header": "补充",
                "question": "还有什么要求？",
                "options": None,
            },
            {
                "id": "custom",
                "header": "部署",
                "question": "选择部署时间或输入其他时间",
                "options": [{"label": "现在", "description": "立即部署"}],
                "isOther": True,
            },
        ],
    }


def answers():
    return {
        "answers": {
            "approach": {"answers": ["优化交互"]},
            "notes": {"answers": ["保留键盘操作\n中文内容不应丢失"]},
            "custom": {"answers": ["明天 18:00"]},
        }
    }


def event(app, host, method, params, *, rpc_id=None, event_id=None, runtime_id=None):
    frame = {
        "type": "event",
        "event_id": event_id or uuid4().hex,
        "runtime_id": runtime_id or host["runtime_id"],
        "seq": 1,
        "method": method,
        "params": params,
    }
    if rpc_id is not None:
        frame["rpc_id"] = rpc_id
    handle_node_message(app, host["host"]["id"], frame)
    return frame


def start(app, api, host, thread):
    response = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "开始任务", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 202, response.get_json()
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": response.get_json()["command"]["id"],
            "result": {"turn": {"id": "question-turn"}},
        },
    )
    event(
        app,
        host,
        "turn/started",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn"}},
    )


def detail(api, thread):
    response = api("GET", f"/api/threads/{thread['id']}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def attention(api, **query):
    response = api("GET", "/api/interactions", query_string=query)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def wires(host, kind):
    return [
        frame["command"]
        for raw in host["socket"].messages
        if (frame := json.loads(raw)).get("type") == "command" and frame["command"]["kind"] == kind
    ]


@pytest.mark.parametrize("rpc_id", [0, 73, "original-question-id"])
@pytest.mark.parametrize("blocking", [True, False])
def test_native_question_round_trip_preserves_rpc_id_and_exact_answers(
    app, api, paired_host, create_thread, tmp_path, rpc_id, blocking
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    peer = tmp_path / "question_peer.py"
    peer.write_text(FAKE_QUESTION_PEER, encoding="utf-8")
    state = tmp_path / "question-node"
    state.mkdir()

    async def scenario():
        service = NodeService({"server": "http://127.0.0.1:9876", "token": "fake-token"}, state)

        async def relay(method, params, request_id):
            await service._native_event(method, params, request_id)
            for frame in service.store.pending_events():
                handle_node_message(app, host["host"]["id"], frame)
                service.store.acknowledge(frame["event_id"])

        runtime = CodexRuntime([sys.executable, "-u", str(peer)], relay, timeout=3)
        runtime.runtime_id = host["runtime_id"]
        service.runtime = runtime
        await runtime.start()
        try:
            params = questions(thread, blocking=blocking)
            await runtime.request(
                "test/ask",
                {"id": rpc_id, "method": "item/tool/requestUserInput", "params": params},
            )
            interaction = detail(api, thread)["interactions"][0]
            assert interaction["params"] == params
            assert interaction["status"] == "pending"
            assert detail(api, thread)["thread"]["status"] == ("waiting" if blocking else "active")
            with app.app_context():
                saved = db.session.get(Interaction, interaction["id"])
                assert saved.rpc_id == rpc_id and type(saved.rpc_id) is type(rpc_id)
            pending = attention(api)
            assert pending["total"] == 1
            assert pending["interactions"][0]["blocking"] is blocking
            assert pending["interactions"][0]["question_count"] == 3
            response = answers()
            accepted = api(
                "POST", f"/api/interactions/{interaction['id']}/answer", json={"response": response}
            )
            assert accepted.status_code == 202, accepted.get_json()
            assert attention(api)["total"] == 0
            command = wires(host, "interaction.answer")[-1]
            assert command["payload"]["rpc_id"] == rpc_id
            assert type(command["payload"]["rpc_id"]) is type(rpc_id)
            assert command["payload"]["response"] == response
            result = await service.handle_command(command)
            assert result["result"] == {"answered": True}
            handle_node_message(app, host["host"]["id"], result)
            native = await runtime.request("test/inspect", {})
            assert native["responses"] == [{"id": rpc_id, "result": response}]
            assert json.dumps(rpc_id) not in runtime.server_requests
            assert detail(api, thread)["interactions"][0]["status"] == "resolved"
            assert detail(api, thread)["thread"]["status"] == "active"
            assert attention(api)["total"] == 0
            # Re-delivery returns the journaled result instead of answering again.
            assert await service.handle_command(command) == result
            assert (await runtime.request("test/inspect", {})) == native
        finally:
            await runtime.close()
            service.store.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["item/tool/requestUserInput", "tool/requestUserInput"])
@pytest.mark.parametrize("resolution", ["native", "answer"])
def test_resolving_question_keeps_another_approval_waiting_and_follow_up_queued(
    app, api, paired_host, create_thread, method, resolution
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    event(app, host, method, questions(thread), rpc_id="question")
    event(
        app,
        host,
        "item/commandExecution/requestApproval",
        {
            "threadId": thread["native_id"],
            "turnId": "question-turn",
            "availableDecisions": ["accept"],
        },
        rpc_id=11,
    )
    accepted = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "随后运行检查", "client_request_id": uuid4().hex},
    )
    assert accepted.status_code == 202, accepted.get_json()
    queued = accepted.get_json()["command"]
    assert (queued["kind"], queued["status"]) == ("turn.queue", "pending")
    assert attention(api)["total"] == 2
    if resolution == "answer":
        question_id = next(
            item["id"] for item in attention(api)["interactions"] if item["method"] == method
        )
        response = api(
            "POST", f"/api/interactions/{question_id}/answer", json={"response": answers()}
        )
        assert response.status_code == 202, response.get_json()
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "command_result",
                "id": response.get_json()["command"]["id"],
                "result": {"answered": True},
            },
        )
    else:
        # Native resolution can identify the request without a thread ID.
        event(app, host, "serverRequest/resolved", {"requestId": "question"})
    assert attention(api)["total"] == 1
    assert detail(api, thread)["thread"]["status"] == "waiting"
    assert len(wires(host, "turn.start")) == 1
    queue = api("GET", f"/api/threads/{thread['id']}/queue").get_json()
    assert [message["id"] for message in queue["messages"]] == [queued["id"]]
    event(
        app,
        host,
        "turn/completed",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn", "status": "completed"}},
    )
    assert attention(api)["total"] == 0
    queue = api("GET", f"/api/threads/{thread['id']}/queue").get_json()
    assert queue["messages"] == []
    assert [command["id"] for command in wires(host, "turn.start")][-1] == queued["id"]


def test_global_reminders_are_paginated_metadata_without_question_or_secret_values(
    app, api, paired_host, create_thread
):
    host = paired_host()
    first = create_thread(host, title="First conversation")
    second = create_thread(host, title="Second conversation")
    start(app, api, host, first)
    start(app, api, host, second)
    for index, thread in enumerate((first, second)):
        params = questions(thread)
        params["questions"][1].update(question="private-secret-prompt", isSecret=True)
        event(app, host, "item/tool/requestUserInput", params, rpc_id=index)
    first_page = attention(api, limit=1)
    second_page = attention(api, limit=1, offset=first_page["next_offset"])
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["has_more"] is True and second_page["has_more"] is False
    assert second_page["next_offset"] is None
    items = first_page["interactions"] + second_page["interactions"]
    assert {item["thread_id"] for item in items} == {first["id"], second["id"]}
    for item in items:
        assert item["host_name"] == "workstation"
        assert item["thread_title"] in {"First conversation", "Second conversation"}
        assert "params" not in item and "rpc_id" not in item
        assert "private-secret-prompt" not in json.dumps(item)
        full = api("GET", f"/api/interactions/{item['id']}").get_json()["interaction"]
        assert full["params"]["questions"][1]["question"] == "private-secret-prompt"


def test_duplicate_outbox_replay_and_same_runtime_hello_do_not_duplicate_reminders(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    frame = event(app, host, "item/tool/requestUserInput", questions(thread), rpc_id=19)
    original = attention(api)["interactions"][0]["id"]
    handle_node_message(app, host["host"]["id"], frame)
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "fake"},
    )
    handle_node_message(app, host["host"]["id"], frame)
    assert attention(api)["total"] == 1
    assert attention(api)["interactions"][0]["id"] == original
    assert detail(api, thread)["thread"]["status"] == "waiting"


@pytest.mark.parametrize("resolution", ["serverRequest/resolved", "turn/completed", "runtime"])
def test_native_resolution_turn_end_and_runtime_restart_remove_reminders(
    app, api, paired_host, create_thread, resolution
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    event(app, host, "item/tool/requestUserInput", questions(thread), rpc_id="question")
    interaction_id = attention(api)["interactions"][0]["id"]
    if resolution == "runtime":
        handle_node_message(
            app,
            host["host"]["id"],
            {"type": "hello", "runtime_id": uuid4().hex, "version": "fake"},
        )
        # Old outbox data remains historical and must not revive attention.
        event(app, host, "item/tool/requestUserInput", questions(thread), rpc_id="old-question")
    else:
        params = {"threadId": thread["native_id"]}
        if resolution == "turn/completed":
            params["turn"] = {"id": "question-turn", "status": "completed"}
        else:
            params["requestId"] = "question"
        event(app, host, resolution, params)
    assert attention(api)["total"] == 0
    response = api(
        "POST", f"/api/interactions/{interaction_id}/answer", json={"response": answers()}
    )
    assert response.status_code == 409, response.get_json()
    assert wires(host, "interaction.answer") == []


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"answers": {}},
        {"answers": {"invented": {"answers": ["yes"]}}},
        {
            "answers": {
                "approach": {"answers": ["unsupported"]},
                "notes": {"answers": ["text"]},
                "custom": {"answers": ["now"]},
            }
        },
        {
            "answers": {
                "approach": {"answers": ["优化交互"]},
                "notes": {"answers": ["   "]},
                "custom": {"answers": ["now"]},
            }
        },
    ],
)
def test_invalid_or_incomplete_answers_do_not_consume_pending_question(
    app, api, paired_host, create_thread, invalid
):
    host = paired_host()
    thread = create_thread(host)
    start(app, api, host, thread)
    event(app, host, "item/tool/requestUserInput", questions(thread), rpc_id="question")
    interaction_id = attention(api)["interactions"][0]["id"]
    response = api(
        "POST",
        f"/api/interactions/{interaction_id}/answer",
        json={"response": copy.deepcopy(invalid)},
    )
    assert response.status_code == 400, response.get_json()
    assert attention(api)["total"] == 1
    assert detail(api, thread)["interactions"][0]["status"] == "pending"
    assert wires(host, "interaction.answer") == []
