"""Real HTTP/WebSocket/node/subprocess checks with deterministic Codex JSON-RPC."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from websockets.sync.client import connect
from werkzeug.serving import make_server

from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import db
from codex_anywhere.node.config import load_config
from codex_anywhere.node.runtime import CodexRuntime
from codex_anywhere.node.service import VERSION, NodeService, pair_node


def eventually(predicate, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}")


@pytest.fixture
def live_system(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    Image.new("RGB", (3, 3), color="blue").save(root / "output.png")
    audit_path = tmp_path / "native-input.jsonl"
    peer_script = Path(__file__).parent / "fixtures" / "codex_peer.py"
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "e2e-secret",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "e2e-password",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'hub.sqlite3'}",
            "ASSET_DIR": str(tmp_path / "assets"),
            "SESSION_COOKIE_SECURE": False,
            "PUBLIC_URL": "",
        }
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}"
    hub_thread = threading.Thread(target=server.serve_forever, daemon=True)
    hub_thread.start()
    client = httpx.Client(base_url=url, trust_env=False, timeout=10)
    node_state = tmp_path / "node"
    node_thread = None
    holder = {}
    errors = []

    def post(path, **kwargs):
        token = client.get("/api/auth/me").json()["csrf_token"]
        return client.post(path, headers={"X-CSRF-Token": token}, **kwargs)

    class PeerNode(NodeService):
        async def _start_runtime(self):
            runtime = CodexRuntime(
                [sys.executable, str(peer_script), "--root", str(root), "--audit", str(audit_path)],
                self._native_event,
                self._runtime_exit,
                timeout=10,
            )
            self.runtime = runtime
            await runtime.start()
            self.store.record_event(
                runtime.runtime_id,
                "anywhere/runtime_info",
                {"clientVersion": VERSION, "userAgent": runtime.version.get("userAgent")},
            )
            self.event_wake.set()

    def run_node_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        service = PeerNode(load_config(node_state), node_state)
        task = loop.create_task(service.run())
        holder.update(loop=loop, task=task, service=service)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    try:
        login = post("/api/auth/login", json={"username": "admin", "password": "e2e-password"})
        assert login.status_code == 200, login.text
        code = post("/api/pairings", json={}).json()["code"]
        host = pair_node(url, "test-node", [str(root)], node_state, code)
        node_thread = threading.Thread(target=run_node_thread, daemon=True)
        node_thread.start()
        eventually(
            lambda: any(h["online"] for h in client.get("/api/hosts").json()["hosts"]),
            "node WebSocket handshake",
        )
        yield {
            "client": client,
            "post": post,
            "host": host,
            "root": root,
            "url": url,
            "audit": audit_path,
            "node": holder,
        }
    finally:
        if holder and not holder["loop"].is_closed():
            holder["loop"].call_soon_threadsafe(holder["task"].cancel)
        if node_thread:
            node_thread.join(timeout=15)
        client.close()
        server.shutdown()
        server.server_close()
        hub_thread.join(timeout=5)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        assert not (node_thread and node_thread.is_alive()), "node thread did not stop"
        assert not errors, errors


def test_remote_chat_images_approval_references_and_browser_replay(live_system):
    system = live_system
    client, post = system["client"], system["post"]

    def make_thread(title):
        created = post(
            "/api/threads",
            json={"host_id": system["host"]["id"], "cwd": str(system["root"]), "title": title},
        )
        assert created.status_code == 202, created.text
        thread_id = created.json()["thread"]["id"]
        command_id = created.json()["command"]["id"]
        eventually(
            lambda: (
                client.get(f"/api/commands/{command_id}").json()["command"]["status"] == "completed"
            ),
            "native thread/start response",
        )
        return thread_id

    thread_id = make_thread("Source task")
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    ws_url = system["url"].replace("http://", "ws://") + "/ws/client"
    with connect(
        ws_url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as browser:
        assert json.loads(browser.recv(timeout=5)) == {"type": "ready"}
        browser.send(json.dumps({"type": "subscribe", "thread_id": thread_id, "after": 0}))
        upload = post(
            "/api/assets",
            files={
                "file": ("input.png", (system["root"] / "output.png").read_bytes(), "image/png")
            },
        )
        assert upload.status_code == 200
        sent = post(
            f"/api/threads/{thread_id}/messages",
            json={
                "text": "Inspect the image",
                "asset_ids": [upload.json()["asset"]["id"]],
                "client_request_id": uuid4().hex,
            },
        )
        assert sent.status_code == 202, sent.text
        interaction = eventually(
            lambda: next(
                iter(client.get(f"/api/threads/{thread_id}").json()["interactions"]), None
            ),
            "native approval request",
        )
        answer = post(
            f"/api/interactions/{interaction['id']}/answer",
            json={"response": {"decision": "accept"}},
        )
        assert answer.status_code == 202, answer.text

        def completed_with_asset():
            history = client.get(f"/api/threads/{thread_id}").json()
            images = [item for item in history["items"] if item["type"] == "imageView"]
            if (
                history["thread"]["status"] == "idle"
                and images
                and images[0]["data"].get("_assets")
            ):
                return history
            return None

        history = eventually(completed_with_asset, "completed turn and output image upload")
        image = next(item for item in history["items"] if item["type"] == "imageView")
        asset = image["data"]["_assets"][0]
        downloaded = client.get(asset["url"])
        assert downloaded.status_code == 200
        assert downloaded.content == (system["root"] / "output.png").read_bytes()
        with httpx.Client(base_url=system["url"], trust_env=False) as anonymous:
            assert anonymous.get(asset["url"]).status_code == 401
        search = next(item for item in history["items"] if item["type"] == "webSearch")
        assert search["data"]["action"]["url"] == "https://example.com/codex"
        streamed = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            event = json.loads(browser.recv(timeout=5))
            streamed.append(event)
            if event.get("method") == "turn/completed":
                break
        deltas = [
            event["params"]["delta"]
            for event in streamed
            if event.get("method") == "item/agentMessage/delta"
        ]
        assert "".join(deltas) == "Connected from fake Codex."
        last_cursor = max(event["id"] for event in streamed)

    target = make_thread("Target task")
    source_item = next(item for item in history["items"] if item["type"] == "agentMessage")
    referenced = post(
        f"/api/threads/{target}/messages",
        json={
            "text": "Reuse this answer",
            "client_request_id": uuid4().hex,
            "references": [
                {"kind": "task", "thread_id": thread_id, "item_id": source_item["id"]},
                {
                    "kind": "web",
                    "url": "https://example.com/reference",
                    "title": "Web source",
                    "excerpt": "Selected web context",
                },
            ],
        },
    )
    assert referenced.status_code == 202, referenced.text
    target_history = eventually(
        lambda: (lambda result: result if result["thread"]["status"] == "idle" else None)(
            client.get(f"/api/threads/{target}").json()
        ),
        "referenced second turn",
    )
    target_user = next(item for item in target_history["items"] if item["type"] == "userMessage")
    refs = target_user["data"]["_references"]
    assert len(refs) == 2 and all(reference["snapshot_id"] for reference in refs)
    assert refs[0]["url"].startswith(f"/tasks/{thread_id}?item=")
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    approval = next(message for message in audit if message.get("id") == 101)
    assert isinstance(approval["id"], int) and approval["result"] == {"decision": "accept"}
    native_turns = [message for message in audit if message.get("method") == "turn/start"]
    assert any(
        value["type"] == "image" and value["url"].startswith("data:image/png;base64,")
        for value in native_turns[0]["params"]["input"]
    )
    quoted_input = native_turns[1]["params"]["input"][0]["text"]
    assert "Connected from fake Codex." in quoted_input
    assert "Selected web context" in quoted_input

    with connect(
        ws_url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as replay:
        assert json.loads(replay.recv(timeout=5))["type"] == "ready"
        replay.send(json.dumps({"type": "subscribe", "after": last_cursor}))
        event = json.loads(replay.recv(timeout=5))
        assert event["id"] > last_cursor
        assert event["type"] == "event"
