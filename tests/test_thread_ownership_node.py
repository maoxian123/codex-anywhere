"""Writer lifecycle tests use a real stdio peer, without Codex inference."""

import asyncio
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from codex_anywhere.node import runtime as runtime_module
from codex_anywhere.node import service as service_module
from codex_anywhere.node.runtime import CodexRuntime
from codex_anywhere.node.service import NodeService

PEER = r"""
import json, os, sys
calls = []
pending = None
pending_turn = None
pending_steer = None
turn_count = 0
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method, rpc_id = message.get("method"), message.get("id")
    params = message.get("params", {})
    if method:
        calls.append([method, params])
    if method == "initialize":
        emit({"id":rpc_id,"result":{"userAgent":"fake-writer-peer"}})
    elif method == "emit":
        emit(params)
        emit({"id":rpc_id,"result":{}})
    elif method == "inspect":
        emit({"id":rpc_id,"result":{"calls":calls}})
    elif method == "thread/read":
        emit({"id":rpc_id,"result":{"thread":{"id":params["threadId"],"cwd":os.path.dirname(__file__)}}})
    elif method == "thread/start":
        emit({"id":rpc_id,"result":{"thread":{"id":"created","cwd":params["cwd"],"path":os.path.join(os.path.dirname(__file__),"created.jsonl")}}})
    elif method == "thread/name/set":
        if params["name"] == "unsupported":
            emit({"id":rpc_id,"error":{"code":-32601,"message":"Unknown method thread/name/set"}})
        else:
            with open(os.path.join(os.path.dirname(__file__),"created.jsonl"),"w") as output:
                output.write("fake rollout")
            emit({"id":rpc_id,"result":{}})
    elif method == "thread/resume":
        if params["threadId"] == "slow":
            pending = rpc_id
            emit({"method":"test/resumePending","params":{"threadId":"slow"}})
        else:
            emit({"id":rpc_id,"result":{"thread":{"id":params["threadId"]},"approvalPolicy":"on-request","sandbox":{"type":"workspaceWrite"}}})
    elif method == "releaseResume":
        emit({"id":pending,"result":{"thread":{"id":"slow"}}})
        emit({"id":rpc_id,"result":{}})
    elif method == "turn/start":
        turn_count += 1
        turn_id = "turn-" + str(turn_count)
        text = params["input"][0].get("text", "")
        if text == "reject":
            emit({"id":rpc_id,"error":{"code":-32000,"message":"Native start rejected"}})
        elif text in {"defer", "fast-defer"}:
            pending_turn = (rpc_id, params["threadId"], turn_id)
            if text == "fast-defer":
                emit({"method":"turn/started","params":{"threadId":params["threadId"],"turn":{"id":turn_id}}})
                emit({"method":"turn/completed","params":{"threadId":params["threadId"],"turn":{"id":turn_id,"status":"completed"}}})
            emit({"method":"test/turnPending","params":{"threadId":params["threadId"],"turnId":turn_id}})
        elif text in {"completed", "failed", "interrupted"}:
            emit({"id":rpc_id,"result":{"turn":{"id":turn_id,"status":text}}})
        else:
            emit({"method":"turn/started","params":{"threadId":params["threadId"],"turn":{"id":turn_id}}})
            emit({"id":rpc_id,"result":{"turn":{"id":turn_id,"status":"inProgress"}}})
    elif method == "finishTurnRpc":
        if params.get("reject"):
            emit({"id":pending_turn[0],"error":{"code":-32000,"message":"Native start rejected"}})
        else:
            emit({"id":pending_turn[0],"result":{"turn":{"id":pending_turn[2],"status":params.get("status","inProgress")}}})
        pending_turn = None
        emit({"id":rpc_id,"result":{}})
    elif method == "turn/steer":
        if params["input"][0].get("text") == "defer":
            pending_steer = rpc_id
            emit({"method":"test/steerPending","params":{"threadId":params["threadId"]}})
        else:
            emit({"id":rpc_id,"result":{}})
    elif method == "finishSteerRpc":
        emit({"id":pending_steer,"result":{}})
        pending_steer = None
        emit({"id":rpc_id,"result":{}})
    elif method == "thread/unsubscribe":
        if params["threadId"] == "unsupported":
            emit({"id":rpc_id,"error":{"code":-32601,"message":"Unknown method thread/unsubscribe"}})
        elif params["threadId"] == "unconfirmed":
            emit({"id":rpc_id,"result":{}})
        else:
            emit({"id":rpc_id,"result":{}})
            emit({"method":"thread/closed","params":{"threadId":params["threadId"]}})
    elif rpc_id is not None:
        emit({"id":rpc_id,"result":{}})
"""


class Ownership:
    state = "available"

    def __init__(self):
        self.terminated = []

    def inspect(self, native_id, *, managed=False):
        return {"state": "managed" if managed else self.state, "owner_token": "token"}

    def terminate(self, native_id, token):
        if token != "token":
            raise ValueError("Writer changed")
        self.terminated.append(native_id)
        self.state = "available"


async def make_service(tmp_path):
    script = tmp_path / "writer_peer.py"
    script.write_text(PEER, encoding="utf-8")
    service = NodeService(
        {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"},
        tmp_path,
    )
    service.ownership = Ownership()
    runtime = CodexRuntime(
        [sys.executable, "-u", str(script)], service._native_event, service._runtime_exit, timeout=2
    )
    service.runtime = runtime
    await runtime.start()
    service.execution_epoch = runtime.runtime_id
    return service, runtime


def command(kind, native_id="native", **payload):
    return {
        "id": f"{kind}-{uuid.uuid4()}",
        "kind": kind,
        "payload": {"native_thread_id": native_id, **payload},
    }


def writer_request(client="tab-1", *, issued_at=None, expires_at=None):
    return {
        "client_id": client,
        "issued_at": time.time() if issued_at is None else issued_at,
        "expires_at": time.time() + 90 if expires_at is None else expires_at,
    }


async def notify(runtime, method, params, rpc_id=None):
    message = {"method": method, "params": params}
    if rpc_id is not None:
        message["id"] = rpc_id
    await runtime.request("emit", message)


async def cleanup(service, runtime):
    await runtime.close()
    service.store.close()


def test_takeover_preserves_native_settings_and_manual_release_ignores_browser_presence(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            taken = await service.handle_command(command("thread.takeover", **writer_request()))
            assert taken["result"]["ownership"]["state"] == "managed"
            presence = await service.handle_command(
                command("thread.presence", **writer_request("tab-2"))
            )
            assert presence["result"]["ignored"] is True
            legacy = await service.handle_command(
                command("thread.release", **writer_request(expires_at=0))
            )
            assert legacy["result"]["ignored"] is True
            assert "native" in runtime.loaded_threads
            released = await service.handle_command(
                command("thread.release", manual=True, **writer_request())
            )
            assert released["result"]["released"] is True
            assert "native" not in runtime.loaded_threads
            assert "native" not in service.thread_execution
            calls = (await runtime.request("inspect", {}))["calls"]
            resumed = next(params for method, params in calls if method == "thread/resume")
            assert resumed == {"threadId": "native", "cwd": str(tmp_path), "excludeTurns": True}
            assert not any(method == "turn/start" for method, _ in calls)
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_browser_noops_cannot_create_or_change_completion_deadline(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            await service.handle_command(command("thread.takeover"))
            for kind in ["thread.presence", "thread.release"]:
                result = await service.handle_command(
                    command(kind, client_id="old-page", issued_at=10, expires_at=0)
                )
                assert result["result"]["ignored"] is True
            assert not service.writer_request_versions
            assert not service.completion_idle_since
            assert not service._release_due("native", 1000000000)
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "title,persisted", [("User title", True), ("unsupported", False), (None, False)]
)
def test_new_empty_thread_is_persisted_or_kept_until_first_turn(
    tmp_path, monkeypatch, title, persisted
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            started = await service.handle_command(
                command("thread.start", cwd=str(tmp_path), title=title)
            )
            assert started["result"]["thread"]["id"] == "created"
            await service.handle_command(command("thread.presence", "created", **writer_request()))
            result = await service.handle_command(
                command("thread.release", "created", manual=True, **writer_request())
            )
            assert result["result"]["released"] is persisted
            if not persisted:
                assert result["result"]["deferred"] is True
                assert "not persisted" in result["result"]["reason"]
                assert "created" in runtime.loaded_threads
                await service.handle_command(
                    command("turn.start", "created", input=[{"type": "text", "text": "first"}])
                )
                await notify(
                    runtime, "turn/completed", {"threadId": "created", "turn": {"id": "turn-1"}}
                )
                assert "created" in runtime.loaded_threads
                completed = service.completion_idle_since["created"]
                monkeypatch.setattr(
                    service_module,
                    "time",
                    SimpleNamespace(time=time.time, monotonic=lambda: completed + 120),
                )
                await service._sweep_threads_once()
                assert "created" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_web_exit_preserves_turn_and_approval_until_completed_idle_window(tmp_path, monkeypatch):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = {"now": 1000.0}
        monkeypatch.setattr(
            service_module, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock["now"])
        )
        try:
            await service.handle_command(command("thread.takeover", **writer_request()))
            started = await service.handle_command(
                command("turn.start", input=[{"type": "text", "text": "continue"}])
            )
            assert started.get("error") is None
            released = await service.handle_command(
                command("thread.release", **writer_request(expires_at=0))
            )
            assert released["result"]["ignored"] is True
            clock["now"] += 600
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            await notify(runtime, "turn/completed", {"threadId": "native", "turn": {"id": "old"}})
            assert "native" not in service.completion_idle_since
            await notify(
                runtime, "item/commandExecution/requestApproval", {"threadId": "native"}, rpc_id=123
            )
            clock["now"] += 600
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            await notify(
                runtime, "serverRequest/resolved", {"threadId": "native", "requestId": 123}
            )
            await notify(
                runtime, "turn/completed", {"threadId": "native", "turn": {"id": "turn-1"}}
            )
            completed = clock["now"]
            clock["now"] = completed + 119
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["now"] = completed + 120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
            await service.handle_command(
                command("turn.start", input=[{"type": "text", "text": "next"}])
            )
            assert "native" in runtime.loaded_threads
            calls = (await runtime.request("inspect", {}))["calls"]
            assert sum(method == "thread/resume" for method, _ in calls) == 2
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_release_cannot_race_pending_resume_or_turn(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            service.store.save_thread("slow", str(tmp_path))
            start = asyncio.create_task(
                service.handle_command(
                    command("turn.start", "slow", input=[{"type": "text", "text": "hello"}])
                )
            )
            while not runtime.pending:
                await asyncio.sleep(0)
            release = asyncio.create_task(
                service.handle_command(
                    command("thread.release", "slow", manual=True, **writer_request())
                )
            )
            await service._sweep_threads_once()
            result = await release
            assert not start.done()
            await runtime.request("releaseResume", {})
            await start
            assert result["result"]["deferred"] is True
            calls = (await runtime.request("inspect", {}))["calls"]
            assert not any(method == "thread/unsubscribe" for method, _ in calls)
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("native_id", ["unsupported", "unconfirmed"])
def test_release_requires_native_confirmation(tmp_path, monkeypatch, native_id):
    monkeypatch.setattr(service_module, "THREAD_RELEASE_TIMEOUT", 0.1)

    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            await service.handle_command(command("thread.takeover", native_id, **writer_request()))
            result = await service.handle_command(
                command("thread.release", native_id, manual=True, **writer_request())
            )
            assert result["result"]["released"] is False
            assert result["result"]["error"]
            assert native_id in runtime.loaded_threads
            await service._sweep_threads_once()
            calls = (await runtime.request("inspect", {}))["calls"]
            assert sum(method == "thread/unsubscribe" for method, _ in calls) == 1
            # A later explicit send can re-establish subscription after a failed release.
            result = await service.handle_command(
                command("turn.start", native_id, input=[{"type": "text", "text": "hello"}])
            )
            assert result.get("error") is None
            assert native_id not in service.release_errors
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_external_takeover_needs_confirmation_and_current_token(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            service.ownership.state = "external"
            denied = await service.handle_command(command("thread.takeover"))
            assert "Confirm" in denied["error"]
            assert not service.ownership.terminated
            denied = await service.handle_command(
                command("thread.takeover", confirmed=True, owner_token="old")
            )
            assert "Writer changed" in denied["error"]
            accepted = await service.handle_command(
                command("thread.takeover", confirmed=True, owner_token="token", **writer_request())
            )
            assert accepted["result"]["ownership"]["state"] == "managed"
            assert service.ownership.terminated == ["native"]
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["available", "external"])
@pytest.mark.parametrize(
    "prior_release,expired,issued_at",
    [(True, False, 10), (True, True, 10), (True, False, 20), (False, True, 10)],
)
def test_late_or_expired_takeover_has_no_side_effects(
    tmp_path, state, prior_release, expired, issued_at
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            if prior_release:
                await service.handle_command(
                    command("thread.release", manual=True, **writer_request(issued_at=20))
                )
            service.ownership.state = state
            result = await service.handle_command(
                command(
                    "thread.takeover",
                    confirmed=True,
                    owner_token="token",
                    **writer_request(
                        issued_at=issued_at, expires_at=time.time() + (-1 if expired else 90)
                    ),
                )
            )
            assert "stale" in result.get("error", "") or "expired" in result.get("error", "")
            assert not service.ownership.terminated
            assert "native" not in runtime.loaded_threads
            calls = (await runtime.request("inspect", {}))["calls"]
            assert not any(method == "thread/resume" for method, _ in calls)
            if prior_release:
                assert service.writer_request_versions[("native", "tab-1")] == (20, True)
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_takeover_can_reuse_same_millisecond_live_presence(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            presence = writer_request(issued_at=10)
            await service.handle_command(command("thread.presence", **presence))
            service.ownership.state = "external"
            result = await service.handle_command(
                command("thread.takeover", confirmed=True, owner_token="token", **presence)
            )
            assert result["result"]["ownership"]["state"] == "managed"
            assert service.ownership.terminated == ["native"]
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("clock_offset", [-3600, 3600])
def test_hub_clock_skew_preserves_request_freshness_without_browser_leases(tmp_path, clock_offset):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        try:
            service._sync_hub_clock({"server_time": time.time() + clock_offset})
            live = writer_request(issued_at=10, expires_at=service._hub_now() + 90)
            taken = await service.handle_command(command("thread.takeover", **live))
            assert taken["result"]["ownership"]["state"] == "managed"
            assert not service.completion_idle_since
            released = await service.handle_command(
                command(
                    "thread.release",
                    manual=True,
                    **writer_request(issued_at=11, expires_at=service._hub_now() + 90),
                )
            )
            assert released["result"]["released"] is True
            service.ownership.state = "external"
            expired = writer_request(issued_at=12, expires_at=service._hub_now() - 1)
            denied = await service.handle_command(
                command("thread.takeover", confirmed=True, owner_token="token", **expired)
            )
            assert "expired" in denied["error"]
            assert not service.ownership.terminated
            assert "native" not in runtime.loaded_threads
            calls = (await runtime.request("inspect", {}))["calls"]
            assert sum(method == "thread/resume" for method, _ in calls) == 1
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_hub_clock_uses_monotonic_elapsed_and_legacy_fallback(tmp_path, monkeypatch):
    service = NodeService(
        {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"}, tmp_path
    )
    clock = {"wall": 1000.0, "monotonic": 10.0}
    monkeypatch.setattr(
        service_module,
        "time",
        SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]),
    )
    try:
        service._sync_hub_clock({"server_time": 4600.0})
        assert service._hub_now() == 4600.0
        clock.update(wall=2000.0, monotonic=25.0)
        assert service._hub_now() == 4615.0
        service._sync_hub_clock({})
        assert service._hub_now() == 2000.0
    finally:
        service.store.close()


@pytest.mark.parametrize("layout", ["global", "local"])
@pytest.mark.parametrize("subdirectory", ["bin", "codex"])
def test_windows_npm_starts_native_writer_directly(tmp_path, monkeypatch, layout, subdirectory):
    wrapper = (
        tmp_path / "codex.cmd"
        if layout == "global"
        else tmp_path / "node_modules" / ".bin" / "codex.cmd"
    )
    package = tmp_path / "node_modules" / "@openai" / "codex"
    binary = (
        package
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / subdirectory
        / "codex.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test")
    monkeypatch.setattr(runtime_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runtime_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _: str(wrapper))
    assert runtime_module.native_codex_command(["codex", "app-server"]) == [
        str(binary),
        "app-server",
    ]
    binary.unlink()
    with pytest.raises(ValueError, match="native Codex executable"):
        runtime_module.native_codex_command(["codex", "app-server"])
