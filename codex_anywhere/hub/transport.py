"""Persist native Codex traffic and project it into reloadable UI state."""

from __future__ import annotations

import copy
import time

from .models import Asset, Command, Event, Host, Interaction, Item, Thread, db


def emit(method, params, thread_id=None, host_id=None):
    event = Event(method=method, params=params, thread_id=thread_id, host_id=host_id)
    db.session.add(event)
    return event


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
            Command.query.filter_by(thread_id=thread.id, kind="turn.start")
            .order_by(Command.created_at.desc())
            .limit(5)
            .all()
        )
        for command in commands:
            payload = command.payload
            if payload.get("_attached_item") not in (None, native_id):
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
    elif method == "turn/completed" and live:
        turn = params.get("turn", {})
        for item in turn.get("items", []):
            _put_item(thread, item, turn.get("id"))
        thread.status = {"failed": "error", "interrupted": "interrupted"}.get(
            turn.get("status"), "idle"
        )
        thread.active_turn_id = None
        for interaction in Interaction.query.filter_by(thread_id=thread.id, status="pending"):
            interaction.status = "expired"
    elif method == "serverRequest/resolved" and live:
        for interaction in Interaction.query.filter_by(thread_id=thread.id, status="pending"):
            if interaction.rpc_id == params.get("requestId"):
                interaction.status = "resolved"
        thread.status = "active" if thread.active_turn_id else "idle"
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
        thread.status = "error"


def handle_node_message(app, host_id, message):
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
                for interaction in Interaction.query.filter_by(host_id=host_id, status="pending"):
                    interaction.status = "expired"
                for thread in Thread.query.filter_by(host_id=host_id):
                    if thread.status in {"active", "waiting"}:
                        thread.status = "interrupted"
                        thread.active_turn_id = None
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
                else:
                    command.runtime_id = epoch
                    command.status = "sent"
                    replay.append(command.wire())
            emit("anywhere/host_online", {"host_id": host_id}, host_id=host_id)
            db.session.commit()
            broker.send(host_id, {"type": "welcome", "host_id": host_id, "protocol": 1})
            for wire in replay:
                broker.send_command({"host_id": host_id, "wire": wire})
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
                            thread.status = "error"
                        else:
                            thread.native_id = str(native["id"])
                            thread.status = "idle"
                    elif command.kind in {"turn.start", "turn.steer"}:
                        turn = result.get("turn", {})
                        if thread.status in {"active", "waiting"}:
                            thread.active_turn_id = (
                                turn.get("id") or result.get("turnId") or thread.active_turn_id
                            )
                elif thread and command.kind in {"thread.start", "turn.start"}:
                    thread.status = "error"
                    thread.active_turn_id = None
                if command.kind == "interaction.answer":
                    interaction = db.session.get(
                        Interaction, command.payload.get("_interaction_id")
                    )
                    if interaction:
                        interaction.status = (
                            "resolved" if command.status == "completed" else "expired"
                        )
                        if thread and thread.status == "waiting":
                            thread.status = "active" if thread.active_turn_id else "idle"
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
                status="pending" if live else "expired",
            )
            db.session.add(interaction)
            if thread and live:
                thread.status = "waiting"
            emit("anywhere/interaction", {"method": method}, thread.id if thread else None, host_id)
        if method == "anywhere/runtime_stopped" and live:
            for interaction in Interaction.query.filter_by(
                host_id=host_id, runtime_id=epoch, status="pending"
            ):
                interaction.status = "expired"
            for active in Thread.query.filter_by(host_id=host_id):
                if active.status in {"waiting", "active"}:
                    active.status = "interrupted"
                    active.active_turn_id = None
        db.session.commit()
        broker.send(host_id, {"type": "event_ack", "event_id": event_id})
