import asyncio
import copy
import json

import pytest

from codex_anywhere.node.history import import_thread
from codex_anywhere.node.runtime import RpcError


class MemoryStore:
    def __init__(self):
        self.events = []
        self.threads = {}

    def save_thread(self, thread_id, cwd):
        self.threads[thread_id] = cwd

    def record_event(self, runtime_id, method, params, *, asset_job=False):
        self.events.append({"method": method, "params": params, "asset_job": asset_job})


class HistoryPeer:
    runtime_id = "history-test-epoch"

    def __init__(self, cwd, handler):
        self.cwd = str(cwd)
        self.handler = handler
        self.calls = []
        self.reply_sizes = []

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/read" and not params["includeTurns"]:
            return {"thread": {"id": "native", "cwd": self.cwd, "createdAt": 1}}
        result = self.handler(method, params)
        size = len(json.dumps(result).encode("utf-8"))
        assert size < 32 * 1024 * 1024, "A history response exceeded the JSONL reader limit"
        self.reply_sizes.append(size)
        return result


def run_import(peer, store):
    return asyncio.run(
        import_thread(
            peer,
            store,
            {"id": "import-command", "payload": {"native_thread_id": "native"}},
            lambda: None,
        )
    )


def imported_items(store):
    return [event["params"] for event in store.events if event["method"] == "anywhere/import_item"]


def test_metadata_and_item_pages_preserve_order_cursors_and_turn_timestamps(tmp_path):
    opaque = {"id": "opaque", "type": "futureNativeItem", "payload": {"preserve": [1, True]}}

    def handler(method, params):
        if method == "thread/turns/list":
            assert params["itemsView"] == "notLoaded" and params["limit"] == 50
            if params.get("cursor") is None:
                return {
                    "data": [{"id": "turn-1", "startedAt": 10, "items": []}],
                    "nextCursor": "turn-next",
                }
            assert params["cursor"] == "turn-next"
            return {"data": [{"id": "turn-2", "items": []}], "nextCursor": None}
        assert method == "thread/items/list" and params["limit"] == 1
        assert params["sortDirection"] == "asc"
        if params["turnId"] == "turn-1" and params.get("cursor") is None:
            return {
                "data": [
                    {
                        "turnId": "turn-1",
                        "item": {"id": "first", "type": "agentMessage", "text": "First"},
                        "startedAtMs": 999000,
                    }
                ],
                "nextCursor": "item-next",
            }
        if params["turnId"] == "turn-1":
            assert params["cursor"] == "item-next"
            return {
                "data": [{"turnId": "turn-1", "item": opaque, "startedAtMs": 999000}],
                "nextCursor": None,
            }
        assert params["turnId"] == "turn-2" and params.get("cursor") is None
        return {
            "data": [
                {
                    "turnId": "turn-2",
                    "item": {"id": "last", "type": "agentMessage", "text": "Last"},
                    "startedAtMs": 22000,
                    "completedAtMs": 23000,
                }
            ],
            "nextCursor": None,
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    result = run_import(peer, store)
    items = imported_items(store)
    assert result["itemCount"] == 3
    assert [item["position"] for item in items] == [1, 2, 3]
    assert [item["createdAt"] for item in items] == [10, 10, 22]
    assert [item["turnId"] for item in items] == ["turn-1", "turn-1", "turn-2"]
    assert items[1]["item"] == opaque
    assert all(
        event["asset_job"] for event in store.events if event["method"] == "anywhere/import_item"
    )
    assert store.events[-1]["method"] == "anywhere/import_complete"
    assert not any(method in {"thread/resume", "turn/start"} for method, _ in peer.calls)


def test_multiple_image_turns_never_share_a_32_mib_response(tmp_path):
    url = "data:image/png;base64," + "A" * (17 * 1024 * 1024)

    def handler(method, params):
        if method == "thread/turns/list":
            assert params["itemsView"] == "notLoaded"
            return {
                "data": [
                    {"id": "turn-1", "startedAt": 1, "items": []},
                    {"id": "turn-2", "startedAt": 2, "items": []},
                ],
                "nextCursor": None,
            }
        assert method == "thread/items/list" and params["limit"] == 1
        return {
            "data": [
                {
                    "turnId": params["turnId"],
                    "item": {
                        "id": params["turnId"] + "-image",
                        "type": "userMessage",
                        "content": [{"type": "image", "url": url}],
                    },
                }
            ],
            "nextCursor": None,
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    assert run_import(peer, store)["itemCount"] == 2
    assert sum(peer.reply_sizes) > 32 * 1024 * 1024
    assert max(peer.reply_sizes) < 32 * 1024 * 1024
    assert all(item["item"]["content"][0]["url"] == url for item in imported_items(store))


def test_unsupported_item_pagination_falls_back_to_one_full_turn_per_page(tmp_path):
    def handler(method, params):
        if method == "thread/items/list":
            raise RpcError("Unknown method thread/items/list", code=-32601)
        assert method == "thread/turns/list"
        if params["itemsView"] == "notLoaded":
            return {"data": [{"id": "turn-1", "items": []}], "nextCursor": None}
        assert params["limit"] == 1 and params["sortDirection"] == "asc"
        second = params.get("cursor") == "next"
        return {
            "data": [
                {
                    "id": "turn-2" if second else "turn-1",
                    "startedAt": 12 if second else 11,
                    "items": [{"id": "second" if second else "first", "type": "agentMessage"}],
                }
            ],
            "nextCursor": None if second else "next",
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    assert run_import(peer, store)["itemCount"] == 2
    assert [item["item"]["id"] for item in imported_items(store)] == ["first", "second"]
    assert [item["position"] for item in imported_items(store)] == [1, 2]
    assert not any(
        method == "thread/read" and params["includeTurns"] for method, params in peer.calls
    )


def test_unsupported_turn_pagination_uses_legacy_read_before_emitting_items(tmp_path):
    def handler(method, params):
        if method == "thread/turns/list":
            raise RpcError("Method not found", code=-32601)
        assert method == "thread/read" and params["includeTurns"] is True
        return {
            "thread": {
                "turns": [
                    {
                        "id": "legacy",
                        "startedAt": 11,
                        "items": [{"id": "item", "type": "agentMessage"}],
                    }
                ]
            }
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    assert run_import(peer, store)["itemCount"] == 1
    assert len(imported_items(store)) == 1
    assert [method for method, _ in peer.calls] == [
        "thread/read",
        "thread/turns/list",
        "thread/read",
    ]


def test_not_loaded_view_rejection_uses_full_turn_paging_instead_of_legacy_read(tmp_path):
    def handler(method, params):
        assert method == "thread/turns/list"
        if params["itemsView"] == "notLoaded":
            raise RpcError("unknown variant notLoaded for itemsView", code=-32602)
        assert params["itemsView"] == "full" and params["limit"] == 1
        return {
            "data": [{"id": "turn", "items": [{"id": "item", "type": "agentMessage"}]}],
            "nextCursor": None,
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    assert run_import(peer, store)["itemCount"] == 1
    assert [method for method, _ in peer.calls] == [
        "thread/read",
        "thread/turns/list",
        "thread/turns/list",
    ]


@pytest.mark.parametrize(
    "message,code", [("Unknown method thread/items/list", -32601), ("Permission denied", -32000)]
)
def test_failure_after_an_emitted_item_never_restarts_or_signals_completion(
    tmp_path, message, code
):
    def handler(method, params):
        if method == "thread/turns/list":
            return {"data": [{"id": "turn", "items": []}], "nextCursor": None}
        assert method == "thread/items/list"
        if params.get("cursor") is None:
            return {
                "data": [{"turnId": "turn", "item": {"id": "first", "type": "agentMessage"}}],
                "nextCursor": "next",
            }
        raise RpcError(message, code=code)

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    with pytest.raises(RpcError):
        run_import(peer, store)
    assert len(imported_items(store)) == 1
    assert not any(event["method"] == "anywhere/import_complete" for event in store.events)
    assert not any(
        method == "thread/read" and params["includeTurns"] for method, params in peer.calls
    )
    assert not any(params.get("itemsView") == "full" for _, params in peer.calls)


def test_permission_error_before_any_item_is_not_treated_as_unsupported(tmp_path):
    def handler(method, params):
        if method == "thread/turns/list":
            return {"data": [{"id": "turn", "items": []}], "nextCursor": None}
        raise RpcError("Permission denied", code=-32000)

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    with pytest.raises(RpcError, match="Permission denied"):
        run_import(peer, store)
    assert not imported_items(store)
    assert len(peer.calls) == 3


def test_repeated_item_cursor_ends_import_without_fallback(tmp_path):
    def handler(method, params):
        if method == "thread/turns/list":
            return {"data": [{"id": "turn", "items": []}], "nextCursor": None}
        return {
            "data": [{"turnId": "turn", "item": {"id": "item", "type": "agentMessage"}}],
            "nextCursor": "repeated",
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    with pytest.raises(ValueError, match="repeated"):
        run_import(peer, store)
    assert not any(event["method"] == "anywhere/import_complete" for event in store.events)
    assert not any(params.get("itemsView") == "full" for _, params in peer.calls)


def test_full_turn_fallback_error_after_first_item_does_not_retry_legacy_read(tmp_path):
    def handler(method, params):
        if method == "thread/items/list":
            raise RpcError("Unknown method thread/items/list", code=-32601)
        assert method == "thread/turns/list"
        if params["itemsView"] == "notLoaded":
            return {"data": [{"id": "turn", "items": []}], "nextCursor": None}
        if params.get("cursor") is None:
            return {
                "data": [{"id": "turn", "items": [{"id": "first", "type": "agentMessage"}]}],
                "nextCursor": "next",
            }
        raise RpcError("thread/turns/list pagination unsupported", code=-32601)

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    with pytest.raises(RpcError):
        run_import(peer, store)
    assert len(imported_items(store)) == 1
    assert not any(event["method"] == "anywhere/import_complete" for event in store.events)
    assert not any(
        method == "thread/read" and params["includeTurns"] for method, params in peer.calls
    )


def test_both_pagination_apis_unavailable_before_items_can_use_legacy_read(tmp_path):
    def handler(method, params):
        if method == "thread/items/list":
            raise RpcError("Unknown method thread/items/list", code=-32601)
        if method == "thread/turns/list":
            if params["itemsView"] == "notLoaded":
                return {"data": [{"id": "turn", "items": []}], "nextCursor": None}
            raise RpcError("Full turn pagination is not supported", code=-32601)
        assert method == "thread/read" and params["includeTurns"]
        return {
            "thread": {
                "turns": [
                    {
                        "id": "legacy",
                        "startedAt": 10,
                        "items": [{"id": "legacy-item", "type": "agentMessage"}],
                    }
                ]
            }
        }

    peer, store = HistoryPeer(tmp_path, handler), MemoryStore()
    assert run_import(peer, store)["itemCount"] == 1
    assert [item["item"]["id"] for item in imported_items(store)] == ["legacy-item"]
    assert (
        len([event for event in store.events if event["method"] == "anywhere/import_metadata"]) == 1
    )
