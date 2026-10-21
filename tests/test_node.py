"""Node tests use a tiny local JSON-RPC peer, never an account or real inference."""

import asyncio
import base64
import io
import json
import os
import signal
import struct
import sys
import zlib

import httpx
import pytest
from PIL import Image

from codex_anywhere.node.assets import AssetBridge, extract_images, markdown_local_images
from codex_anywhere.node.config import load_config, node_lock, resolve_path, validate_server
from codex_anywhere.node.runtime import CodexRuntime, RuntimeGone
from codex_anywhere.node.service import NodeService, pair_node
from codex_anywhere.node.store import NodeStore

FAKE_CODEX = r"""
import json, os, sys, time
pending = None
pending_resume = None
calls = []
call_params = {}
history_turns = [
    {"id":"stored-turn-1","startedAt":11,"items":[
        {"id":"stored-user","type":"userMessage","content":[{"type":"text","text":"Original question"}]},
        {"id":"stored-answer","type":"agentMessage","text":"Original answer"}]},
    {"id":"stored-turn-2","startedAt":12,"items":[
        {"id":"stored-future","type":"futureNativeItem","payload":{"preserve":True}}]},
]
def history_metadata(thread_id):
    return {"id":thread_id,"cwd":os.path.dirname(os.path.abspath(__file__)),"name":"Saved CLI task",
            "preview":"Original question","createdAt":10,"updatedAt":20,"turns":history_turns}
def emit(value):
    raw = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    # Split bytes inside multibyte UTF-8 as well as JSON tokens.
    for byte in raw:
        sys.stdout.buffer.write(bytes([byte]))
    sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
    message = json.loads(line)
    method, rpc_id = message.get("method"), message.get("id")
    if method:
        calls.append(method)
        call_params.setdefault(method, []).append(message.get("params"))
    if method == "initialize":
        emit({"id":rpc_id,"result":{"userAgent":"fake-test-peer"}})
    elif method == "echo":
        emit({"method":"test/notice","params":{"text":"你好\nline two"}})
        emit({"id":rpc_id,"result":message["params"]})
    elif method == "needsApproval":
        pending = rpc_id
        emit({"id":42,"method":"item/commandExecution/requestApproval","params":{"threadId":"native-1","availableDecisions":["accept","decline"]}})
    elif method == "needsApprovalThenResolve":
        emit({"id":"approval-original","method":"item/commandExecution/requestApproval","params":{"threadId":"native-1","availableDecisions":["accept","decline"]}})
        time.sleep(0.05)
        emit({"method":"serverRequest/resolved","params":{"threadId":"native-1","requestId":"approval-original"}})
        emit({"id":rpc_id,"result":{"expired":True}})
    elif method is None and rpc_id == 42:
        emit({"id":pending,"result":{"approval_id":rpc_id,"response":message["result"]}})
    elif method == "thread/start":
        emit({"id":rpc_id,"result":{"thread":{"id":"native-1","cwd":message["params"]["cwd"]}}})
    elif method == "thread/list":
        emit({"id":rpc_id,"result":{"data":[history_metadata("imported-1")],"nextCursor":"catalog-next"}})
    elif method == "thread/read":
        thread_id = message["params"]["threadId"]
        if thread_id == "denied-import":
            emit({"id":rpc_id,"error":{"code":-32000,"message":"Permission denied"}})
        else:
            metadata=history_metadata(thread_id)
            if not message["params"].get("includeTurns"):
                metadata.pop("turns")
            emit({"id":rpc_id,"result":{"thread":metadata}})
    elif method == "thread/turns/list":
        thread_id = message["params"]["threadId"]
        if thread_id == "legacy-import":
            emit({"id":rpc_id,"error":{"code":-32601,"message":"Unknown method thread/turns/list"}})
        elif thread_id == "broken-import":
            emit({"id":rpc_id,"error":{"code":-32000,"message":"History storage failed"}})
        else:
            cursor = message["params"].get("cursor")
            next_cursor = "page-2" if not cursor or thread_id == "repeated-import" else None
            turn = dict(history_turns[0 if not cursor else 1])
            if message["params"].get("itemsView") == "notLoaded":
                turn["items"] = []
            emit({"id":rpc_id,"result":{"data":[turn],"nextCursor":next_cursor}})
    elif method == "thread/items/list":
        params = message["params"]
        turn = next(turn for turn in history_turns if turn["id"] == params["turnId"])
        offset = int(params.get("cursor", "0"))
        next_cursor = str(offset + 1) if offset + 1 < len(turn["items"]) else None
        emit({"id":rpc_id,"result":{"data":[{"turnId":turn["id"],"item":turn["items"][offset],
             "startedAtMs":turn["startedAt"] * 1000,"completedAtMs":None}],"nextCursor":next_cursor}})
    elif method == "thread/resume":
        if message["params"]["threadId"] == "slow-thread":
            pending_resume = rpc_id
            emit({"method":"test/resumePending","params":{"threadId":"slow-thread"}})
        else:
            emit({"id":rpc_id,"result":{"thread":{"id":"native-1"}}})
    elif method == "releaseResume":
        emit({"id":pending_resume,"result":{"thread":{"id":"slow-thread"}}})
        emit({"id":rpc_id,"result":{}})
    elif method == "turn/start":
        if message["params"]["input"][0].get("text") == "DIE":
            os._exit(3)
        emit({"id":rpc_id,"result":{"turn":{"id":"turn-1"}}})
    elif method == "inspect":
        emit({"id":rpc_id,"result":{"calls":calls,"params":call_params}})
    elif method == "die":
        os._exit(3)
    elif method == "stopReading":
        emit({"id":rpc_id,"result":{}})
        time.sleep(30)
    elif method in {"inheritPipes", "inheritPipesAndExit"}:
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL)
        emit({"id":rpc_id,"result":{"childPid":child.pid}})
        if method == "inheritPipesAndExit":
            os._exit(3)
    elif rpc_id is not None:
        emit({"id":rpc_id,"result":{}})
"""


@pytest.fixture
def peer(tmp_path):
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    return [sys.executable, "-u", str(script)]


def test_runtime_utf8_pending_and_original_approval_ids(peer):
    async def scenario():
        events = []
        approval = asyncio.Event()

        async def event(method, params, rpc_id):
            events.append((method, params, rpc_id))
            if rpc_id is not None:
                approval.set()

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        try:
            values = await asyncio.gather(
                *[
                    runtime.request("echo", {"text": f"多行\n消息 {number} 👋"})
                    for number in range(5)
                ]
            )
            assert [value["text"] for value in values] == [
                f"多行\n消息 {number} 👋" for number in range(5)
            ]
            assert events[0][1]["text"] == "你好\nline two"
            task = asyncio.create_task(runtime.request("needsApproval", {}))
            await asyncio.wait_for(approval.wait(), 3)
            assert not task.done()
            assert events[-1][2] == 42
            await runtime.answer(42, {"decision": "accept"})
            result = await task
            assert result == {"approval_id": 42, "response": {"decision": "accept"}}
            assert not runtime.pending
            with pytest.raises(ValueError, match="expired"):
                await runtime.answer(42, {})
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_runtime_process_death_fails_pending(peer):
    async def scenario():
        async def event(*args):
            pass

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        with pytest.raises(RuntimeGone):
            await runtime.request("die", {})
        await runtime.stopped.wait()
        assert not runtime.server_requests
        assert not runtime.pending
        await runtime.close()

    asyncio.run(scenario())


def test_node_websocket_reconnect_keeps_process_and_replays_outbox(peer, tmp_path):
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed

    async def scenario():
        service = NodeService(
            {
                "server": "http://127.0.0.1:9876",
                "token": "test-token",
                "host_id": "host",
                "name": "test",
            },
            tmp_path,
        )
        runtime = CodexRuntime(peer, service._native_event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        await runtime.start()
        process_id = runtime.process.pid
        await runtime.request("echo", {"text": "offline event"})
        epochs, received_events, responses = [], [], []
        command = {
            "id": "network-cmd",
            "kind": "thread.start",
            "thread_id": "hub-1",
            "payload": {"cwd": str(tmp_path)},
        }

        async def fake_hub(ws):
            hello = json.loads(await ws.recv())
            epochs.append(hello["runtime_id"])
            assert ws.request.headers["Authorization"] == "Bearer test-token"
            await ws.send(json.dumps({"type": "welcome", "host_id": "host", "protocol": 1}))
            await ws.send(json.dumps({"type": "command", "command": command}))
            got_result, got_event = False, False
            async for raw in ws:
                message = json.loads(raw)
                if message["type"] == "event":
                    received_events.append(message)
                    got_event = True
                    # Deliberately withhold ACK on first connection to force replay.
                    if len(epochs) > 1:
                        await ws.send(
                            json.dumps({"type": "event_ack", "event_id": message["event_id"]})
                        )
                elif message["type"] == "command_result":
                    responses.append(message)
                    got_result = True
                if got_event and got_result:
                    await ws.close()
                    return

        try:
            async with serve(fake_hub, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                service.server = f"http://127.0.0.1:{port}"
                for _ in range(2):
                    try:
                        await asyncio.wait_for(service._connect_once(), 5)
                    except ConnectionClosed:
                        pass
            assert epochs == [runtime.runtime_id, runtime.runtime_id]
            assert runtime.process.pid == process_id and runtime.process.returncode is None
            assert received_events[0]["event_id"] == received_events[1]["event_id"]
            assert responses[0] == responses[1]
            calls = await runtime.request("inspect", {})
            assert calls["calls"].count("thread/start") == 1
        finally:
            for task in service.command_tasks:
                await task
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


def test_node_completed_commands_cached_and_uncertain_not_repeated(peer, tmp_path):
    async def scenario():
        service = NodeService(
            {
                "server": "http://127.0.0.1:9876",
                "token": "test-token",
                "host_id": "host",
            },
            tmp_path,
        )
        runtime = CodexRuntime(peer, service._native_event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        await runtime.start()
        command = {
            "id": "c1",
            "kind": "thread.start",
            "thread_id": "hub-1",
            "payload": {"cwd": str(tmp_path)},
        }
        try:
            first = await service.handle_command(command)
            assert first["result"]["thread"]["id"] == "native-1"
            assert await service.handle_command(command) == first
            calls = await runtime.request("inspect", {})
            assert calls["calls"].count("thread/start") == 1
            uncertain = {
                "id": "c2",
                "kind": "turn.start",
                "thread_id": "hub-1",
                "payload": {
                    "native_thread_id": "native-1",
                    "input": [{"type": "text", "text": "DIE"}],
                },
            }
            result = await service.handle_command(uncertain)
            assert result["unknown"] is True
            assert await service.handle_command(uncertain) == result
            row = service.store.db.execute("SELECT status FROM commands WHERE id='c2'").fetchone()
            assert row[0] == "unknown"
        finally:
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


def test_outbox_survives_reopen_and_dedupes_command(tmp_path):
    path = tmp_path / "node.sqlite3"
    store = NodeStore(path)
    event = store.record_event("epoch-1", "item/completed", {"text": "你好"}, rpc_id="original")
    command = {"id": "cmd", "kind": "turn.start", "payload": {}}
    assert store.claim(command) == (True, None)
    store.close()
    store = NodeStore(path)
    store.recover_uncertain()
    assert store.pending_events() == [event]
    claimed, response = store.claim(command)
    assert not claimed and response["unknown"]
    store.acknowledge(event["event_id"])
    assert store.pending_events() == []
    assert store.record_event("epoch-2", "test", {})["seq"] > event["seq"]
    with pytest.raises(ValueError, match="reused"):
        store.claim({**command, "kind": "thread.start"})
    store.close()


def test_paths_can_resolve_any_absolute_accessible_location(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sibling = tmp_path / "project-other"
    sibling.mkdir()
    outside = sibling / "secret.png"
    outside.write_bytes(b"secret")
    assert resolve_path(str(root), directory=True) == root
    assert resolve_path(str(root / ".." / "project-other" / "secret.png")) == outside
    assert resolve_path(str(outside)) == outside
    with pytest.raises(ValueError, match="absolute"):
        resolve_path("secret.png")
    link = root / "escape"
    try:
        link.symlink_to(sibling, target_is_directory=True)
    except OSError:
        return  # Windows may not grant symlink privileges to this test user.
    assert resolve_path(str(link / "secret.png")) == outside


def test_server_requires_https_except_loopback():
    assert validate_server("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert validate_server("http://[::1]:8080") == "http://[::1]:8080"
    assert validate_server("https://example.test") == "https://example.test"
    with pytest.raises(ValueError, match="HTTPS"):
        validate_server("http://example.test")
    assert validate_server("http://example.test", True) == "http://example.test"
    with pytest.raises(ValueError, match="credentials"):
        validate_server("https://user:password@example.test")


def test_node_state_directory_has_a_single_process_owner(tmp_path):
    with node_lock(tmp_path), pytest.raises(ValueError, match="already using"), node_lock(tmp_path):
        pytest.fail("A second process owner must not be admitted")
    # OS lock is released even though the marker file remains.
    with node_lock(tmp_path):
        pass


def test_markdown_excludes_fenced_and_inline_code():
    text = """![visible](image.png)
![spaces](<folder/my image.png>)
![nested](image(1).png)
`![inline](secret.png)`
```python
![hidden](secret.png)
```
~~~
![hidden](secret-2.png)
~~~
![web](https://example.test/image.png)
![file](file:///tmp/image.png)
"""
    assert markdown_local_images(text) == [
        "image.png",
        "folder/my image.png",
        "image(1).png",
        "/tmp/image.png",
    ]


def png_bytes():
    result = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(result, format="PNG")
    return result.getvalue()


def test_output_image_bridge_reads_referenced_images_in_any_directory(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    image = root / "图像.png"
    image.write_bytes(png_bytes())
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    sources = extract_images(
        {"type": "agentMessage", "text": f"![yes](<{image}>)\n![no](<{outside}>)"}
    )
    assert {source.source_path for source in sources} == {str(image), str(outside)}
    encoded = base64.b64encode(png_bytes()).decode()
    assert len(extract_images({"type": "imageGeneration", "result": encoded})) == 1
    assert (
        len(
            extract_images(
                {
                    "type": "mcpToolCall",
                    "result": {
                        "content": [{"type": "image", "mimeType": "image/png", "data": encoded}]
                    },
                },
            )
        )
        == 1
    )

    async def scenario():
        count = 0

        def handle(request):
            nonlocal count
            count += 1
            assert request.headers["Authorization"] == "Bearer token"
            assert b"native-thread" in request.content
            return httpx.Response(
                200, json={"asset": {"id": "asset-1", "url": "/api/assets/asset-1"}}
            )

        store = NodeStore(tmp_path / "asset.sqlite3")
        store.save_thread("native-thread", str(root))
        event = {
            "params": {
                "threadId": "native-thread",
                "item": {"type": "imageView", "id": "item-1", "path": str(image)},
            }
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), headers={"Authorization": "Bearer token"}
        ) as client:
            bridge = AssetBridge(store, client, "https://hub.test")
            first = await bridge.process(event)
            assert first[0]["source_path"] == str(image)
            assert await bridge.process(event) == first
            assert count == 1
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("item_type", "container", "image_type", "image_key", "text_type"),
    [
        ("dynamicToolCall", "contentItems", "inputImage", "imageUrl", "inputText"),
        ("functionCallOutput", "output", "input_image", "image_url", "input_text"),
    ],
)
def test_native_tool_image_output_containers(
    tmp_path, item_type, container, image_type, image_key, text_type
):
    root = tmp_path / "project"
    root.mkdir()
    local_image = root / "allowed.png"
    local_image.write_bytes(png_bytes())
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    encoded = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    arguments_image = root / "argument.png"
    arguments_image.write_bytes(png_bytes())
    output = [
        {"type": image_type, image_key: encoded},
        {"type": text_type, "text": f"![local](<{local_image}>) ![outside](<{outside}>)"},
        {"type": image_type, image_key: "https://example.test/private.png"},
        {"type": image_type, image_key: f"file:///{outside}"},
        {"type": image_type, image_key: "data:image/png;base64,not-an-image"},
    ]
    sources = extract_images(
        {
            "type": item_type,
            container: output,
            "arguments": {
                "type": text_type,
                "text": f"![argument](<{arguments_image}>)",
                container: [{"type": image_type, image_key: encoded}],
            },
        },
    )
    assert len(sources) == 3
    assert {source.source_path for source in sources} == {None, str(local_image), str(outside)}


def test_function_output_string_resolves_relative_markdown_using_cwd(tmp_path):
    image = tmp_path / "result.png"
    image.write_bytes(png_bytes())
    sources = extract_images(
        {"type": "functionCallOutput", "output": "![result](result.png)"},
        str(tmp_path),
    )
    assert len(sources) == 1 and sources[0].source_path == str(image)
    # Similarly named fields in call inputs must never be treated as output.
    assert (
        extract_images(
            {"type": "functionCall", "output": "![result](result.png)"},
            str(tmp_path),
        )
        == []
    )


def test_pair_stores_token_locally_without_returning_it(tmp_path, monkeypatch):
    config_dir = tmp_path / "state"
    original_client = httpx.Client

    def handle(request):
        body = json.loads(request.content)
        assert body["code"] == "ABC123" and "roots" not in body
        return httpx.Response(
            200, json={"host": {"id": "host-1", "name": "laptop"}, "token": "secret-token"}
        )

    def fake_client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("codex_anywhere.node.service.httpx.Client", fake_client)
    result = pair_node("http://localhost:8080", "laptop", config_dir, "ABC123")
    assert result == {"id": "host-1", "name": "laptop"}
    assert json.loads((config_dir / "node.json").read_text())["token"] == "secret-token"
    if os.name != "nt":
        assert (config_dir / "node.json").stat().st_mode & 0o077 == 0


def test_resolved_request_cannot_be_answered_after_waiting_for_writer(peer):
    async def scenario():
        approval, resolved = asyncio.Event(), asyncio.Event()

        async def event(method, params, rpc_id):
            if rpc_id == "approval-original":
                approval.set()
            elif method == "serverRequest/resolved":
                resolved.set()

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        try:
            request = asyncio.create_task(runtime.request("needsApprovalThenResolve", {}))
            await asyncio.wait_for(approval.wait(), 3)
            # Hold the writer until Codex resolves the request independently.
            await runtime._write_lock.acquire()
            answer = asyncio.create_task(
                runtime.answer("approval-original", {"decision": "accept"})
            )
            try:
                await asyncio.wait_for(resolved.wait(), 3)
            finally:
                runtime._write_lock.release()
            with pytest.raises(ValueError, match="expired"):
                await answer
            assert await request == {"expired": True}
            assert not runtime.server_requests
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_concurrent_answers_write_only_one_response(peer):
    async def scenario():
        approval = asyncio.Event()

        async def event(method, params, rpc_id):
            if rpc_id == 42:
                approval.set()

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        try:
            request = asyncio.create_task(runtime.request("needsApproval", {}))
            await asyncio.wait_for(approval.wait(), 3)
            results = await asyncio.gather(
                runtime.answer(42, {"decision": "accept"}),
                runtime.answer(42, {"decision": "decline"}),
                return_exceptions=True,
            )
            assert sum(isinstance(result, ValueError) for result in results) == 1
            assert {"answered": True} in results
            assert (await request)["approval_id"] == 42
            assert not runtime.server_requests
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_controls_and_approval_do_not_wait_for_slow_thread_resume(peer, tmp_path):
    async def scenario():
        service = NodeService(
            {
                "server": "http://127.0.0.1:9876",
                "token": "test-token",
                "host_id": "host",
            },
            tmp_path,
        )
        resuming, approval = asyncio.Event(), asyncio.Event()

        async def event(method, params, rpc_id):
            await service._native_event(method, params, rpc_id)
            if method == "test/resumePending":
                resuming.set()
            if rpc_id == 42:
                approval.set()

        runtime = CodexRuntime(peer, event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        service.store.save_thread("slow-thread", str(tmp_path))
        await runtime.start()
        start_command = {
            "id": "slow-start",
            "kind": "turn.start",
            "thread_id": "hub-thread",
            "payload": {
                "native_thread_id": "slow-thread",
                "input": [{"type": "text", "text": "Continue"}],
            },
        }
        start = asyncio.create_task(service.handle_command(start_command))
        try:
            await asyncio.wait_for(resuming.wait(), 3)
            for kind, extra in [
                ("turn.interrupt", {"turn_id": "active-turn"}),
                (
                    "turn.steer",
                    {
                        "expected_turn_id": "active-turn",
                        "input": [{"type": "text", "text": "Change direction"}],
                    },
                ),
            ]:
                result = await asyncio.wait_for(
                    service.handle_command(
                        {
                            "id": kind,
                            "kind": kind,
                            "thread_id": "hub-thread",
                            "payload": {"native_thread_id": "slow-thread", **extra},
                        }
                    ),
                    1,
                )
                assert "error" not in result
                assert not start.done()
            approval_request = asyncio.create_task(runtime.request("needsApproval", {}))
            await asyncio.wait_for(approval.wait(), 3)
            answer = await asyncio.wait_for(
                service.handle_command(
                    {
                        "id": "answer-during-resume",
                        "kind": "interaction.answer",
                        "thread_id": "hub-thread",
                        "payload": {
                            "runtime_id": runtime.runtime_id,
                            "rpc_id": 42,
                            "response": {"decision": "decline"},
                        },
                    }
                ),
                1,
            )
            assert answer["result"] == {"answered": True}
            assert not start.done()
            assert (await approval_request)["response"] == {"decision": "decline"}
            await runtime.request("releaseResume", {})
            assert (await start)["result"]["turn"]["id"] == "turn-1"
            inspected = await runtime.request("inspect", {})
            assert inspected["calls"].count("thread/resume") == 1
            assert inspected["params"]["thread/resume"][0]["excludeTurns"] is True
            assert "slow-thread" in runtime.loaded_threads
        finally:
            if not start.done():
                start.cancel()
                await asyncio.gather(start, return_exceptions=True)
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


def test_restart_resumes_owned_thread_once_without_loading_history(peer, tmp_path):
    async def scenario():
        config = {
            "server": "http://127.0.0.1:9876",
            "token": "test-token",
            "host_id": "host",
        }
        first = NodeService(config, tmp_path)
        runtime = CodexRuntime(peer, first._native_event, first._runtime_exit, timeout=3)
        first.runtime = runtime
        await runtime.start()
        await first.handle_command(
            {
                "id": "initial-start",
                "kind": "thread.start",
                "thread_id": "hub-thread",
                "payload": {"cwd": str(tmp_path)},
            }
        )
        epoch = runtime.runtime_id
        await runtime.close()
        first.store.close()

        second = NodeService(config, tmp_path)
        runtime = CodexRuntime(peer, second._native_event, second._runtime_exit, timeout=3)
        second.runtime = runtime
        await runtime.start()
        try:
            assert runtime.runtime_id != epoch and not runtime.loaded_threads
            for number in range(2):
                result = await second.handle_command(
                    {
                        "id": f"restarted-turn-{number}",
                        "kind": "turn.start",
                        "thread_id": "hub-thread",
                        "payload": {
                            "native_thread_id": "native-1",
                            "input": [{"type": "text", "text": "Continue"}],
                        },
                    }
                )
                assert "error" not in result
            inspected = await runtime.request("inspect", {})
            assert inspected["calls"].count("thread/resume") == 1
            assert inspected["calls"].count("turn/start") == 2
            params = inspected["params"]["thread/resume"][0]
            assert params == {
                "threadId": "native-1",
                "cwd": str(tmp_path),
                "excludeTurns": True,
            }
        finally:
            await runtime.close()
            second.store.close()

    asyncio.run(scenario())


def test_outbox_window_sends_each_event_once_while_acks_arrive_individually(tmp_path):
    async def scenario():
        service = NodeService(
            {
                "server": "http://127.0.0.1:9876",
                "token": "test-token",
                "host_id": "host",
            },
            tmp_path,
        )
        events = [service.store.record_event("epoch", "delta", {"index": i}) for i in range(100)]
        sent, active = [], set()
        max_active = 0
        yields = 0

        async def send(event):
            nonlocal max_active
            assert event["event_id"] not in active, (
                "An in-flight event was sent again before timeout"
            )
            active.add(event["event_id"])
            max_active = max(max_active, len(active))
            sent.append(event["event_id"])
            return True

        async def ticker():
            nonlocal yields
            while True:
                yields += 1
                await asyncio.sleep(0)

        async def reached(count):
            while len(sent) < count:
                await asyncio.sleep(0)

        def acknowledge(event_id):
            if service.store.acknowledge(event_id):
                active.discard(event_id)
                service.event_wake.set()

        service._send = send
        ticker_task = asyncio.create_task(ticker())
        task = asyncio.create_task(service._outbox())
        try:
            await asyncio.wait_for(reached(100), 2)
            assert yields >= 6  # Sending a full batch yields to the reader regularly.
            # New deltas and repeated wakeups must not replay the existing window.
            for index in range(40):
                events.append(service.store.record_event("epoch", "delta", {"index": index + 100}))
                service.event_wake.set()
                await asyncio.sleep(0)
            assert len(sent) == 100
            # Every ACK frees exactly one slot, without resending its 99 peers.
            for index, event in enumerate(events[:40]):
                acknowledge(event["event_id"])
                assert service.store.acknowledge(event["event_id"]) is False
                await asyncio.wait_for(reached(101 + index), 2)
                assert len(sent) == 101 + index
            for event in events[40:]:
                acknowledge(event["event_id"])
                await asyncio.sleep(0)
            assert service.store.pending_events() == []
            assert len(sent) == len(set(sent)) == 140
            assert max_active == 100
            assert service.store.acknowledge("unknown-event") is False
        finally:
            task.cancel()
            ticker_task.cancel()
            await asyncio.gather(task, ticker_task, return_exceptions=True)
            service.store.close()

    asyncio.run(scenario())


def test_outbox_retries_only_after_timeout_and_stops_after_ack(tmp_path, monkeypatch):
    from codex_anywhere.node import service as node_service

    assert node_service.OUTBOX_RETRY_SECONDS >= 5
    monkeypatch.setattr(node_service, "OUTBOX_RETRY_SECONDS", 0.05)

    async def scenario():
        service = NodeService(
            {
                "server": "http://127.0.0.1:9876",
                "token": "test-token",
                "host_id": "host",
            },
            tmp_path,
        )
        event = service.store.record_event("epoch", "delta", {})
        sent_at = []
        resent = asyncio.Event()

        async def send(value):
            assert value["event_id"] == event["event_id"]
            sent_at.append(asyncio.get_running_loop().time())
            if len(sent_at) == 2:
                resent.set()
            return True

        service._send = send
        task = asyncio.create_task(service._outbox())
        try:
            await asyncio.wait_for(resent.wait(), 1)
            assert len(sent_at) == 2 and sent_at[1] - sent_at[0] >= 0.05
            assert service.store.acknowledge(event["event_id"]) is True
            service.event_wake.set()
            await asyncio.sleep(0.12)
            assert len(sent_at) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            service.store.close()

    asyncio.run(scenario())


def test_rpc_deadline_includes_stdin_backpressure(peer):
    async def scenario():
        async def event(*args):
            pass

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        try:
            await runtime.request("stopReading", {})
            runtime.timeout = 0.05
            before = asyncio.get_running_loop().time()
            with pytest.raises(RuntimeGone, match="timed out"):
                await asyncio.wait_for(
                    runtime.request("echo", {"text": "x" * (2 * 1024 * 1024)}), 1
                )
            assert asyncio.get_running_loop().time() - before < 0.8
            assert not runtime.pending
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_request_notification_and_answer_deadlines_include_writer_lock(peer):
    async def scenario():
        async def event(*args):
            pass

        runtime = CodexRuntime(peer, event, timeout=3)
        await runtime.start()
        runtime.timeout = 0.03
        await runtime._write_lock.acquire()
        try:
            with pytest.raises(RuntimeGone, match="timed out"):
                await asyncio.wait_for(runtime.request("echo", {}), 1)
            with pytest.raises(RuntimeGone, match="timed out"):
                await asyncio.wait_for(runtime.notify("notice", {}), 1)
            runtime.server_requests["42"] = {"id": 42}
            with pytest.raises(RuntimeGone, match="timed out"):
                await asyncio.wait_for(runtime.answer(42, {"decision": "decline"}), 1)
            assert not runtime.pending
        finally:
            runtime._write_lock.release()
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("callback_hangs", [False, True])
def test_exit_callback_cannot_prevent_runtime_stopped(peer, monkeypatch, callback_hangs):
    from codex_anywhere.node import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PIPE_CLOSE_TIMEOUT", 0.05)

    async def scenario():
        called = asyncio.Event()

        async def event(*args):
            pass

        async def exit_callback(*args):
            called.set()
            if callback_hangs:
                await asyncio.Event().wait()
            raise OSError("simulated persistence failure")

        runtime = CodexRuntime(peer, event, exit_callback, timeout=3)
        await runtime.start()
        await asyncio.wait_for(runtime.close(), 1)
        assert called.is_set() and runtime.stopped.is_set()
        assert runtime._reader.done() and runtime._stderr.done()

    asyncio.run(scenario())


def test_runtime_close_is_bounded_when_tool_child_inherits_output(peer, monkeypatch):
    from codex_anywhere.node import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROCESS_STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(runtime_module, "PIPE_CLOSE_TIMEOUT", 0.2)

    async def scenario():
        async def event(*args):
            pass

        runtime = CodexRuntime(peer, event, timeout=3)
        child_pid = None
        try:
            await runtime.start()
            child_pid = (await runtime.request("inheritPipes", {}))["childPid"]
            await asyncio.wait_for(runtime.close(), 1)
            assert runtime.stopped.is_set()
            assert runtime._reader.done() and runtime._stderr.done()
            assert runtime.process.returncode is not None
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            await runtime.close()

    asyncio.run(scenario())


def test_runtime_detects_spontaneous_exit_despite_inherited_child_pipes(peer, monkeypatch):
    from codex_anywhere.node import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROCESS_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(runtime_module, "PIPE_CLOSE_TIMEOUT", 0.2)

    async def scenario():
        exits = []

        async def event(*args):
            pass

        async def on_exit(epoch, code):
            exits.append(code)

        runtime = CodexRuntime(peer, event, on_exit, timeout=3)
        child_pid = None
        try:
            await runtime.start()
            child_pid = (await runtime.request("inheritPipesAndExit", {}))["childPid"]
            await asyncio.wait_for(runtime.stopped.wait(), 1)
            assert runtime.process.returncode == 3 and exits == [3]
            assert runtime._reader.done()
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            await runtime.close()

    asyncio.run(scenario())


def test_output_image_batch_has_count_and_memory_limits(tmp_path, monkeypatch):
    from codex_anywhere.node import assets as node_assets

    data = png_bytes()
    files = []
    for index in range(5):
        path = tmp_path / f"{index}.png"
        path.write_bytes(data)
        files.append(path)
    text = "\n".join(f"![image](<{path}>)" for path in files)
    monkeypatch.setattr(node_assets, "MAX_OUTPUT_IMAGES", 2)
    assert len(extract_images({"type": "agentMessage", "text": text})) == 2
    monkeypatch.setattr(node_assets, "MAX_OUTPUT_IMAGES", 16)
    monkeypatch.setattr(node_assets, "MAX_OUTPUT_BYTES", len(data) * 2)
    sources = extract_images({"type": "agentMessage", "text": text})
    assert len(sources) == 2 and sum(len(source.data) for source in sources) == len(data) * 2


def test_local_image_read_is_bounded_even_if_file_grows(tmp_path, monkeypatch):
    from pathlib import Path

    from codex_anywhere.node import assets as node_assets

    image = tmp_path / "growing.png"
    image.write_bytes(png_bytes())
    limit = len(png_bytes()) + 5
    monkeypatch.setattr(node_assets, "MAX_IMAGE_BYTES", limit)
    original_open = Path.open
    reads = []

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    def growing_open(path, *args, **kwargs):
        if path == image:
            return GrowingFile(png_bytes() + b"extra bytes" * 100)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)
    assert extract_images({"type": "imageView", "path": str(image)}) == []
    assert reads == [limit + 1]


def test_image_pixel_limit_matches_hub_before_pixel_decode():
    from codex_anywhere.node.assets import verified_image

    data = bytearray(png_bytes())
    # Amend only IHDR dimensions and CRC; no large pixel buffer is allocated.
    data[16:24] = struct.pack(">II", 5001, 5000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    with pytest.raises(ValueError, match="25 megapixel"):
        verified_image(bytes(data), "large.png")


def test_old_node_config_discards_roots_and_preserves_identity(tmp_path):
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "server": "https://hub.test",
                "host_id": "host-1",
                "token": "keep-me",
                "roots": ["/no-longer-existing-root"],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config == {"server": "https://hub.test", "host_id": "host-1", "token": "keep-me"}
    assert json.loads(path.read_text(encoding="utf-8")) == config


def test_node_cli_has_no_project_or_artifact_allowlist_options():
    from click.testing import CliRunner

    from codex_anywhere.cli import main

    runner = CliRunner()
    pair_help = runner.invoke(main, ["node", "pair", "--help"])
    run_help = runner.invoke(main, ["node", "run", "--help"])
    assert pair_help.exit_code == run_help.exit_code == 0
    assert "--root" not in pair_help.output
    assert "--asset-root" not in run_help.output


def test_directory_browsing_and_explicit_project_creation_use_any_absolute_path(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://localhost:9876", "host_id": "test", "token": "test"},
            tmp_path / "node",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "z-child").mkdir()
        (elsewhere / "a-child").mkdir()
        (elsewhere / "file.txt").write_text("not a directory", encoding="utf-8")
        try:
            result = await service.handle_command(
                {"id": "browse", "kind": "directory.list", "payload": {"path": str(elsewhere)}}
            )
            listing = result["result"]
            assert listing["path"] == str(elsewhere) and listing["parent"] == str(tmp_path)
            assert [item["name"] for item in listing["directories"]] == ["a-child", "z-child"]
            assert listing["locations"] and listing["home"] and not listing["truncated"]
            path = tmp_path / "not-present" / "new-project"
            missing = await service.handle_command(
                {"id": "missing", "kind": "project.open", "payload": {"cwd": str(path)}}
            )
            assert "error" in missing and not path.exists()
            opened = await service.handle_command(
                {
                    "id": "create",
                    "kind": "project.open",
                    "payload": {"cwd": str(path), "name": "Example", "create_directory": True},
                }
            )
            assert opened["result"] == {"cwd": str(path), "name": "Example"} and path.is_dir()
            relative = await service.handle_command(
                {
                    "id": "relative",
                    "kind": "project.open",
                    "payload": {"cwd": "relative-directory", "create_directory": True},
                }
            )
            assert "error" in relative
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_directory_listing_is_bounded(tmp_path, monkeypatch):
    from codex_anywhere.node import directories

    for name in ["a", "b", "c"]:
        (tmp_path / name).mkdir()
    monkeypatch.setattr(directories, "MAX_DIRECTORIES", 2)
    result = directories.list_directories({"path": str(tmp_path)})
    assert len(result["directories"]) == 2 and result["truncated"] is True


def test_catalog_includes_all_sources_and_delegates_filters_to_codex(peer, tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://localhost:9876", "host_id": "test", "token": "test"}, tmp_path
        )
        runtime = CodexRuntime(peer, service._native_event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        await runtime.start()
        try:
            result = await service.handle_command(
                {
                    "id": "catalog",
                    "kind": "thread.list",
                    "payload": {
                        "cursor": "first-cursor",
                        "limit": 20,
                        "archived": True,
                        "search": "Original",
                        "cwd": str(tmp_path),
                    },
                }
            )
            catalog = result["result"]
            assert catalog["nextCursor"] == "catalog-next"
            assert catalog["data"][0]["id"] == "imported-1" and "turns" not in catalog["data"][0]
            inspected = await runtime.request("inspect", {})
            params = inspected["params"]["thread/list"][0]
            assert set(params["sourceKinds"]) == {
                "cli",
                "vscode",
                "appServer",
                "exec",
                "unknown",
                "subAgent",
                "subAgentReview",
                "subAgentCompact",
                "subAgentThreadSpawn",
                "subAgentOther",
            }
            assert "useStateDbOnly" not in params
            assert params["cursor"] == "first-cursor" and params["limit"] == 20
            assert params["archived"] is True and params["searchTerm"] == "Original"
            assert params["cwd"] == str(tmp_path) and params["modelProviders"] == []
            assert (
                "thread/resume" not in inspected["calls"] and "turn/start" not in inspected["calls"]
            )
        finally:
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("thread_id", ["imported-1", "legacy-import"])
def test_read_only_import_streams_ordered_native_history_and_can_later_resume(
    peer, tmp_path, thread_id
):
    async def scenario():
        service = NodeService(
            {"server": "http://localhost:9876", "host_id": "test", "token": "test"}, tmp_path
        )
        runtime = CodexRuntime(peer, service._native_event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        await runtime.start()
        command = {
            "id": "import-history",
            "kind": "thread.import",
            "thread_id": "hub-import",
            "payload": {"native_thread_id": thread_id, "title": "Selected task"},
        }
        try:
            result = await service.handle_command(command)
            assert "error" not in result, result
            summary = result["result"]
            assert summary["itemCount"] == 3 and summary["thread"]["id"] == thread_id
            assert summary["thread"]["title"] == "Selected task"
            assert summary["thread"]["createdAt"] == 10 and summary["thread"]["updatedAt"] == 20
            assert "turns" not in summary["thread"] and "Original answer" not in json.dumps(summary)
            assert service.store.thread_cwd(thread_id) == str(tmp_path)
            assert thread_id not in runtime.loaded_threads
            events = service.store.pending_events()
            assert [event["method"] for event in events] == [
                "anywhere/import_metadata",
                "anywhere/import_item",
                "anywhere/import_item",
                "anywhere/import_item",
                "anywhere/import_complete",
            ]
            items = [
                event["params"] for event in events if event["method"] == "anywhere/import_item"
            ]
            assert [item["position"] for item in items] == [1, 2, 3]
            assert [item["createdAt"] for item in items] == [11, 11, 12]
            assert [item["item"]["id"] for item in items] == [
                "stored-user",
                "stored-answer",
                "stored-future",
            ]
            assert items[-1]["item"] == {
                "id": "stored-future",
                "type": "futureNativeItem",
                "payload": {"preserve": True},
            }
            assert all(event["params"]["commandId"] == command["id"] for event in events)
            assert len(service.store.asset_jobs()) == 3
            assert await service.handle_command(command) == result
            assert (
                len(service.store.pending_events()) == 5
            )  # Deduped command emits no duplicate history.
            inspected = await runtime.request("inspect", {})
            assert (
                "thread/resume" not in inspected["calls"] and "turn/start" not in inspected["calls"]
            )
            page_params = inspected["params"]["thread/turns/list"]
            assert all(
                p["itemsView"] == "notLoaded" and p["limit"] == 50 and p["sortDirection"] == "asc"
                for p in page_params
            )
            if thread_id == "legacy-import":
                assert len(page_params) == 1
                assert [p["includeTurns"] for p in inspected["params"]["thread/read"]] == [
                    False,
                    True,
                ]
            else:
                assert len(page_params) == 2 and page_params[1]["cursor"] == "page-2"
                item_pages = inspected["params"]["thread/items/list"]
                assert len(item_pages) == 3
                assert all(
                    page["limit"] == 1 and page["sortDirection"] == "asc" for page in item_pages
                )
            continued = await service.handle_command(
                {
                    "id": "continue-import",
                    "kind": "turn.start",
                    "payload": {
                        "native_thread_id": thread_id,
                        "input": [{"type": "text", "text": "Continue this task"}],
                    },
                }
            )
            assert "error" not in continued
            inspected = await runtime.request("inspect", {})
            assert inspected["params"]["thread/resume"][0]["threadId"] == thread_id
            assert inspected["params"]["turn/start"][0]["threadId"] == thread_id
        finally:
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("thread_id", ["denied-import", "broken-import", "repeated-import"])
def test_failed_import_does_not_hydrate_or_signal_completion(peer, tmp_path, thread_id):
    async def scenario():
        service = NodeService(
            {"server": "http://localhost:9876", "host_id": "test", "token": "test"}, tmp_path
        )
        runtime = CodexRuntime(peer, service._native_event, service._runtime_exit, timeout=3)
        service.runtime = runtime
        await runtime.start()
        try:
            result = await service.handle_command(
                {
                    "id": "bad-import",
                    "kind": "thread.import",
                    "payload": {"native_thread_id": thread_id},
                }
            )
            assert "error" in result
            assert not any(
                event["method"] == "anywhere/import_complete"
                for event in service.store.pending_events()
            )
            inspected = await runtime.request("inspect", {})
            assert all(not p["includeTurns"] for p in inspected["params"]["thread/read"])
            assert (
                "thread/resume" not in inspected["calls"] and "turn/start" not in inspected["calls"]
            )
        finally:
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())


def test_imported_user_images_bridge_data_and_local_urls_without_remote_download(tmp_path):
    image = tmp_path / "historical.png"
    image.write_bytes(png_bytes())
    url = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    item = {
        "id": "historical-user",
        "type": "userMessage",
        "content": [
            {"type": "image", "url": url},
            {"type": "localImage", "path": str(image)},
            {"type": "image", "url": image.as_uri()},
            {"type": "image", "url": "https://external.test/no-download.png"},
        ],
    }
    images = extract_images(item, str(tmp_path))
    assert len(images) == 2 and {source.source_path for source in images} == {None, str(image)}

    async def scenario():
        store = NodeStore(tmp_path / "node.sqlite3")
        store.save_thread("imported-native", str(tmp_path))
        uploaded = []

        def handle(request):
            assert str(request.url) == "https://hub.test/api/node/assets"
            uploaded.append(request.content)
            return httpx.Response(
                200,
                json={"asset": {"id": str(len(uploaded)), "url": f"/api/assets/{len(uploaded)}"}},
            )

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                bridge = AssetBridge(store, client, "https://hub.test")
                assets = await bridge.process(
                    {
                        "method": "anywhere/import_item",
                        "params": {
                            "threadId": "imported-native",
                            "commandId": "import-1",
                            "item": item,
                        },
                    }
                )
                assert len(assets) == 2 and uploaded
        finally:
            store.close()

    asyncio.run(scenario())


def test_history_fallback_only_handles_unsupported_pagination():
    from codex_anywhere.node.history import _pagination_unsupported
    from codex_anywhere.node.runtime import RpcError

    assert _pagination_unsupported(RpcError("Method not found", code=-32601))
    assert _pagination_unsupported(
        RpcError("thread/turns/list is only supported for paginated threads")
    )
    assert not _pagination_unsupported(RpcError("thread/turns/list failed: thread not found"))
    assert not _pagination_unsupported(RpcError("thread/turns/list failed: permission denied"))
