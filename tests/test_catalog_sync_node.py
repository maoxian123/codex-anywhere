"""Background discovery uses metadata RPCs and remains independent from chat work."""

import asyncio
import copy
import json
import sys

import pytest
from websockets.asyncio.server import serve

from codex_anywhere.node.runtime import CodexRuntime, RpcError
from codex_anywhere.node.service import NodeService
from codex_anywhere.node.store import NodeStore
from codex_anywhere.node.sync import CatalogSynchronizer, SyncSettings, negotiated_settings


class MetadataPeer:
    def __init__(self, catalogs):
        self.runtime_id = "test-runtime"
        self.catalogs = list(catalogs)
        self.requests = []
        self.last = {"data": []}

    async def request(self, method, params):
        self.requests.append((method, copy.deepcopy(params)))
        assert method == "thread/list", "Discovery must not resume, read full history, or infer"
        await asyncio.sleep(0)
        value = self.catalogs.pop(0) if self.catalogs else self.last
        if isinstance(value, Exception):
            raise value
        self.last = value
        return copy.deepcopy(value)


def metadata(thread_id="native-a", *, updated=10, name="Saved task", cwd="/project"):
    return {
        "id": thread_id,
        "updatedAt": updated,
        "createdAt": 1,
        "cwd": cwd,
        "name": name,
        "preview": "Original question",
        "turns": [{"items": ["never download this history"]}],
    }


@pytest.fixture
def sync_factory(tmp_path):
    stores = []

    def make(catalogs, *, settings=None, busy=lambda: False, current=lambda: True):
        runtime = MetadataPeer(catalogs)
        store = NodeStore(tmp_path / f"node-{len(stores)}.sqlite3")
        stores.append(store)
        wakes = []
        sync = CatalogSynchronizer(
            runtime,
            store,
            lambda: wakes.append(True),
            settings or SyncSettings(),
            is_current=current,
            foreground_busy=busy,
        )
        return sync, runtime, store, wakes

    yield make
    for store in stores:
        store.close()


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        [],
        True,
        {},
        {"catalogSync": None},
        {"catalogSync": []},
        {"catalogSync": True},
        {"catalogSync": {"recentLimit": 0}},
        {"catalogSync": {"recentLimit": 101}},
        {"catalogSync": {"recentLimit": True}},
        {"catalogSync": {"recentLimit": "50"}},
        {"catalogSync": {"intervalSeconds": 4}},
        {"catalogSync": {"intervalSeconds": 3601}},
        {"catalogSync": {"intervalSeconds": True}},
        {"catalogSync": {"intervalSeconds": "60"}},
    ],
)
def test_sync_requires_a_valid_negotiated_capability(capabilities):
    assert negotiated_settings({"capabilities": capabilities}) is None


def test_sync_defaults_and_configuration_bounds():
    assert negotiated_settings({"capabilities": {"catalogSync": {}}}) == SyncSettings(50, 60)
    assert negotiated_settings(
        {"capabilities": {"catalogSync": {"recentLimit": 1, "intervalSeconds": 5}}}
    ) == SyncSettings(1, 5)
    assert negotiated_settings(
        {"capabilities": {"catalogSync": {"recentLimit": 100, "intervalSeconds": 3600}}}
    ) == SyncSettings(100, 3600)


def test_first_catalog_then_only_changed_metadata_and_empty_success(sync_factory):
    async def scenario():
        a, b = metadata(), metadata("native-b")
        changed_a = metadata(updated=11, name="Renamed task", cwd="/new-project")
        sync, peer, store, wakes = sync_factory(
            [
                {"data": [a, b]},
                {"data": [b, a]},
                {"data": [changed_a, b]},
            ]
        )
        for index in range(3):
            assert await sync.scan_once()
            event = store.pending_events()[0]
            assert event["method"] == "anywhere/catalog_sync"
            params = event["params"]
            assert (
                "threadId" not in params and params["scanned_count"] == 2 and params["limit"] == 50
            )
            assert isinstance(params["syncedAt"], float) and params["syncedAt"] > 0
            assert all("turns" not in thread for thread in params["threads"])
            assert len(params["threads"]) == [2, 0, 1][index]
            if index == 2:
                assert params["threads"][0]["id"] == "native-a"
                assert params["threads"][0]["updatedAt"] == 11
            store.acknowledge(event["event_id"])
        assert len(wakes) == len(peer.requests) == 3
        assert "useStateDbOnly" not in peer.requests[0][1]
        assert peer.requests[1][1]["useStateDbOnly"] is True
        assert all(
            method == "thread/list" and params["archived"] is False
            for method, params in peer.requests
        )

    asyncio.run(scenario())


def test_discovery_is_bounded_and_never_emits_deletions(sync_factory):
    async def scenario():
        sync, peer, store, _ = sync_factory(
            [
                {"data": [metadata("a"), metadata("b"), metadata("c")]},
                {"data": [metadata("b")]},
            ],
            settings=SyncSettings(2, 60),
        )
        assert await sync.scan_once()
        event = store.pending_events()[0]
        assert len(event["params"]["threads"]) == event["params"]["scanned_count"] == 2
        store.acknowledge(event["event_id"])
        assert await sync.scan_once()
        assert store.pending_events()[0]["params"]["threads"] == []
        assert peer.requests[0][1]["limit"] == 2
        assert set(sync.fingerprints) == {"b"}  # Bounded comparison state, no delete event.

    asyncio.run(scenario())


def test_foreground_and_unacknowledged_events_apply_backpressure(sync_factory, monkeypatch):
    async def scenario():
        foreground = True
        sync, peer, store, _ = sync_factory([{"data": [metadata()]}], busy=lambda: foreground)
        assert not await sync.scan_once() and not peer.requests
        foreground = False
        backlog = [store.record_event("epoch", "delta", {}) for _ in range(100)]
        # Discovery's backpressure check must not deserialize large event bodies.
        with monkeypatch.context() as patch:
            patch.setattr(
                store, "pending_events", lambda **kwargs: pytest.fail("Read pending event bodies")
            )
            assert not await sync.scan_once() and not peer.requests
            store.acknowledge(backlog[0]["event_id"])
            assert await sync.scan_once()
            assert not await sync.scan_once() and len(peer.requests) == 1
            store.acknowledge(sync._pending_sync_event)
            assert await sync.scan_once() and len(peer.requests) == 2

    asyncio.run(scenario())


def test_backpressure_loop_waits_instead_of_spinning(sync_factory, monkeypatch):
    from codex_anywhere.node import sync as sync_module

    monkeypatch.setattr(sync_module, "BACKPRESSURE_DELAY", 0.01)

    async def scenario():
        sync, peer, store, _ = sync_factory([])
        for _ in range(100):
            store.record_event("epoch", "delta", {})
        checks = []
        twice = asyncio.Event()
        original = sync._backpressured

        def backpressured():
            checks.append(True)
            if len(checks) >= 2:
                twice.set()
            return original()

        monkeypatch.setattr(sync, "_backpressured", backpressured)
        task = asyncio.create_task(sync.run())
        try:
            # Windows timer coalescing can exceed a 36ms wall-clock window.
            # Wait for evidence of a second backed-off poll instead.
            await asyncio.wait_for(twice.wait(), timeout=2)
            assert 2 <= len(checks) <= 6 and not peer.requests
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_epoch_change_discards_snapshot_that_finished_after_restart(sync_factory):
    async def scenario():
        current = True
        sync, peer, store, _ = sync_factory([{"data": [metadata()]}], current=lambda: current)
        request = peer.request

        async def switch_epoch(method, params):
            nonlocal current
            result = await request(method, params)
            current = False
            return result

        peer.request = switch_epoch
        assert not await sync.scan_once()
        assert store.pending_events() == []

    asyncio.run(scenario())


def test_connection_negotiation_and_reconnect_reset_the_comparison(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "host_id": "host", "token": "test"}, tmp_path
        )
        peer = MetadataPeer([{"data": [metadata()]}, {"data": [metadata()]}])
        service.runtime = peer
        records = []
        connections = 0

        async def hub(ws):
            nonlocal connections
            connections += 1
            hello = json.loads(await ws.recv())
            assert {"catalogSync", "lazyHistory", "runtimeSettings", "projectInventory"} <= set(hello["capabilities"])
            welcome = {"type": "welcome", "host_id": "host", "protocol": 1}
            if connections > 1:
                welcome["capabilities"] = {
                    "catalogSync": {"recentLimit": 50, "intervalSeconds": 60}
                }
            await ws.send(json.dumps(welcome))
            if connections == 1:
                await asyncio.sleep(0.03)
                assert not peer.requests
                await ws.close()
                return
            async for raw in ws:
                event = json.loads(raw)
                if event.get("type") == "event" and event["method"] == "anywhere/catalog_sync":
                    records.append(event)
                    await ws.send(json.dumps({"type": "event_ack", "event_id": event["event_id"]}))
                    await ws.close()
                    return

        try:
            async with serve(hub, "127.0.0.1", 0) as server:
                service.server = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                for _ in range(3):
                    await asyncio.wait_for(service._connect_once(), 3)
            assert len(records) == len(peer.requests) == 2
            assert [event["params"]["threads"][0]["id"] for event in records] == [
                "native-a",
                "native-a",
            ]
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_sync_error_is_bounded_and_chat_commands_keep_working(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "host_id": "host", "token": "test"}, tmp_path
        )
        peer = MetadataPeer([RpcError("temporary catalog failure " * 300)])
        service.runtime = peer
        errors, results = [], []

        async def hub(ws):
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "host_id": "host",
                        "protocol": 1,
                        "capabilities": {"catalogSync": {}},
                    }
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if (
                    message.get("type") == "event"
                    and message["method"] == "anywhere/catalog_sync_error"
                ):
                    errors.append(message)
                    await ws.send(
                        json.dumps({"type": "event_ack", "event_id": message["event_id"]})
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "command",
                                "command": {
                                    "id": "browse-after-error",
                                    "kind": "directory.list",
                                    "payload": {"path": str(tmp_path)},
                                },
                            }
                        )
                    )
                elif message.get("type") == "command_result":
                    results.append(message)
                    await ws.close()
                    return

        try:
            async with serve(hub, "127.0.0.1", 0) as server:
                service.server = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                await asyncio.wait_for(service._connect_once(), 3)
            assert len(errors) == 1 and len(errors[0]["params"]["error"]) == 2000
            assert "threadId" not in errors[0]["params"]
            assert results[0]["result"]["path"] == str(tmp_path)
        finally:
            await asyncio.gather(*service.command_tasks, return_exceptions=True)
            service.store.close()

    asyncio.run(scenario())


def test_connection_cancellation_cleans_up_pending_discovery_rpc(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "host_id": "host", "token": "test"}, tmp_path
        )
        began, cancelled = asyncio.Event(), asyncio.Event()
        peer = MetadataPeer([])

        async def pending_request(method, params):
            assert method == "thread/list"
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        peer.request = pending_request
        service.runtime = peer

        async def hub(ws):
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "host_id": "host",
                        "protocol": 1,
                        "capabilities": {"catalogSync": {}},
                    }
                )
            )
            await began.wait()
            await ws.close()

        try:
            async with serve(hub, "127.0.0.1", 0) as server:
                service.server = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                await asyncio.wait_for(service._connect_once(), 3)
            assert began.is_set() and cancelled.is_set()
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_pending_discovery_does_not_block_foreground_rpc_and_disconnect_cleans_future(tmp_path):
    script = tmp_path / "metadata_peer.py"
    script.write_text(
        """
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        emit({"id": message["id"], "result": {"userAgent": "metadata-only-test-peer"}})
    elif method == "thread/list":
        # Keep this read pending while continuing to accept foreground RPCs.
        emit({"method": "test/catalog_pending", "params": {}})
    elif method == "thread/start":
        emit({"id": message["id"], "result": {"thread": {"id": "foreground-native", "cwd": message["params"]["cwd"]}}})
""",
        encoding="utf-8",
    )

    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "host_id": "host", "token": "test"}, tmp_path
        )
        runtime = CodexRuntime(
            [sys.executable, "-u", str(script)],
            service._native_event,
            service._runtime_exit,
            timeout=3,
        )
        service.runtime = runtime
        await runtime.start()
        results = []

        async def hub(ws):
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "host_id": "host",
                        "protocol": 1,
                        "capabilities": {"catalogSync": {}},
                    }
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if message.get("method") == "test/catalog_pending":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "command",
                                "command": {
                                    "id": "foreground",
                                    "kind": "thread.start",
                                    "payload": {"cwd": str(tmp_path)},
                                },
                            }
                        )
                    )
                elif message.get("type") == "command_result":
                    results.append(message)
                    await ws.close()
                    return

        try:
            async with serve(hub, "127.0.0.1", 0) as server:
                service.server = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                await asyncio.wait_for(service._connect_once(), 3)
            assert results[0]["result"]["thread"]["id"] == "foreground-native"
            assert not runtime.pending
            assert runtime.process.returncode is None
        finally:
            await asyncio.gather(*service.command_tasks, return_exceptions=True)
            await runtime.close()
            service.store.close()

    asyncio.run(scenario())
