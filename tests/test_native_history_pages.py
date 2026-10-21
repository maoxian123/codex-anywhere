import asyncio
import copy
import json
from uuid import uuid4

import pytest

from codex_anywhere.node import history
from codex_anywhere.node.runtime import RpcError
from codex_anywhere.node.store import NodeStore


class NativePages:
    runtime_id = "native-history-runtime"

    def __init__(self, directory, count=1000):
        self.thread = {
            "id": "native-history",
            "cwd": str(directory),
            "updatedAt": 10,
            "name": "Local",
        }
        self.items = [
            {
                "turnId": f"turn-{index}",
                "item": {"id": f"item-{index}", "type": "futureTool", "raw": {"index": index}},
                "startedAtMs": index * 1000,
            }
            for index in range(count, 0, -1)
        ]
        self.calls = []
        self.failure = None

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {"thread": copy.deepcopy(self.thread)}
        assert method == "thread/items/list"
        assert params["sortDirection"] == "desc" and params["limit"] == 1
        assert "turnId" not in params
        if self.failure:
            raise self.failure
        offset = int(params.get("cursor", "0"))
        page = self.items[offset : offset + 1]
        return {
            "data": copy.deepcopy(page),
            "nextCursor": str(offset + 1) if offset + 1 < len(self.items) else None,
        }


def command(**payload):
    return {
        "id": uuid4().hex,
        "kind": "thread.history",
        "payload": {"native_thread_id": "native-history", **payload},
    }


def run_page(peer, store, **payload):
    return asyncio.run(history.load_history_page(peer, store, command(**payload), lambda: None))


def events(store, method):
    return [event for event in store.pending_events(1000) if event["method"] == method]


def test_many_turns_load_only_requested_native_tail_and_older_page(tmp_path):
    peer = NativePages(tmp_path, count=2000)
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        first = run_page(peer, store, limit=3)
        assert first["itemCount"] == 3 and first["hasMore"] is True
        assert len(peer.calls) == 5
        first_items = events(store, "anywhere/history_item")
        assert [event["params"]["item"] for event in first_items] == [
            entry["item"] for entry in peer.items[:3]
        ]
        assert first_items[0]["params"]["createdAt"] == 2000
        assert store.thread_cwd("native-history") == str(tmp_path)
        second = run_page(peer, store, cursor=first["nextCursor"], limit=2)
        assert second["generation"] == first["generation"]
        assert [
            event["params"]["item"]["id"] for event in events(store, "anywhere/history_item")
        ] == [f"item-{index}" for index in range(2000, 1995, -1)]
        assert len(store.asset_jobs()) == 5
    finally:
        store.close()


def test_byte_budget_defers_candidate_without_skipping_it(tmp_path, monkeypatch):
    peer = NativePages(tmp_path, count=3)
    size = len(json.dumps(peer.items[0]["item"]).encode())
    monkeypatch.setattr(history, "HISTORY_PAGE_MAX_BYTES", size * 2 - 1)
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        first = run_page(peer, store, limit=3)
        assert first["itemCount"] == 1
        second = run_page(peer, store, cursor=first["nextCursor"], limit=3)
        assert second["itemCount"] == 1
        assert [
            event["params"]["item"]["id"] for event in events(store, "anywhere/history_item")
        ] == ["item-3", "item-2"]
        assert first["byteCount"] <= history.HISTORY_PAGE_MAX_BYTES
    finally:
        store.close()


def test_oversized_single_item_is_explicit_failure(tmp_path, monkeypatch):
    peer = NativePages(tmp_path, count=1)
    monkeypatch.setattr(history, "HISTORY_PAGE_MAX_BYTES", 8)
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        with pytest.raises(ValueError, match="byte budget"):
            run_page(peer, store)
        assert events(store, "anywhere/history_item") == []
        assert events(store, "anywhere/history_complete") == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "error", [RpcError("Method not found", code=-32601), RpcError("Permission denied", code=-32000)]
)
def test_unsupported_or_failed_lazy_read_never_hydrates_full_history(tmp_path, error):
    peer = NativePages(tmp_path)
    peer.failure = error
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        with pytest.raises((ValueError, RpcError)):
            run_page(peer, store)
        assert [method for method, _ in peer.calls] == ["thread/read", "thread/items/list"]
        assert events(store, "anywhere/history_complete") == []
    finally:
        store.close()


def test_snapshot_change_during_page_never_publishes_cursor(tmp_path):
    peer = NativePages(tmp_path, count=2)
    original = peer.request

    async def changed(method, params):
        response = await original(method, params)
        if method == "thread/items/list":
            peer.thread["updatedAt"] = 11
        return response

    peer.request = changed
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        with pytest.raises(ValueError, match="changed while"):
            run_page(peer, store, limit=1)
        assert len(events(store, "anywhere/history_item")) == 1
        assert events(store, "anywhere/history_complete") == []
    finally:
        store.close()


def test_same_second_rollout_change_invalidates_older_cursor(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("first", encoding="utf-8")
    peer = NativePages(tmp_path, count=3)
    peer.thread["path"] = str(rollout)
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        first = run_page(peer, store, limit=1)
        rollout.write_text("a longer same-second change", encoding="utf-8")
        calls = len(peer.calls)
        with pytest.raises(ValueError, match="changed since"):
            run_page(peer, store, cursor=first["nextCursor"])
        assert len(peer.calls) == calls + 1
    finally:
        store.close()


def test_complete_checkpoint_survives_item_acks_and_node_reopen(tmp_path):
    path = tmp_path / "state.sqlite"
    peer = NativePages(tmp_path, count=2)
    store = NodeStore(path)
    first = run_page(peer, store, limit=1)
    for event in store.pending_events():
        if event["method"] != "anywhere/history_complete":
            store.acknowledge(event["event_id"])
    store.close()
    store = NodeStore(path)
    try:
        pending = store.pending_events()
        assert len(pending) == 1
        assert pending[0]["params"]["nextCursor"] == first["nextCursor"]
        second = run_page(peer, store, cursor=first["nextCursor"])
        assert second["hasMore"] is False and second["nextCursor"] is None
        assert events(store, "anywhere/history_item")[0]["params"]["item"]["id"] == "item-1"
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"cursor": "bad"}]
)
def test_lazy_page_validates_limits_and_cursor_before_native_reads(tmp_path, payload):
    peer = NativePages(tmp_path)
    store = NodeStore(tmp_path / "state.sqlite")
    try:
        with pytest.raises(ValueError):
            run_page(peer, store, **payload)
        assert peer.calls == []
    finally:
        store.close()
