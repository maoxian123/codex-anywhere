"""Atomic projection of finite native history pages from the durable event log."""

import base64
import json
import math
import time

from .models import Command, Event, Host, Item, db
from .projects import ensure_project

INITIAL_POSITION = 2**40
TAIL_GAP = 2**30
MAX_POSITION = 2**53 - 1
PAGE_METHODS = {"anywhere/history_metadata", "anywhere/history_item", "anywhere/history_complete"}


def history_command_applied(command):
    return isinstance(command.result, dict) and command.result.get("historyProjected") is True


def _command(thread, params, live):
    if not live or not isinstance(params, dict):
        return None
    command_id = params.get("commandId")
    if not isinstance(command_id, str):
        return None
    command = db.session.get(Command, command_id)
    host = db.session.get(Host, thread.host_id)
    if (
        command is None
        or host is None
        or command.kind != "thread.history"
        or command.thread_id != thread.id
        or command.host_id != thread.host_id
        or command.payload.get("native_thread_id") != thread.native_id
        or command.runtime_id != host.runtime_id
        or command.status == "failed"
        or thread.status in {"active", "waiting", "starting"}
    ):
        return None
    latest = (
        Command.query.filter(
            Command.thread_id == thread.id,
            Command.kind.in_(["thread.history", "thread.import"]),
        )
        .order_by(Command.created_at.desc(), Command.id.desc())
        .first()
    )
    if latest.id != command.id:
        return None
    if Command.query.filter(
        Command.thread_id == thread.id,
        Command.kind.in_(["thread.start", "turn.start", "turn.steer"]),
        Command.created_at >= command.created_at,
    ).first():
        return None
    generation = params.get("generation")
    if (
        not isinstance(generation, str)
        or len(generation) != 64
        or any(char not in "0123456789abcdef" for char in generation)
    ):
        return None
    if command.payload.get("cursor") and (
        command.payload["cursor"] != thread.history_cursor
        or generation != thread.history_generation
    ):
        return None
    return command


def _events(thread, command, method, generation):
    return (
        Event.query.filter(
            Event.thread_id == thread.id,
            Event.method == method,
            Event.runtime_id == command.runtime_id,
            Event.params["commandId"].as_string() == command.id,
            Event.params["generation"].as_string() == generation,
        )
        .order_by(Event.id)
        .all()
    )


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _anchor_position(thread, token):
    try:
        state = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        anchor_id = state["anchorItemId"]
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Invalid native history page anchor") from error
    anchor = Item.query.filter_by(thread_id=thread.id, native_id=anchor_id).first()
    if anchor is None:
        raise ValueError("Native history page anchor is no longer cached; refresh the latest page")
    return anchor.position


def _validate_next_cursor(token, thread, generation, version, fingerprint, anchor_id):
    if token is None:
        return
    try:
        state = json.loads(
            base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid native history continuation cursor") from error
    if (
        not isinstance(state, dict)
        or state.get("v") != 1
        or state.get("threadId") != thread.native_id
        or state.get("generation") != generation
        or state.get("snapshotUpdatedAt") != version
        or state.get("snapshotFingerprint") != fingerprint
        or state.get("anchorItemId") != anchor_id
        or not isinstance(state.get("nativeCursor"), str)
        or not state["nativeCursor"]
    ):
        raise ValueError("Native history cursor does not match the completed snapshot")


def _positions(thread, entries, cursor):
    """Reconcile covered slots to native order; leave uncovered positions intact."""
    ids = [entry["item"]["id"] for entry in entries]
    known = {
        item.native_id: item.position
        for item in Item.query.filter(Item.thread_id == thread.id, Item.native_id.in_(ids)).all()
    }
    upper = _anchor_position(thread, cursor) if cursor else None
    sequence = [known[ident] for ident in ids if ident in known]
    if len(sequence) != len(set(sequence)) or (
        upper is not None and any(p >= upper for p in sequence)
    ):
        raise ValueError("Cached history order changed; use explicit full import to reconcile it")
    if any(not 1 <= position <= MAX_POSITION for position in sequence):
        raise ValueError("Cached history positions exceed the supported range")
    # Live notifications assign slots in arrival order. Parallel tools can finish
    # in a different order from the persisted native history. A validated page is
    # authoritative for its covered items: reuse their slots in native order,
    # without moving uncovered items or changing stable hub/native identities.
    known = dict(zip((ident for ident in ids if ident in known), sorted(sequence), strict=True))
    result = [known.get(ident) for ident in ids]
    index = 0
    while index < len(result):
        if result[index] is not None:
            index += 1
            continue
        end = index
        while end < len(result) and result[end] is None:
            end += 1
        count = end - index
        left = result[index - 1] if index else None
        right = result[end] if end < len(result) else upper
        if right is not None and left is None:
            left = (
                db.session.query(db.func.max(Item.position))
                .filter(Item.thread_id == thread.id, Item.position < right)
                .scalar()
                or 0
            )
        if left is not None and right is None:
            right = (
                db.session.query(db.func.min(Item.position))
                .filter(Item.thread_id == thread.id, Item.position > left)
                .scalar()
            )
        if left is None and right is None:
            maximum = (
                db.session.query(db.func.max(Item.position)).filter_by(thread_id=thread.id).scalar()
            )
            start = maximum + TAIL_GAP if maximum is not None else INITIAL_POSITION
        elif right is not None:
            start = right - count
            if left is not None and start <= left:
                raise ValueError(
                    "Cached history has no stable position gap; use explicit full import"
                )
        else:
            start = left + 1
        if start < 1 or start + count - 1 > MAX_POSITION:
            raise ValueError(
                "Native history positions exceed the supported range; use explicit full import"
            )
        result[index:end] = range(start, start + count)
        index = end
    return result


def _complete(thread, command, params, put_item):
    generation = params["generation"]
    metadata_events = _events(thread, command, "anywhere/history_metadata", generation)
    item_events = _events(thread, command, "anywhere/history_item", generation)
    count = params.get("itemCount")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100:
        raise ValueError("Invalid native history page count")
    if len(metadata_events) != 1 or len(item_events) != count:
        raise ValueError("Native history page is incomplete")
    metadata = metadata_events[0].params.get("thread")
    if not isinstance(metadata, dict) or metadata.get("id") != thread.native_id:
        raise ValueError("Native history metadata identity does not match")
    version = metadata_events[0].params.get("snapshotUpdatedAt")
    fingerprint = metadata.get("_history_fingerprint")
    if (
        not _number(version)
        or params.get("snapshotUpdatedAt") != version
        or not isinstance(fingerprint, str)
    ):
        raise ValueError("Invalid native history snapshot")
    next_cursor = params.get("nextCursor")
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 8192
    ):
        raise ValueError("Invalid native history continuation cursor")
    if params.get("hasMore") is not (next_cursor is not None):
        raise ValueError("Invalid native history coverage")
    entries = [event.params for event in item_events]
    ids = []
    for index, entry in enumerate(entries):
        item = entry.get("item")
        if (
            entry.get("index") != index
            or not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(entry.get("turnId"), str)
            or not entry["turnId"]
            or not isinstance(item.get("type", "unknown"), str)
        ):
            raise ValueError("Invalid native history item order")
        ids.append(item["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Native history page repeated an item")
    if next_cursor is not None and not ids:
        raise ValueError("Empty native history page cannot advance coverage")
    _validate_next_cursor(
        next_cursor, thread, generation, version, fingerprint, ids[-1] if ids else None
    )
    entries.reverse()
    positions = _positions(thread, entries, command.payload.get("cursor"))
    for entry, position in zip(entries, positions, strict=True):
        timestamp = entry.get("createdAt")
        put_item(
            thread,
            entry["item"],
            entry["turnId"],
            position=position,
            created_at=timestamp if _number(timestamp) else None,
        )
    host = db.session.get(Host, thread.host_id)
    if not command.payload.get("cursor"):
        cwd = metadata.get("cwd")
        if cwd:
            thread.cwd = cwd
            thread.project_id = ensure_project(host, cwd).id
        if metadata.get("title"):
            thread.title = str(metadata["title"])[:240]
    current_version = thread.native_updated_at
    if current_version is None or version > current_version:
        thread.native_updated_at = version
        thread.native_fingerprint = fingerprint
    elif version == current_version and (
        thread.native_fingerprint is None
        or thread.native_fingerprint == command.payload.get("_native_fingerprint")
    ):
        thread.native_fingerprint = fingerprint
    thread.history_fingerprint = fingerprint
    thread.history_native_updated_at = version
    thread.history_synced_at = time.time()
    thread.history_stale = (
        thread.native_updated_at != version or thread.native_fingerprint != fingerprint
    )
    thread.history_cursor = next_cursor
    thread.history_generation = generation
    thread.history_complete = next_cursor is None
    thread.history_loaded = True
    thread.imported = True
    if thread.status == "importing":
        thread.status = "idle"
        thread.last_error = None
    command.status = "completed"
    command.error = None
    command.updated_at = time.time()
    command.result = {
        key: params[key]
        for key in ("generation", "snapshotUpdatedAt", "itemCount", "nextCursor", "hasMore")
    }
    command.result = dict(command.result, historyProjected=True)


def project_history(thread, method, params, live, put_item):
    """Handle a staged page event; complete is the sole atomic coverage checkpoint."""
    if method not in PAGE_METHODS:
        return False
    command = _command(thread, params, live)
    if command is None or history_command_applied(command) or method != "anywhere/history_complete":
        return True
    try:
        with db.session.begin_nested():
            _complete(thread, command, params, put_item)
            db.session.flush()
    except (ValueError, TypeError, KeyError) as error:
        command.status = "failed"
        command.error = str(error)
        command.updated_at = time.time()
        if not thread.history_loaded:
            thread.status = "error"
            thread.last_error = str(error)
    return True
