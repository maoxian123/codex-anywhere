"""Exercise large command delivery over the real Flask and node WebSockets."""

import asyncio
import base64
import io
import json
import random
import time
from uuid import uuid4

import pytest
from PIL import Image
from test_e2e import eventually
from test_e2e import live_system as live_system
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from codex_anywhere.hub.models import Event, db


def test_large_image_command_reaches_native_peer_without_heartbeat_reconnect(live_system):
    system = live_system
    client, post = system["client"], system["post"]
    created = post(
        "/api/threads",
        json={"host_id": system["host"]["id"], "cwd": str(system["root"]), "title": "Large image"},
    ).json()
    thread_id, command_id = created["thread"]["id"], created["command"]["id"]
    eventually(
        lambda: (
            client.get(f"/api/commands/{command_id}").json()["command"]["status"] == "completed"
        ),
        "thread creation before image command",
    )
    output = io.BytesIO()
    # Incompressible pixels make the actual PNG exceed 1 MiB without a huge canvas.
    pixels = random.Random(42).randbytes(768 * 768 * 3)
    Image.frombytes("RGB", (768, 768), pixels).save(output, format="PNG")
    image = output.getvalue()
    assert len(image) > 1024 * 1024
    uploaded = post("/api/assets", files={"file": ("large.png", image, "image/png")})
    assert uploaded.status_code == 200
    runtime_id = system["node"]["service"].runtime.runtime_id
    connection = system["node"]["service"].ws
    started = time.monotonic()
    accepted = post(
        f"/api/threads/{thread_id}/messages",
        json={
            "text": "Receive the complete image",
            "client_request_id": uuid4().hex,
            "asset_ids": [uploaded.json()["asset"]["id"]],
        },
    )
    assert accepted.status_code == 202, accepted.text

    def received_image():
        for line in system["audit"].read_text(encoding="utf-8").splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # The child may currently be flushing its final audit line.
            if message.get("method") == "turn/start":
                return next(
                    (
                        value["url"]
                        for value in message["params"]["input"]
                        if value["type"] == "image"
                    ),
                    None,
                )
        return None

    received = eventually(received_image, "large image over the active node WebSocket", timeout=5)
    assert time.monotonic() - started < 5
    assert base64.b64decode(received.split(",", 1)[1]) == image
    assert system["node"]["service"].runtime.runtime_id == runtime_id
    assert system["node"]["service"].ws is connection


def test_logout_closes_the_existing_browser_websocket(live_system):
    system = live_system
    cookie = "; ".join(f"{name}={value}" for name, value in system["client"].cookies.items())
    url = system["url"].replace("http://", "ws://") + "/ws/client"
    with connect(
        url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as ws:
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        assert system["post"]("/api/auth/logout", json={}).status_code == 200
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=2)
        assert closed.value.rcvd.code == 1008
    with pytest.raises(InvalidStatus) as denied:
        with connect(url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None):
            pytest.fail("Logged-out cookie unexpectedly opened a WebSocket")
    assert denied.value.response.status_code == 401


def test_administrator_save_closes_old_socket_and_current_browser_reconnects(live_system):
    system = live_system
    client = system["client"]
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    url = system["url"].replace("http://", "ws://") + "/ws/client"
    with connect(
        url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as ws:
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        csrf = client.get("/api/auth/me").json()["csrf_token"]
        saved = client.patch(
            "/api/settings/administrator",
            headers={"X-CSRF-Token": csrf},
            json={"username": "new-admin", "current_password": "e2e-password"},
        )
        assert saved.status_code == 200
        assert saved.json()["session"]["authenticated"] is True
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=2)
        assert closed.value.rcvd.code == 1008
    with pytest.raises(InvalidStatus) as denied:
        with connect(url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None):
            pytest.fail("Old administrator cookie unexpectedly opened a WebSocket")
    assert denied.value.response.status_code == 401
    renewed = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    with connect(
        url, origin=system["url"], additional_headers={"Cookie": renewed}, proxy=None
    ) as ws:
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
    assert client.get("/api/hosts").json()["hosts"][0]["online"] is True


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "subscribe", "thread_id": {"invalid": "id"}, "after": 0},
        {"type": "subscribe", "after": 2**100},
        {"type": "subscribe", "after": True},
    ],
)
def test_invalid_subscription_closes_cleanly_without_sql_error(live_system, frame, caplog):
    system = live_system
    cookie = "; ".join(f"{name}={value}" for name, value in system["client"].cookies.items())
    url = system["url"].replace("http://", "ws://") + "/ws/client"
    with connect(
        url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as ws:
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(json.dumps(frame))
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=2)
        assert closed.value.rcvd.code == 1000
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_ready_cursor_skips_backlog_for_live_questions_without_removing_native_journals(
    live_system,
):
    system = live_system
    client, post, app = system["client"], system["post"], system["app"]
    created = post(
        "/api/threads",
        json={
            "host_id": system["host"]["id"],
            "cwd": str(system["root"]),
            "title": "Current question",
        },
    ).json()
    thread_id = created["thread"]["id"]
    eventually(
        lambda: (
            client.get(f"/api/commands/{created['command']['id']}").json()["command"]["status"]
            == "completed"
        ),
        "native thread before live cursor subscription",
    )
    native_id = client.get(f"/api/threads/{thread_id}").json()["thread"]["native_id"]
    service, loop = system["node"]["service"], system["node"]["loop"]
    asyncio.run_coroutine_threadsafe(
        service._native_event(
            "turn/started", {"threadId": native_id, "turn": {"id": "current-question-turn"}}, None
        ),
        loop,
    ).result(timeout=5)
    eventually(
        lambda: (
            client.get(f"/api/threads/{thread_id}").json()["thread"]["active_turn_id"]
            == "current-question-turn"
        ),
        "current native turn projection",
    )
    with app.app_context(), app.extensions["broker"].command_lock:
        db.session.add_all(
            [
                Event(
                    host_id=system["host"]["id"],
                    thread_id=thread_id,
                    event_key=f"historical-{uuid4().hex}",
                    method="test/old_history",
                    params={"index": index},
                )
                for index in range(1001)
            ]
        )
        db.session.commit()
        old_tip = db.session.query(db.func.max(Event.id)).scalar()

    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    url = system["url"].replace("http://", "ws://") + "/ws/client"
    raw = {
        "threadId": native_id,
        "turnId": "current-question-turn",
        "item": {
            "id": "fresh-native-question",
            "type": "agentMessage",
            "text": "Choose an option while work continues.",
            "questions": [{"title": "Which approach?", "options": ["Continue", "Revise"]}],
            "unknownNativeField": {"preserve": True},
        },
    }

    async def publish_question():
        await service._native_event("item/completed", raw, None)
        # This runs on the node loop before its outbox can drain the new row.
        return service.store.db.execute(
            "SELECT event_id FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]

    with connect(
        url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as browser:
        ready = json.loads(browser.recv(timeout=5))
        assert ready["type"] == "ready" and type(ready["event_cursor"]) is int
        cursor = ready["event_cursor"]
        assert cursor >= old_tip
        browser.send(json.dumps({"type": "subscribe", "after": cursor}))
        event_key = asyncio.run_coroutine_threadsafe(publish_question(), loop).result(timeout=5)
        received = []
        while True:
            event = json.loads(browser.recv(timeout=5))
            assert event["type"] == "event" and event["id"] > cursor
            assert event["method"] != "test/old_history"
            received.append(event)
            if event["method"] == "anywhere/message_questions":
                assert event["params"].get("interaction_id")
                break
            assert len(received) < 10, "Live question refresh is buried behind unrelated replay"
        assert any(
            event["method"] == "item/completed"
            and event["params"].get("item", {}).get("id") == "fresh-native-question"
            for event in received
        )
    pending = client.get("/api/interactions").json()
    assert pending["total"] == 1
    assert pending["interactions"][0]["thread_id"] == thread_id
    with app.app_context():
        native_event = Event.query.filter_by(event_key=f"{system['host']['id']}:{event_key}").one()
        assert native_event.params == raw
        assert Event.query.filter_by(method="test/old_history").count() == 1001

    async def acknowledged():
        row = service.store.db.execute(
            "SELECT acknowledged FROM events WHERE event_id=?", (event_key,)
        ).fetchone()
        return row is not None and row[0] == 1

    eventually(
        lambda: asyncio.run_coroutine_threadsafe(acknowledged(), loop).result(timeout=5),
        "committed native event ACK retained in the node journal",
    )
    # An explicit cursor still supports historical replay; the ready cursor is opt-in.
    with connect(
        url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as browser:
        assert json.loads(browser.recv(timeout=5))["type"] == "ready"
        browser.send(json.dumps({"type": "subscribe", "after": old_tip - 1}))
        replayed = json.loads(browser.recv(timeout=5))
        assert replayed["id"] == old_tip and replayed["method"] == "test/old_history"
