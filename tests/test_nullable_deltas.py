"""Nullable native item snapshots must not poison durable event replay."""

import asyncio
import json
from uuid import uuid4

import pytest
from test_e2e import eventually
from test_e2e import live_system as live_system
from websockets.sync.client import connect

from codex_anywhere.hub.models import Event
from codex_anywhere.hub.transport import handle_node_message


def frame(host, thread, method, params):
    return {
        "type": "event",
        "event_id": uuid4().hex,
        "runtime_id": host["runtime_id"],
        "seq": 1,
        "method": method,
        "params": {"threadId": thread["native_id"], "turnId": "nullable-turn", **params},
    }


@pytest.mark.parametrize("initial", [None, False, 0, {"unknown": True}, []])
@pytest.mark.parametrize(
    "kind,method,field",
    [
        ("commandExecution", "item/commandExecution/outputDelta", "aggregatedOutput"),
        ("agentMessage", "item/agentMessage/delta", "text"),
    ],
)
def test_nullable_native_text_snapshot_accepts_delta_without_losing_unknown_fields(
    app,
    api,
    paired_host,
    create_thread,
    kind,
    method,
    field,
    initial,
):
    host = paired_host()
    thread = create_thread(host)
    item = {
        "id": "nullable-item",
        "type": kind,
        field: initial,
        "futureNativeField": {"nested": [None, {"keep": True}]},
    }
    started = frame(host, thread, "item/started", {"item": item})
    delta = frame(host, thread, method, {"itemId": item["id"], "delta": "first\n"})
    handle_node_message(app, host["host"]["id"], started)
    handle_node_message(app, host["host"]["id"], delta)
    for invalid in (None, False, {"future": "value"}, ["native extension"]):
        handle_node_message(
            app,
            host["host"]["id"],
            frame(
                host,
                thread,
                method,
                {
                    "itemId": item["id"],
                    "delta": invalid,
                },
            ),
        )
    history = api("GET", f"/api/threads/{thread['id']}").get_json()
    projected = next(
        value["data"] for value in history["items"] if value["native_id"] == item["id"]
    )
    assert projected[field] == "first\n"
    assert projected["futureNativeField"] == item["futureNativeField"]
    with app.app_context():
        assert (
            Event.query.filter_by(event_key=f"{host['host']['id']}:{started['event_id']}")
            .one()
            .params["item"]
            == item
        )


def test_nullable_command_output_is_acked_and_duplicate_replay_does_not_poison_connection(
    live_system,
):
    system = live_system
    client, post = system["client"], system["post"]
    created = post(
        "/api/threads",
        json={
            "host_id": system["host"]["id"],
            "cwd": str(system["root"]),
            "title": "Nullable native output",
        },
    )
    assert created.status_code == 202
    thread_id = created.json()["thread"]["id"]
    command_id = created.json()["command"]["id"]

    def started():
        command = client.get(f"/api/commands/{command_id}").json()["command"]
        assert command["status"] not in {"failed", "unknown"}, command
        return command["result"] if command["status"] == "completed" else None

    native_id = eventually(started, "fake native thread")["thread"]["id"]
    service, loop = system["node"]["service"], system["node"]["loop"]

    def in_node(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=5)

    async def inject(method, params):
        params = {"threadId": native_id, "turnId": "nullable-stream", **params}
        await service._native_event(method, params, None)
        return next(
            event
            for event in service.store.pending_events(limit=100)
            if event["method"] == method and event["params"] == params
        )

    async def acknowledged(event_id):
        row = service.store.db.execute(
            "SELECT acknowledged FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return row is not None and row[0] == 1

    def assert_acked(event):
        eventually(
            lambda: in_node(acknowledged(event["event_id"])), "durable native event ACK", timeout=5
        )

    def output():
        values = client.get(f"/api/threads/{thread_id}?item_limit=10").json()["items"]
        return next(item["data"] for item in values if item["native_id"] == "nullable-command")

    item = {
        "id": "nullable-command",
        "type": "commandExecution",
        "command": "synthetic only",
        "aggregatedOutput": None,
        "status": "inProgress",
        "exitCode": None,
        "futureNativeField": {"keep": [None, "opaque"]},
    }
    snapshot = in_node(inject("item/started", {"item": item}))
    assert_acked(snapshot)
    assert output()["aggregatedOutput"] is None
    delta = in_node(
        inject("item/commandExecution/outputDelta", {"itemId": item["id"], "delta": "first\n"})
    )
    assert_acked(delta)
    assert output()["aggregatedOutput"] == "first\n"
    assert output()["futureNativeField"] == item["futureNativeField"]
    epoch, connection = service.runtime.runtime_id, service.ws
    in_node(connection.close())
    eventually(
        lambda: (
            service.ws is not None
            and service.ws is not connection
            and any(host["online"] for host in client.get("/api/hosts").json()["hosts"])
        ),
        "same-epoch node reconnect",
        timeout=15,
    )
    assert service.runtime.runtime_id == epoch
    # A lost ACK may replay both the nullable snapshot and the earlier delta.
    # Their original IDs must be ACKed without applying the delta a second time.
    assert in_node(service._send(snapshot))
    assert in_node(service._send(delta))
    next_delta = in_node(
        inject("item/commandExecution/outputDelta", {"itemId": item["id"], "delta": "second\n"})
    )
    assert_acked(next_delta)
    assert output()["aggregatedOutput"] == "first\nsecond\n"
    completed = {
        **item,
        "aggregatedOutput": "first\nsecond\n",
        "status": "completed",
        "exitCode": 0,
    }
    final = in_node(inject("item/completed", {"item": completed}))
    assert_acked(final)
    assert output() == completed
    # Raw journal fields remain native, including the original null value.
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    with connect(
        system["url"].replace("http://", "ws://") + "/ws/client",
        origin=system["url"],
        additional_headers={"Cookie": cookie},
        proxy=None,
    ) as browser:
        assert json.loads(browser.recv(timeout=5))["type"] == "ready"
        browser.send(json.dumps({"type": "subscribe", "thread_id": thread_id, "after": 0}))
        replayed = []
        while True:
            event = json.loads(browser.recv(timeout=5))
            replayed.append(event)
            if event["method"] == "item/completed":
                break
        originals = [event for event in replayed if event["method"] == "item/started"]
        assert len(originals) == 1 and originals[0]["params"]["item"] == item
        deltas = [
            event for event in replayed if event["method"] == "item/commandExecution/outputDelta"
        ]
        assert [event["params"]["delta"] for event in deltas] == ["first\n", "second\n"]
