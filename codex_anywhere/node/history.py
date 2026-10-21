"""Explicitly requested, read-only local Codex catalog and history import."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .config import resolve_path
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
