"""Explicitly requested, read-only local Codex catalog and history import."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Callable

from .config import resolve_path
from .fingerprints import history_fingerprint
from .runtime import CodexRuntime, RpcError
from .store import NodeStore

SOURCE_KINDS = [
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
]

HISTORY_PAGE_LIMIT = 50
HISTORY_PAGE_MAX_ITEMS = 100
HISTORY_PAGE_MAX_BYTES = 28 * 1024 * 1024
HISTORY_CURSOR_MAX_BYTES = 8192


def _snapshot_version(thread: dict) -> float:
    version = thread.get("updatedAt")
    if (
        isinstance(version, bool)
        or not isinstance(version, (int, float))
        or not math.isfinite(version)
    ):
        raise ValueError("Codex history metadata has no usable snapshot version")
    return version


def _history_cursor(state: dict) -> str:
    value = (
        base64.urlsafe_b64encode(
            json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode()
        )
        .decode()
        .rstrip("=")
    )
    if len(value) > HISTORY_CURSOR_MAX_BYTES:
        raise ValueError("Codex returned an oversized history cursor")
    return value


def _read_history_cursor(value: str, thread_id: str) -> dict:
    if not isinstance(value, str) or not value or len(value) > HISTORY_CURSOR_MAX_BYTES:
        raise ValueError("Invalid history cursor")
    try:
        state = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid history cursor") from error
    if (
        not isinstance(state, dict)
        or state.get("v") != 1
        or state.get("threadId") != thread_id
        or not isinstance(state.get("generation"), str)
        or len(state["generation"]) != 64
        or not isinstance(state.get("nativeCursor"), str)
        or not state["nativeCursor"]
        or not isinstance(state.get("anchorItemId"), str)
        or not state["anchorItemId"]
    ):
        raise ValueError("History cursor belongs to a different or invalid snapshot")
    return state


async def load_history_page(
    runtime: CodexRuntime, store: NodeStore, command: dict, wake: Callable[[], None]
) -> dict:
    """Read one finite native tail/older page; never resume or hydrate full history."""
    payload = command.get("payload", {})
    thread_id = payload.get("native_thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("native_thread_id is required")
    limit = payload.get("limit", HISTORY_PAGE_LIMIT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= HISTORY_PAGE_MAX_ITEMS
    ):
        raise ValueError(f"History limit must be between 1 and {HISTORY_PAGE_MAX_ITEMS}")
    state = (
        _read_history_cursor(payload["cursor"], thread_id)
        if payload.get("cursor") is not None
        else None
    )
    response = await runtime.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = _metadata(response["thread"])
    if thread.get("id") != thread_id:
        raise ValueError("Codex returned a different thread identity")
    version = _snapshot_version(thread)
    fingerprint = history_fingerprint(thread)
    thread["_history_fingerprint"] = fingerprint
    if state and (
        state.get("snapshotUpdatedAt") != version or state.get("snapshotFingerprint") != fingerprint
    ):
        raise ValueError("History changed since this cursor was created; refresh the latest page")
    generation = (
        state["generation"]
        if state
        else hashlib.sha256(
            json.dumps([thread_id, version, command["id"]], separators=(",", ":")).encode()
        ).hexdigest()
    )
    thread["cwd"] = str(resolve_path(thread.get("cwd"), directory=True, must_exist=False))
    store.save_thread(thread_id, thread["cwd"])

    def emit(method, params):
        store.record_event(
            runtime.runtime_id,
            method,
            {"threadId": thread_id, "commandId": command["id"], "generation": generation, **params},
            asset_job=method == "anywhere/history_item",
        )
        wake()

    emit("anywhere/history_metadata", {"thread": thread, "snapshotUpdatedAt": version})
    cursor = state["nativeCursor"] if state else None
    anchor_item_id = state["anchorItemId"] if state else None
    item_count, byte_count, seen_cursors, seen_items = 0, 0, set(), set()
    while item_count < limit:
        params = {"threadId": thread_id, "limit": 1, "sortDirection": "desc"}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            page = await runtime.request("thread/items/list", params)
        except RpcError as error:
            if _pagination_unsupported(error):
                raise ValueError(
                    "This Codex version does not support bounded history pages; use explicit full import"
                ) from error
            raise
        entries = page.get("data")
        if not isinstance(entries, list) or len(entries) > 1:
            raise ValueError("Codex returned an invalid bounded history page")
        next_cursor = page.get("nextCursor")
        if next_cursor is not None and (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor == cursor
            or next_cursor in seen_cursors
        ):
            raise ValueError("Codex returned a repeated or invalid history cursor")
        if not entries:
            if next_cursor is not None:
                raise ValueError("Codex returned an empty history page with a continuation cursor")
            cursor = None
            break
        entry = entries[0]
        item = entry.get("item") if isinstance(entry, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(entry.get("turnId"), str)
            or not entry["turnId"]
            or item["id"] in seen_items
        ):
            raise ValueError("Codex returned an invalid or repeated history item")
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if size > HISTORY_PAGE_MAX_BYTES:
            raise ValueError(
                "One native history item exceeds the bounded page byte budget; use explicit full import"
            )
        if byte_count + size > HISTORY_PAGE_MAX_BYTES:
            # Leave the cursor before this un-emitted item. It is the first item
            # of the next request, including after reconnect or an ACK loss.
            break
        item_params = {"item": item, "turnId": entry["turnId"], "index": item_count}
        timestamp = entry.get("startedAtMs")
        if (
            isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
            and math.isfinite(timestamp)
        ):
            item_params["createdAt"] = timestamp / 1000
        emit("anywhere/history_item", item_params)
        item_count += 1
        byte_count += size
        seen_items.add(item["id"])
        anchor_item_id = item["id"]
        cursor = next_cursor
        if cursor is None:
            break
        seen_cursors.add(cursor)
        await asyncio.sleep(0)
    # The cursor is only published by a complete event, after verifying that the
    # native snapshot did not change while individual items were being read.
    check = await runtime.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    if (
        check.get("thread", {}).get("id") != thread_id
        or _snapshot_version(check["thread"]) != version
        or history_fingerprint(check["thread"]) != fingerprint
    ):
        raise ValueError("History changed while loading this page; refresh the latest page")
    next_token = (
        _history_cursor(
            {
                "v": 1,
                "threadId": thread_id,
                "generation": generation,
                "snapshotUpdatedAt": version,
                "snapshotFingerprint": fingerprint,
                "nativeCursor": cursor,
                "anchorItemId": anchor_item_id,
            }
        )
        if cursor is not None
        else None
    )
    result = {
        "generation": generation,
        "snapshotUpdatedAt": version,
        "itemCount": item_count,
        "byteCount": byte_count,
        "nextCursor": next_token,
        "hasMore": next_token is not None,
    }
    emit("anywhere/history_complete", result)
    return result


def _metadata(thread: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    result = {key: value for key, value in thread.items() if key != "turns"}
    result["preview"] = result.get("preview") or ""
    result["name"] = result.get("name")
    result["createdAt"] = result.get("createdAt", 0)
    result["updatedAt"] = result.get("updatedAt", result["createdAt"])
    result["title"] = (
        payload.get("title")
        or result.get("name")
        or result["preview"][:120]
        or "Imported Codex task"
    )
    return result


async def list_threads(runtime: CodexRuntime, payload: dict) -> dict:
    limit = payload.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Catalog limit must be between 1 and 100")
    archived = payload.get("archived", False)
    if not isinstance(archived, bool):
        raise ValueError("archived must be a boolean")
    params = {
        "limit": limit,
        "archived": archived,
        "sourceKinds": list(SOURCE_KINDS),
        "modelProviders": [],
        "sortKey": "updated_at",
        "sortDirection": "desc",
    }
    for source, target in (("cursor", "cursor"), ("search", "searchTerm")):
        value = payload.get(source)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{source} must be text")
            params[target] = value
    if payload.get("cwd"):
        params["cwd"] = str(resolve_path(payload["cwd"], directory=True, must_exist=False))
    if payload.get("indexed") is True:
        params["useStateDbOnly"] = True
    try:
        result = await runtime.request("thread/list", params)
    except RpcError as error:
        # Old peers may reject the optional optimization. Do not reinterpret
        # ordinary storage/auth failures as permission to scan more history.
        if "useStateDbOnly" not in params or not (
            error.code == -32602 and "usestatedbonly" in str(error).lower()
        ):
            raise
        params.pop("useStateDbOnly")
        result = await runtime.request("thread/list", params)
    return {**result, "data": [_metadata(thread) for thread in result.get("data", [])]}


def _pagination_unsupported(error: RpcError) -> bool:
    if error.code == -32601:
        return True
    message = str(error).lower()
    return (
        any(
            term in message
            for term in (
                "thread/turns/list",
                "thread/items/list",
                "pagination",
                "paginated",
                "itemsview",
                "items view",
                "notloaded",
            )
        )
    ) and any(
        term in message
        for term in (
            "unsupported",
            "not supported",
            "unknown method",
            "method not found",
            "not implemented",
            "only supported",
            "only available",
            "requires paginated",
            "unknown variant",
            "unknown field",
        )
    )


async def import_thread(
    runtime: CodexRuntime, store: NodeStore, command: dict, wake: Callable[[], None]
) -> dict:
    payload = command.get("payload", {})
    thread_id = payload["native_thread_id"]
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("native_thread_id is required")
    response = await runtime.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = _metadata(response["thread"], payload)
    if thread.get("id") != thread_id:
        raise ValueError("Codex returned a different thread identity")
    cwd = thread.get("cwd") or payload.get("cwd")
    thread["cwd"] = str(resolve_path(cwd, directory=True, must_exist=False))
    store.save_thread(thread_id, thread["cwd"])

    def emit(method: str, params: dict) -> None:
        store.record_event(
            runtime.runtime_id,
            method,
            {"threadId": thread_id, "commandId": command["id"], **params},
            asset_job=method == "anywhere/import_item",
        )
        wake()

    emit("anywhere/import_metadata", {"thread": thread})
    position = 0

    async def emit_item(turn: dict, item: dict, wrapper: dict | None = None) -> None:
        nonlocal position
        if not isinstance(item, dict):
            raise ValueError("Codex returned an invalid history item")
        position += 1
        params = {"turnId": turn.get("id"), "item": item, "position": position}
        timestamp = turn.get("startedAt")
        if timestamp is None:
            timestamp = turn.get("createdAt")
        if timestamp is None and wrapper is not None:
            milliseconds = wrapper.get("startedAtMs")
            if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool):
                timestamp = milliseconds / 1000
        if timestamp is not None:
            params["createdAt"] = timestamp
        emit("anywhere/import_item", params)
        if position % 16 == 0:
            await asyncio.sleep(0)

    async def emit_turns(turns: list) -> None:
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("Codex returned invalid turn metadata")
            for item in turn.get("items", []):
                await emit_item(turn, item)

    def next_cursor(page: dict, seen: set) -> str | None:
        cursor = page.get("nextCursor")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("Codex returned a repeated or invalid history cursor")
        seen.add(cursor)
        return cursor

    def page_data(page: dict) -> list:
        data = page.get("data")
        if not isinstance(data, list):
            raise ValueError("Codex returned an invalid history page")
        return data

    async def legacy_read() -> None:
        legacy = await runtime.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        await emit_turns(legacy["thread"].get("turns", []))

    async def full_turn_pages() -> None:
        cursor, seen = None, set()
        while True:
            params = {
                "threadId": thread_id,
                "limit": 1,
                "itemsView": "full",
                "sortDirection": "asc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            try:
                page = await runtime.request("thread/turns/list", params)
            except RpcError as error:
                if position or not _pagination_unsupported(error):
                    raise
                await legacy_read()
                return
            await emit_turns(page_data(page))
            cursor = next_cursor(page, seen)
            if cursor is None:
                return

    # Metadata pages cannot aggregate image-bearing turns. Retrieve each turn's
    # items separately, one per RPC, so large images do not share one JSONL reply.
    cursor, seen = None, set()
    phase = "turns"
    try:
        while True:
            params = {
                "threadId": thread_id,
                "limit": 50,
                "itemsView": "notLoaded",
                "sortDirection": "asc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            phase = "turns"
            page = await runtime.request("thread/turns/list", params)
            for turn in page_data(page):
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise ValueError("Codex returned invalid turn metadata")
                item_cursor, item_seen = None, set()
                while True:
                    item_params = {
                        "threadId": thread_id,
                        "turnId": turn["id"],
                        "limit": 1,
                        "sortDirection": "asc",
                    }
                    if item_cursor is not None:
                        item_params["cursor"] = item_cursor
                    phase = "items"
                    items_page = await runtime.request("thread/items/list", item_params)
                    for wrapper in page_data(items_page):
                        if (
                            not isinstance(wrapper, dict)
                            or wrapper.get("turnId", turn["id"]) != turn["id"]
                        ):
                            raise ValueError("Codex returned an item from a different turn")
                        await emit_item(turn, wrapper.get("item"), wrapper)
                    item_cursor = next_cursor(items_page, item_seen)
                    if item_cursor is None:
                        break
            cursor = next_cursor(page, seen)
            if cursor is None:
                break
    except RpcError as error:
        if position or not _pagination_unsupported(error):
            raise
        if phase == "items" or "notloaded" in str(error).lower():
            await full_turn_pages()
        else:
            await legacy_read()
    emit("anywhere/import_complete", {"itemCount": position})
    return {"thread": thread, "itemCount": position}
