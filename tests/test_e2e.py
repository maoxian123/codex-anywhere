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
        host = pair_node(url, "test-node", node_state, code)
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


def test_projects_catalog_import_images_and_continue_original_thread(live_system):
    system = live_system
    client, post = system["client"], system["post"]
    host_id = system["host"]["id"]

    def command(response):
        assert response.status_code == 202, response.text
        command_id = response.json()["command"]["id"]

        def done():
            value = client.get(f"/api/commands/{command_id}").json()["command"]
            assert value["status"] not in {"failed", "unknown"}, value
            return value["result"] if value["status"] == "completed" else None

        return eventually(done, "project/history command")

    unrelated = system["root"].parent / "new-project"
    result = command(
        post(
            "/api/projects",
            json={
                "host_id": host_id,
                "cwd": str(unrelated),
                "name": "Another project",
                "create_directory": True,
            },
        )
    )
    assert unrelated.is_dir()
    project = result["project"]
    assert Path(project["cwd"]) == unrelated
    listing = command(
        post(f"/api/hosts/{host_id}/directories", json={"path": str(unrelated.parent)})
    )
    assert any(Path(entry["path"]) == unrelated for entry in listing["directories"])

    catalog = command(post(f"/api/hosts/{host_id}/catalog", json={}))
    native = catalog["data"][0]
    imported = post(f"/api/hosts/{host_id}/imports", json={"native_thread_id": native["id"]})
    thread_id = imported.json()["thread"]["id"]
    command(imported)

    def loaded():
        result = client.get(f"/api/threads/{thread_id}").json()
        return result if result["thread"]["history_loaded"] else None

    history = eventually(loaded, "durable imported items")
    assert [item["native_id"] for item in history["items"]] == [
        "old-user-1",
        "old-answer-1",
        "old-image",
        "old-search",
    ]
    assert [item["position"] for item in history["items"]] == [1, 2, 3, 4]
    assert history["thread"]["imported"] is True
    assert Path(history["thread"]["cwd"]) == system["root"].parent / "previous-project"
    assert history["thread"]["project_id"] != project["id"]
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    assert not any(message.get("method") in {"thread/resume", "turn/start"} for message in audit)
    assert len([m for m in audit if m.get("method") == "thread/turns/list"]) == 2

    def images():
        result = loaded()
        return (
            result
            if all(result["items"][index]["data"].get("_assets") for index in (0, 2))
            else None
        )

    history = eventually(images, "imported input and local output images")
    for index in (0, 2):
        assert client.get(history["items"][index]["data"]["_assets"][0]["url"]).status_code == 200
    repeated = post(f"/api/hosts/{host_id}/imports", json={"native_thread_id": native["id"]})
    assert repeated.json()["thread"]["id"] == thread_id
    command(repeated)
    eventually(loaded, "reimport complete")
    assert len(loaded()["items"]) == 4

    command(
        post(
            f"/api/threads/{thread_id}/messages",
            json={
                "text": "Continue the same conversation",
                "client_request_id": uuid4().hex,
            },
        )
    )
    eventually(
        lambda: client.get(f"/api/threads/{thread_id}").json()["thread"]["status"] == "idle",
        "continued original conversation",
    )
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    resume = next(message for message in audit if message.get("method") == "thread/resume")
    assert resume["params"]["threadId"] == "native-history"
    assert resume["params"]["excludeTurns"] is True
    assert (
        next(m for m in audit if m.get("method") == "turn/start")["params"]["threadId"]
        == "native-history"
    )


def test_connecting_discovers_local_projects_and_opening_automatically_loads_history(live_system):
    system = live_system
    client, post = system["client"], system["post"]

    def discovered():
        return next(
            (
                thread
                for thread in client.get("/api/threads").json()["threads"]
                if thread["native_id"] == "native-history"
            ),
            None,
        )

    thread = eventually(discovered, "automatic catalog after node welcome")
    assert thread["project_id"] and not thread["history_loaded"]
    history_url = f"/api/threads/{thread['id']}?item_limit=2"
    assert client.get(history_url).json()["items"] == []
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    assert not any(
        message.get("method") in {"thread/read", "thread/resume", "turn/start"} for message in audit
    )
    started = post(f"/api/threads/{thread['id']}/history", json={"limit": 2})
    assert started.status_code == 202
    first_command = started.json()["command"]
    assert first_command["kind"] == "thread.history"

    def loaded():
        command = client.get(f"/api/commands/{first_command['id']}").json()["command"]
        assert command["status"] not in {"failed", "unknown"}, command
        result = client.get(history_url).json()
        return result if result["thread"]["history_loaded"] else None

    latest = eventually(loaded, "lazy history projected after opening")
    assert [item["native_id"] for item in latest["items"]] == ["old-image", "old-search"]
    assert latest["thread"]["history_complete"] is False
    assert latest["thread"]["history_cursor"]
    assert latest["has_more_items"] is False  # Only this bounded native page is cached.
    tail_positions = [(item["native_id"], item["position"]) for item in latest["items"]]
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    item_pages = [message for message in audit if message.get("method") == "thread/items/list"]
    assert len(item_pages) == 2
    assert all(
        message["params"]["limit"] == 1
        and message["params"]["sortDirection"] == "desc"
        and "turnId" not in message["params"]
        for message in item_pages
    )
    assert not any(message.get("method") == "thread/turns/list" for message in audit)

    continuation = post(
        f"/api/threads/{thread['id']}/history",
        json={
            "limit": 2,
            "cursor": latest["thread"]["history_cursor"],
        },
    )
    assert continuation.status_code == 202
    older_command = continuation.json()["command"]
    assert older_command["kind"] == "thread.history"

    def complete():
        command = client.get(f"/api/commands/{older_command['id']}").json()["command"]
        assert command["status"] not in {"failed", "unknown"}, command
        result = client.get(history_url).json()
        return result if result["thread"]["history_complete"] else None

    latest = eventually(complete, "bounded older native history page")
    assert [(item["native_id"], item["position"]) for item in latest["items"]] == tail_positions
    assert latest["has_more_items"] and latest["next_before_position"] == tail_positions[0][1]
    older = client.get(history_url + f"&before_position={latest['next_before_position']}").json()
    assert [item["native_id"] for item in older["items"]] == ["old-user-1", "old-answer-1"]
    assert older["has_more_items"] is False
    current = post(f"/api/threads/{thread['id']}/history", json={})
    assert current.status_code == 200 and current.json()["command"] is None
    assert len(client.get("/api/threads").json()["threads"]) == 1
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    assert not any(message.get("method") in {"thread/resume", "turn/start"} for message in audit)
    item_pages = [message for message in audit if message.get("method") == "thread/items/list"]
    assert len(item_pages) == 4
    assert [message["params"].get("cursor") for message in item_pages] == [None, "1", "2", "3"]
    reads = [message for message in audit if message.get("method") == "thread/read"]
    assert len(reads) == 4 and all(message["params"]["includeTurns"] is False for message in reads)


def test_runtime_catalog_and_execution_settings_apply_to_create_and_next_turn(live_system):
    system = live_system
    client, post = system["client"], system["post"]
    host_id = system["host"]["id"]

    def completed(response):
        assert response.status_code == 202, response.text
        command_id = response.json()["command"]["id"]

        def done():
            command = client.get(f"/api/commands/{command_id}").json()["command"]
            assert command["status"] not in {"failed", "unknown"}, command
            return command if command["status"] == "completed" else None

        return eventually(done, "runtime settings command")

    catalog = completed(
        post(
            f"/api/hosts/{host_id}/capabilities",
            json={
                "cwd": str(system["root"]),
                "refresh": True,
            },
        )
    )["result"]
    assert [model["model"] for model in catalog["models"]] == ["test-default", "test-special"]
    assert catalog["models"][0]["futureCatalogField"] == {"preserve": True}
    assert (
        catalog["models"][1]["supportedReasoningEfforts"][1]["reasoningEffort"] == "future-effort"
    )
    assert "PRIVATE-TEST" not in json.dumps(catalog)
    assert catalog["constraints"]["approval_policies"] == ["on-request", "untrusted"]
    cached = client.get(
        f"/api/hosts/{host_id}/capabilities", params={"cwd": str(system["root"])}
    ).json()
    assert cached["capabilities"] == catalog

    initial = {
        "model": "test-special",
        "effort": "future-effort",
        "approval_policy": "untrusted",
        "sandbox_mode": "workspace-write",
        "approvals_reviewer": "user",
        "network_access": True,
    }
    created = post(
        "/api/threads",
        json={
            "host_id": host_id,
            "cwd": str(system["root"]),
            "title": "Settings test",
            "execution_settings": initial,
        },
    )
    assert created.status_code == 202, created.text
    thread_id = created.json()["thread"]["id"]
    start_result = completed(created)["result"]
    assert start_result["effective_settings"] == initial
    assert start_result["settings_revision"] == 0
    started = eventually(
        lambda: (lambda value: value if value["effective_settings"] == initial else None)(
            client.get(f"/api/threads/{thread_id}").json()["thread"]
        ),
        "created effective settings",
    )
    assert started["effective_settings_revision"] == 0

    next_settings = {
        "model": "test-default",
        "effort": "medium",
        "approval_policy": "on-request",
        "sandbox_mode": "read-only",
        "approvals_reviewer": "user",
        "network_access": False,
    }
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    patched = client.patch(
        f"/api/threads/{thread_id}/settings",
        json={
            "execution_settings": next_settings,
            "settings_revision": 0,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["thread"]["settings_revision"] == 1
    assert patched.json()["thread"]["effective_settings"] == initial
    turn = completed(
        post(
            f"/api/threads/{thread_id}/messages",
            json={
                "text": "Use the next-turn settings",
                "client_request_id": uuid4().hex,
            },
        )
    )
    assert turn["result"]["settings_revision"] == 1
    assert turn["result"]["effective_settings"] == next_settings
    interaction = eventually(
        lambda: next(
            (
                value
                for value in client.get(f"/api/threads/{thread_id}").json()["interactions"]
                if value["status"] == "pending"
            ),
            None,
        ),
        "approval retained under selected permissions",
    )
    completed(
        post(
            f"/api/interactions/{interaction['id']}/answer",
            json={"response": {"decision": "accept"}},
        )
    )
    final = eventually(
        lambda: (lambda value: value if value["status"] == "idle" else None)(
            client.get(f"/api/threads/{thread_id}").json()["thread"]
        ),
        "settings turn complete",
    )
    assert final["execution_settings"] == final["effective_settings"] == next_settings
    assert final["effective_settings_revision"] == 1

    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    model_pages = [message for message in audit if message.get("method") == "model/list"]
    assert [message["params"].get("cursor") for message in model_pages] == [None, "1"]
    start = next(message["params"] for message in audit if message.get("method") == "thread/start")
    assert start["model"] == "test-special"
    assert start["config"] == {
        "model_reasoning_effort": "future-effort",
        "sandbox_workspace_write.network_access": True,
    }
    assert start["approvalPolicy"] == "untrusted" and start["sandbox"] == "workspace-write"
    turn_params = next(
        message["params"] for message in audit if message.get("method") == "turn/start"
    )
    assert turn_params["model"] == "test-default" and turn_params["effort"] == "medium"
    assert (
        turn_params["approvalPolicy"] == "on-request" and turn_params["approvalsReviewer"] == "user"
    )
    assert turn_params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert not any(message.get("method") == "thread/resume" for message in audit)


def test_browser_websocket_compacts_large_native_events_and_authenticated_raw_is_exact(live_system):
    system = live_system
    client, post = system["client"], system["post"]
    created = post(
        "/api/threads",
        json={
            "host_id": system["host"]["id"],
            "cwd": str(system["root"]),
            "title": "Large event test",
        },
    )
    assert created.status_code == 202, created.text
    thread_id = created.json()["thread"]["id"]
    command_id = created.json()["command"]["id"]

    def started():
        command = client.get(f"/api/commands/{command_id}").json()["command"]
        assert command["status"] not in {"failed", "unknown"}, command
        return command["result"] if command["status"] == "completed" else None

    native_id = eventually(started, "fake native thread created")["thread"]["id"]
    native_params = {
        "threadId": native_id,
        "turnId": "synthetic-turn",
        "futureEventField": {"version": 7, "values": [False, None, {"opaque": "preserve"}]},
        "item": {
            "id": "synthetic-large-tool",
            "type": "commandExecution",
            "status": "completed",
            "command": "synthetic event; no tool executed",
            "aggregatedOutput": "x" * 200_000,
            "futureNativeFields": {
                "opaque": [1, 2, {"keep": True}],
                "inlineImage": "data:image/png;base64," + "QUJD" * 50_000,
            },
        },
    }
    service = system["node"]["service"]
    asyncio.run_coroutine_threadsafe(
        service._native_event("item/completed", native_params, None), system["node"]["loop"]
    ).result(timeout=5)
    eventually(
        lambda: any(
            item["native_id"] == "synthetic-large-tool"
            for item in client.get(f"/api/threads/{thread_id}?item_limit=10").json()["items"]
        ),
        "synthetic event journaled",
    )

    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    ws_url = system["url"].replace("http://", "ws://") + "/ws/client"
    with connect(
        ws_url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
    ) as browser:
        assert json.loads(browser.recv(timeout=5)) == {"type": "ready"}
        browser.send(json.dumps({"type": "subscribe", "thread_id": thread_id, "after": 0}))
        event = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            frame = browser.recv(timeout=5)
            received = json.loads(frame)
            if (
                received.get("method") == "item/completed"
                and received["params"].get("item", {}).get("id") == "synthetic-large-tool"
            ):
                assert len(frame.encode("utf-8")) < 100_000
                event = received
                break
        assert event is not None, "large native item missing from cursor replay"
        assert event["params"]["_truncated"] is True
        assert len(event["params"]["item"]["aggregatedOutput"]) < 200_000
        assert not event["params"]["item"]["futureNativeFields"]["inlineImage"].startswith(
            "data:image/"
        )
        raw_url = event["params"]["_raw_url"]
        assert raw_url == f"/api/events/{event['id']}"
        original = client.get(raw_url)
        assert original.status_code == 200
        raw_event = original.json()["event"]
        assert raw_event["id"] == event["id"]
        assert raw_event["method"] == "item/completed" and raw_event["thread_id"] == thread_id
        assert raw_event["params"] == native_params
        with httpx.Client(base_url=system["url"], trust_env=False) as anonymous:
            assert anonymous.get(raw_url).status_code == 401
    audit = [json.loads(line) for line in system["audit"].read_text(encoding="utf-8").splitlines()]
    assert not any(message.get("method") == "turn/start" for message in audit)
