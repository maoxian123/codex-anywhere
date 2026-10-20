"""Persist native Codex traffic and project it into reloadable UI state."""

from __future__ import annotations

import copy
import json
import time

from .models import Asset, Command, Event, Host, Interaction, Item, Thread, db


def emit(method, params, thread_id=None, host_id=None):
    event = Event(method=method, params=params, thread_id=thread_id, host_id=host_id)
    db.session.add(event)
    return event


def _error_detail(value, fallback):
    """Keep native human-readable diagnostics available after a page reload."""
    if isinstance(value, str):
        return value.strip()[:16000] or fallback
    if isinstance(value, dict):
        parts = []
        for key in ("message", "additionalDetails", "details"):
            detail = value.get(key)
            if isinstance(detail, str) and detail.strip() and detail.strip() not in parts:
                parts.append(detail.strip())
        if parts:
            return "\n".join(parts)[:16000]
        if value:
            return json.dumps(value, ensure_ascii=False)[:16000]
    return fallback


def _assets(item):
    previous = {a.get("id"): a for a in item.data.get("_assets", []) if isinstance(a, dict)}
    result = []
    for asset in Asset.query.filter_by(thread_id=item.thread_id, native_item_id=item.native_id):
        data = asset.as_dict()
        source_path = previous.get(asset.id, {}).get("source_path")
        if source_path:
            data["source_path"] = source_path
        result.append(data)
    return result


def _input_identity(values):
    if not isinstance(values, list):
        return None
    result = []
    for value in values:
        if not isinstance(value, dict):
            return None
        kind = value.get("type")
        if kind == "text":
            if value.get("text"):
                result.append((kind, value["text"]))
        elif kind == "image":
            result.append((kind, value.get("url")))
        else:
            return None
    return result


def _open_interactions(thread):
    host = db.session.get(Host, thread.host_id)
    return Interaction.query.filter(
        Interaction.thread_id == thread.id,
        Interaction.runtime_id == host.runtime_id,
        Interaction.status.in_(["pending", "answering"]),
    )


def _refresh_waiting(thread):
    if thread.status not in {"active", "waiting"}:
        return
    if _open_interactions(thread).first():
        thread.status = "waiting"
    else:
        thread.status = "active" if thread.active_turn_id else "idle"


def _put_item(thread, data, turn_id):
    native_id = data.get("id")
    if not native_id:
        return
    item = Item.query.filter_by(thread_id=thread.id, native_id=str(native_id)).first()
    metadata = {k: v for k, v in (item.data if item else {}).items() if k.startswith("_")}
    if item is None:
        item = Item(
            thread_id=thread.id,
            native_id=str(native_id),
            turn_id=turn_id,
            type=data.get("type", "unknown"),
            data={},
        )
        db.session.add(item)
    item.type = data.get("type", item.type)
    item.turn_id = turn_id or item.turn_id
    value = copy.deepcopy(data)
    value.update(metadata)
    attached_assets = _assets(item)
    if attached_assets:
        value["_assets"] = attached_assets
    if item.type == "userMessage" and "_displayText" not in value:
        commands = (
            Command.query.filter(
                Command.thread_id == thread.id,
                Command.kind.in_(["turn.start", "turn.steer"]),
                Command.status != "failed",
                Command.payload["_attached_item"].as_string().is_(None),
            )
            .order_by(Command.created_at, Command.id)
            .all()
        )
        identity = _input_identity(data.get("content"))
        for command in commands:
            payload = command.payload
            if identity is None or _input_identity(payload.get("input")) != identity:
                continue
            if payload.get("_display_text") is not None:
                value["_displayText"] = payload["_display_text"]
                value["_references"] = payload.get("_references", [])
                value["_assets"] = payload.get("_assets", [])
                command.payload = dict(payload, _attached_item=native_id)
                break
    item.data = value


def _project(thread, method, params, live=True):
    if not thread:
        return
    if live:
        thread.updated_at = time.time()
    turn_id = params.get("turnId")
    if method in {"item/started", "item/completed"}:
        item = params.get("item", {})
        if isinstance(item, dict):
            _put_item(thread, item, turn_id)
    elif method == "item/agentMessage/delta":
        native_id = str(params.get("itemId", ""))
        item = Item.query.filter_by(thread_id=thread.id, native_id=native_id).first()
        if not item:
            item = Item(
                thread_id=thread.id,
                native_id=native_id,
                turn_id=turn_id,
                type="agentMessage",
                data={"id": native_id, "type": "agentMessage", "text": ""},
            )
            db.session.add(item)
        data = dict(item.data)
        data["text"] = data.get("text", "") + str(params.get("delta", ""))
        item.data = data
    elif method == "item/commandExecution/outputDelta":
        item = Item.query.filter_by(
            thread_id=thread.id, native_id=str(params.get("itemId"))
        ).first()
        if item:
            item.data = dict(
                item.data,
                aggregatedOutput=item.data.get("aggregatedOutput", "")
                + str(params.get("delta", "")),
            )
    elif method == "turn/started" and live:
        thread.active_turn_id = params.get("turn", {}).get("id")
        thread.status = "active"
        thread.last_error = None
    elif method == "turn/completed":
        turn = params.get("turn", {})
        for item in turn.get("items", []):
            _put_item(thread, item, turn.get("id"))
        if (
            not live
            or (thread.active_turn_id and thread.active_turn_id != turn.get("id"))
            or (thread.status in {"active", "waiting"} and thread.active_turn_id != turn.get("id"))
        ):
            return
        thread.status = {"failed": "error", "interrupted": "interrupted"}.get(
            turn.get("status"), "idle"
        )
        if turn.get("status") == "failed":
            thread.last_error = _error_detail(
                turn.get("error"), thread.last_error or "Codex 执行失败，未返回错误详情"
            )
        elif turn.get("status") == "completed":
            thread.last_error = None
        thread.active_turn_id = None
        for interaction in _open_interactions(thread):
            interaction.status = "expired"
    elif method == "serverRequest/resolved" and live:
        for interaction in _open_interactions(thread):
            if interaction.rpc_id == params.get("requestId"):
                interaction.status = "resolved"
        _refresh_waiting(thread)
    elif method == "anywhere/assets":
        item = Item.query.filter_by(
            thread_id=thread.id, native_id=str(params.get("itemId"))
        ).first()
        if item:
            values = _assets(item)
            reported = {a.get("id"): a for a in params.get("assets", []) if isinstance(a, dict)}
            for value in values:
                source_path = reported.get(value["id"], {}).get("source_path")
                if isinstance(source_path, str):
                    value["source_path"] = source_path[:4000]
            item.data = dict(item.data, _assets=values)
    elif method == "error" and live:
        if params.get("willRetry") is True or (
            turn_id and thread.active_turn_id and turn_id != thread.active_turn_id
        ):
            return
        thread.status = "error"
        thread.last_error = _error_detail(params.get("error", params), "Codex 执行失败")


def handle_node_message(app, host_id, message, flush_outgoing=None, outgoing_socket=None):
    """Persist first; let the connection owner stream hello replay outside DB locks."""
    if flush_outgoing is not None and outgoing_socket is None:
        raise ValueError("Hello replay requires its original outgoing socket")
    replay = _persist_node_message(app, host_id, message, defer_hello=flush_outgoing is not None)
    if replay is not None:
        broker = app.extensions["broker"]
        for frame in replay:
            if (
                not broker.send(host_id, frame, expected_socket=outgoing_socket)
                or not flush_outgoing()
            ):
                return
        # Keep HTTP producers offline until welcome and older commands have
        # reached this owner; otherwise a new command can precede hello replay.
        if broker.mark_ready(host_id, outgoing_socket):
            with app.app_context():
                emit("anywhere/host_online", {"host_id": host_id}, host_id=host_id)
                db.session.commit()


def _persist_node_message(app, host_id, message, defer_hello=False):
    """Process one frame, committing before an event ACK. Also used by integration tests."""
    broker = app.extensions["broker"]
    with app.app_context(), broker.command_lock:
        host = db.session.get(Host, host_id)
        if not host:
            return
        kind = message.get("type")
        host.last_seen_at = time.time()
        broker.touch(host_id)
        if kind == "hello":
            epoch = str(message.get("runtime_id", ""))[:100]
            if not epoch:
                raise ValueError("runtime_id is required")
            changed = host.runtime_id and host.runtime_id != epoch
            host.runtime_id = epoch
            host.version = str(message.get("version", host.version))[:120]
            # Roots are granted at pairing, never silently expanded through hello.
            if changed:
                for interaction in Interaction.query.filter(
                    Interaction.host_id == host_id,
                    Interaction.status.in_(["pending", "answering"]),
                ):
                    interaction.status = "expired"
                for thread in Thread.query.filter_by(host_id=host_id):
                    if thread.status in {"active", "waiting"}:
                        thread.status = "interrupted"
                        thread.active_turn_id = None
                        thread.last_error = "节点运行进程已更换，执行结果未知；未自动重试"
            pending = Command.query.filter(
                Command.host_id == host_id, Command.status.in_(["queued", "sent", "running"])
            ).all()
            replay = []
            for command in pending:
                if command.runtime_id and command.runtime_id != epoch:
                    command.status = "unknown"
                    command.error = "节点运行进程已更换，执行结果未知；未自动重试"
                    affected = (
                        db.session.get(Thread, command.thread_id) if command.thread_id else None
                    )
                    if affected and command.kind in {"thread.start", "turn.start"}:
                        affected.status = "interrupted"
                        affected.active_turn_id = None
                        affected.last_error = command.error
                else:
                    command.runtime_id = epoch
                    command.status = "sent"
                    replay.append(command.wire())
            if not defer_hello:
                emit("anywhere/host_online", {"host_id": host_id}, host_id=host_id)
            db.session.commit()
            if defer_hello:
                return [{"type": "welcome", "host_id": host_id, "protocol": 1}] + [
                    {"type": "command", "command": wire} for wire in replay
                ]
            broker.send(host_id, {"type": "welcome", "host_id": host_id, "protocol": 1})
            for wire in replay:
                if not broker.send_command({"host_id": host_id, "wire": wire}):
                    return
            return
        if kind == "ping":
            db.session.commit()
            broker.send(host_id, {"type": "pong"})
            return
        if kind in {"command_started", "command_result"}:
            command = Command.query.filter_by(id=message.get("id"), host_id=host_id).first()
            definitive_late = (
                kind == "command_result"
                and not message.get("unknown")
                and isinstance(message.get("result"), dict)
            )
            if (
                not command
                or command.status in {"completed", "failed"}
                or (command.status == "unknown" and not definitive_late)
            ):
                return
            thread = db.session.get(Thread, command.thread_id) if command.thread_id else None
            # A completed old command is useful evidence, but cannot overwrite a
            # newer runtime's live failure. A late thread creation can settle its
            # own unknown state when no newer state has replaced that diagnostic.
            project_result = command.runtime_id == host.runtime_id or (
                thread is not None
                and command.kind == "thread.start"
                and thread.native_id is None
                and thread.last_error == command.error
            )
            if thread and command.kind == "turn.start":
                # A result can arrive after its turn's events and even after
                # another turn has started on the same process.
                newer_start = Command.query.filter(
                    Command.thread_id == thread.id,
                    Command.kind == "turn.start",
                    Command.created_at > command.created_at,
                ).first()
                project_result = project_result and newer_start is None
            command.updated_at = time.time()
            if kind == "command_started":
                command.status = "running"
            else:
                command.status = (
                    "unknown"
                    if message.get("unknown")
                    else ("failed" if message.get("error") else "completed")
                )
                command.error = str(message["error"])[:8000] if message.get("error") else None
                command.result = message.get("result")
                if command.status == "completed" and thread:
                    result = command.result or {}
                    if command.kind == "thread.start":
                        native = result.get("thread", {})
                        if not native.get("id"):
                            command.status, command.error = "failed", "Codex 未返回会话 ID"
                            if project_result:
                                thread.status = "error"
                                thread.last_error = command.error
                        else:
                            thread.native_id = str(native["id"])
                            if project_result:
                                thread.status = "idle"
                                thread.last_error = None
                    elif command.kind == "turn.start" and project_result:
                        turn = result.get("turn", {})
                        if thread.status in {"active", "waiting"} and not thread.active_turn_id:
                            thread.active_turn_id = (
                                turn.get("id") or result.get("turnId") or thread.active_turn_id
                            )
                elif thread and command.kind in {"thread.start", "turn.start"} and project_result:
                    thread.status = "interrupted" if command.status == "unknown" else "error"
                    thread.active_turn_id = None
                    thread.last_error = command.error or "Codex 指令未成功完成"
                if command.kind == "interaction.answer":
                    interaction = db.session.get(
                        Interaction, command.payload.get("_interaction_id")
                    )
                    if interaction:
                        interaction.status = (
                            "resolved" if command.status == "completed" else "expired"
                        )
                        if thread and project_result:
                            _refresh_waiting(thread)
                emit(
                    "anywhere/command_completed",
                    {"command": command.as_dict()},
                    command.thread_id,
                    host_id,
                )
            db.session.commit()
            return
        if kind != "event":
            raise ValueError("Unknown node frame")
        event_id = str(message.get("event_id", ""))
        if not event_id or len(event_id) > 100:
            raise ValueError("Invalid event id")
        key = f"{host_id}:{event_id}"
        if Event.query.filter_by(event_key=key).first():
            broker.send(host_id, {"type": "event_ack", "event_id": event_id})
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Event params must be an object")
        method = str(message.get("method", ""))[:200]
        thread_params = params.get("thread")
        native_id = params.get("threadId") or (
            thread_params.get("id") if isinstance(thread_params, dict) else None
        )
        thread = (
            Thread.query.filter_by(host_id=host_id, native_id=str(native_id)).first()
            if native_id
            else None
        )
        epoch = str(message.get("runtime_id", ""))[:100]
        event = Event(
            event_key=key,
            host_id=host_id,
            thread_id=thread.id if thread else None,
            method=method,
            params=params,
            runtime_id=epoch,
            seq=message.get("seq"),
        )
        db.session.add(event)
        live = epoch == host.runtime_id
        _project(thread, method, params, live=live)
        if "rpc_id" in message:
            interaction = Interaction(
                host_id=host_id,
                thread_id=thread.id if thread else None,
                runtime_id=epoch,
                rpc_id=message["rpc_id"],
                method=method,
                params=params,
                status="pending"
                if live
                and not (
                    thread
                    and thread.active_turn_id
                    and params.get("turnId")
                    and params["turnId"] != thread.active_turn_id
                )
                else "expired",
            )
            db.session.add(interaction)
            if thread and interaction.status == "pending":
                thread.active_turn_id = thread.active_turn_id or params.get("turnId")
                thread.status = "waiting"
            emit("anywhere/interaction", {"method": method}, thread.id if thread else None, host_id)
        if method == "anywhere/runtime_stopped" and live:
            for interaction in Interaction.query.filter(
                Interaction.host_id == host_id,
                Interaction.runtime_id == epoch,
                Interaction.status.in_(["pending", "answering"]),
            ):
                interaction.status = "expired"
            for active in Thread.query.filter_by(host_id=host_id):
                if active.status in {"waiting", "active"}:
                    active.status = "interrupted"
                    active.active_turn_id = None
                    code = params.get("returncode")
                    active.last_error = "Codex 进程已停止，当前执行已中断" + (
                        f"（退出码 {code}）" if code is not None else ""
                    )
        db.session.commit()
        broker.send(host_id, {"type": "event_ack", "event_id": event_id})
