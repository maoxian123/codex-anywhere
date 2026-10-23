"""Durable, per-thread follow-up messages that have not reached the node yet."""

from __future__ import annotations

import copy
import time

from flask import current_app
from sqlalchemy import and_, or_
from werkzeug.exceptions import Conflict, NotFound

from .execution import execution_settings, require_runtime_settings
from .history import history_command_applied
from .models import Command, Event, Host, Interaction, Thread, db, iso
from .references import quote_references

CONTROL_KINDS = {
    "thread.start",
    "turn.start",
    "turn.steer",
    "turn.interrupt",
    "thread.takeover",
    "interaction.answer",
}
PENDING_STATUSES = ("queued", "sent", "running")
ASSET_KEYS = {"id", "name", "mime", "size", "url"}
REFERENCE_KEYS = {"kind", "title", "url", "thread_id", "item_id", "snapshot_id", "excerpt"}


def _pending(thread_id):
    return Command.query.filter_by(thread_id=thread_id, kind="turn.queue", status="pending")


def _controls(thread, host):
    return Command.query.filter(
        Command.thread_id == thread.id,
        Command.runtime_id == host.runtime_id,
        Command.kind.in_(CONTROL_KINDS),
    )


def _unknown_controls(thread, host):
    return [
        command
        for command in _controls(thread, host).filter_by(status="unknown")
        if not command.payload.get("_queue_resume_ack")
    ]


def _interactions(thread, host):
    return Interaction.query.filter(
        Interaction.thread_id == thread.id,
        Interaction.runtime_id == host.runtime_id,
        Interaction.status.in_(("pending", "answering")),
    ).first()


def should_enqueue(thread, host):
    """Use the same runtime guards for new messages as for saved queue entries."""
    return bool(
        thread.active_turn_id
        or thread.status in {"active", "waiting"}
        or _pending(thread.id).first()
        or _controls(thread, host).filter(Command.status.in_(PENDING_STATUSES)).first()
        or _unknown_controls(thread, host)
        or _interactions(thread, host)
    )


def _settings(host, thread, payload):
    settings = execution_settings(thread.execution_settings or {})
    overrides = payload.get("_queue_overrides")
    if overrides is not None:
        settings.update(execution_settings(overrides))
    if "runtimeSettings" not in (host.capabilities or []) and set(settings) <= {"model"}:
        return None
    require_runtime_settings(host, settings)
    return settings


def _pause_reason(thread, host, queued=None):
    if queued is None:
        queued = _pending(thread.id).all()
    for command in queued:
        reason = command.payload.get("_queue_pause")
        if isinstance(reason, str) and reason:
            return reason
    if host is not None and _unknown_controls(thread, host):
        return "上一条操作结果未知，请确认后继续队列"
    if thread.last_error or thread.status in {"error", "interrupted"}:
        return "上一轮失败或已停止，请确认后继续队列"
    return None


def _mark_paused(thread, reason):
    changed = False
    for command in _pending(thread.id):
        if not command.payload.get("_queue_pause"):
            command.payload = dict(command.payload, _queue_pause=reason)
            changed = True
    return changed


def _blocked(thread, host, *, resume=False):
    """Return (blocked, paused, reason); ordinary waiting never needs Resume."""
    if host is None:
        return True, True, "节点不存在"
    if not resume and (reason := _pause_reason(thread, host)):
        return True, True, reason
    if thread.active_turn_id or thread.status in {"active", "waiting"}:
        return True, False, "等待当前轮次结束"
    if _interactions(thread, host):
        return True, False, "等待审批或用户回答"
    if _controls(thread, host).filter(Command.status.in_(PENDING_STATUSES)).first():
        return True, False, "等待已发送的操作完成"
    if not current_app.extensions["broker"].is_online(host.id) or not host.runtime_id:
        return True, False, "等待节点重新连接"
    if thread.archived:
        return True, False, "等待取消归档"
    if not thread.native_id or not thread.history_loaded or thread.status in {"starting", "importing"}:
        return True, False, "等待会话准备完成"
    if thread.status != "idle" and not (resume and thread.status in {"error", "interrupted"}):
        return True, False, "等待会话空闲"
    return False, False, None


def queue_state(thread):
    """Public queue projection: never return native input or private payload metadata."""
    messages = []
    queued = _pending(thread.id).order_by(Command.created_at, Command.id).all()
    for command in queued:
        payload = command.payload
        messages.append(
            dict(
                id=command.id,
                text=payload.get("_display_text", ""),
                assets=[
                    {key: copy.deepcopy(value) for key, value in asset.items() if key in ASSET_KEYS}
                    for asset in payload.get("_assets", [])
                    if isinstance(asset, dict)
                ],
                references=[
                    {key: copy.deepcopy(value) for key, value in reference.items() if key in REFERENCE_KEYS}
                    for reference in payload.get("_references", [])
                    if isinstance(reference, dict)
                ],
                revision=payload.get("_queue_revision", 0),
                created_at=iso(command.created_at),
                updated_at=iso(command.updated_at),
            )
        )
    host = db.session.get(Host, thread.host_id)
    blocked, paused, reason = _blocked(thread, host)
    if queued and not blocked:
        try:
            _settings(host, thread, queued[0].payload)
        except (ValueError, Conflict) as exc:
            paused = True
            reason = exc.description if isinstance(exc, Conflict) else str(exc)
    result = dict(messages=messages, paused=paused if messages else False)
    if reason and messages:
        result["reason"] = reason
    return result


def _emit(thread):
    db.session.add(
        Event(
            method="anywhere/message_queue",
            params={"queue": queue_state(thread)},
            thread_id=thread.id,
            host_id=thread.host_id,
        )
    )


def mark_queue_paused(thread, reason=None):
    """Record a terminal projection's pause in its caller's transaction, without commit."""
    if reason is None:
        host = db.session.get(Host, thread.host_id)
        reason = _pause_reason(thread, host) or "上一轮失败或已停止，请确认后继续队列"
    if not _mark_paused(thread, reason):
        return False
    _emit(thread)
    return True


def enqueue_prepared(host, thread, payload, key):
    """Accept already validated immutable input; journal before any later dispatch."""
    with current_app.extensions["broker"].command_lock:
        existing = Command.query.filter_by(idempotency_key=key).first()
        if existing:
            if existing.payload.get("_signature") != payload.get("_signature"):
                raise Conflict("同一个请求 ID 对应了不同内容")
            return existing
        prepared = copy.deepcopy(payload)
        prepared["_queue_revision"] = 0
        if reason := _pause_reason(thread, host):
            prepared["_queue_pause"] = reason
            _mark_paused(thread, reason)
        # Desired settings are selected at dispatch, rather than enqueue time.
        prepared.pop("execution_settings", None)
        prepared.pop("settings_revision", None)
        command = Command(
            host_id=host.id,
            thread_id=thread.id,
            kind="turn.queue",
            payload=prepared,
            idempotency_key=key,
            status="pending",
        )
        db.session.add(command)
        db.session.flush()
        _emit(thread)
        db.session.commit()
        return command


def _editable(thread, command_id, revision):
    command = Command.query.filter_by(id=command_id, thread_id=thread.id).populate_existing().first()
    if command is None:
        raise NotFound("排队消息不存在")
    if command.kind != "turn.queue" or command.status != "pending":
        raise Conflict("消息已经发送或取消，请刷新队列")
    if type(revision) is not int or revision != command.payload.get("_queue_revision", 0):
        raise Conflict("排队消息已更新，请刷新后重试")
    return command


def update_queued(thread, command_id, text, revision):
    """Edit only user text; attachments and reference snapshots remain immutable."""
    if not isinstance(text, str) or len(text) > 100_000:
        raise ValueError("消息文本无效或超过 10 万字符")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("文本包含无效的 Unicode 字符") from exc
    with current_app.extensions["broker"].command_lock:
        command = _editable(thread, command_id, revision)
        payload = copy.deepcopy(command.payload)
        if not text.strip() and not payload.get("_assets") and not payload.get("_references"):
            raise ValueError("请输入消息或添加图片、引用")
        quoted = quote_references(text, payload.get("_references", []))
        inputs = payload["input"]
        for item in inputs:
            if item.get("type") == "text":
                item["text"] = quoted
                break
        else:
            inputs.insert(0, {"type": "text", "text": quoted})
        payload.update(_display_text=text, _queue_revision=revision + 1)
        command.payload = payload
        command.updated_at = time.time()
        _emit(thread)
        db.session.commit()
        return command


def cancel_queued(thread, command_id, revision):
    """Keep a cancelled message in the command journal without delivering it."""
    with current_app.extensions["broker"].command_lock:
        command = _editable(thread, command_id, revision)
        command.status = "cancelled"
        command.updated_at = time.time()
        command.payload = dict(command.payload, _queue_revision=revision + 1)
        _emit(thread)
        db.session.commit()
        return command


def _acknowledge_pause(thread, host):
    for command in _unknown_controls(thread, host):
        # Acknowledge uncertainty, preserving its original outcome and signature.
        command.payload = dict(command.payload, _queue_resume_ack=time.time())
    for command in _pending(thread.id):
        if "_queue_pause" in command.payload:
            payload = dict(command.payload)
            payload.pop("_queue_pause")
            command.payload = payload
    thread.status = "idle"
    thread.last_error = None


def dispatch_queue(app, host_id=None, thread_id=None, resume=False):
    """Commit one eligible FIFO message before delivery; never retry old outcomes."""
    broker = app.extensions["broker"]
    with app.app_context(), broker.command_lock:
        query = Command.query.filter_by(kind="turn.queue", status="pending")
        if host_id is not None:
            query = query.filter_by(host_id=host_id)
        if thread_id is not None:
            query = query.filter_by(thread_id=thread_id)
        considered = set()
        for command in query.order_by(Command.created_at, Command.id).all():
            if command.thread_id in considered:
                continue
            considered.add(command.thread_id)
            thread = db.session.get(Thread, command.thread_id)
            host = db.session.get(Host, command.host_id)
            if thread is None:
                continue
            if not resume and (reason := _pause_reason(thread, host)):
                if mark_queue_paused(thread, reason):
                    db.session.commit()
                continue
            blocked, _, reason = _blocked(thread, host, resume=resume)
            if blocked:
                if resume:
                    raise Conflict(reason)
                continue
            try:
                settings = _settings(host, thread, command.payload)
            except (ValueError, Conflict):
                if resume:
                    raise
                continue
            if resume:
                _acknowledge_pause(thread, host)
            payload = copy.deepcopy(command.payload)
            payload["native_thread_id"] = thread.native_id
            if settings is not None:
                payload.update(execution_settings=settings, settings_revision=thread.settings_revision)
            for history in Command.query.filter_by(thread_id=thread.id, kind="thread.history").filter(
                Command.status.in_((*PENDING_STATUSES, "completed"))
            ):
                if not history_command_applied(history):
                    history.status = "failed"
                    history.error = "会话已开始新轮次，此历史页未应用；请刷新历史"
            command.payload = payload
            command.kind = "turn.start"
            command.status = "queued"
            command.runtime_id = host.runtime_id
            command.updated_at = time.time()
            thread.status = "active"
            thread.last_error = None
            wire = command.wire()
            _emit(thread)
            db.session.commit()
            delivered = broker.send_command({"host_id": host.id, "wire": wire})
            if delivered:
                # A fast peer can complete even before send_command returns.
                Command.query.filter_by(id=command.id, status="queued").update({"status": "sent"})
                db.session.commit()
            db.session.refresh(command)
            db.session.expunge(command)
            return command
        return None


def resume_queue(app, thread_id):
    """Explicitly continue pending messages without retrying an uncertain command."""
    return dispatch_queue(app, thread_id=thread_id, resume=True)


def dispatch_ready_queues(app, host_id):
    """Give each connected host's pending thread a bounded dispatch opportunity."""
    with app.app_context(), app.extensions["broker"].command_lock:
        heads = (
            db.session.query(
                Command.thread_id.label("thread_id"),
                db.func.min(Command.created_at).label("queued_at"),
            )
            .filter_by(host_id=host_id, kind="turn.queue", status="pending")
            .group_by(Command.thread_id)
            .subquery()
        )
        query = db.session.query(heads.c.thread_id, heads.c.queued_at).order_by(
            heads.c.queued_at, heads.c.thread_id
        )
        cursors = app.extensions.setdefault("message_queue_cursors", {})
        cursor = cursors.get(host_id)
        if cursor:
            after_cursor = or_(
                heads.c.queued_at > cursor[0],
                and_(heads.c.queued_at == cursor[0], heads.c.thread_id > cursor[1]),
            )
            rows = query.filter(after_cursor).limit(100).all()
            if len(rows) < 100:
                rows.extend(query.filter(~after_cursor).limit(100 - len(rows)).all())
        else:
            rows = query.limit(100).all()
        if rows:
            cursors[host_id] = (rows[-1].queued_at, rows[-1].thread_id)
        else:
            cursors.pop(host_id, None)
        thread_ids = [row.thread_id for row in rows]
    dispatched = []
    for thread_id in thread_ids:
        command = dispatch_queue(app, host_id=host_id, thread_id=thread_id)
        if command is not None:
            dispatched.append(command)
    return dispatched
