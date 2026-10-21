"""Durable control-plane records; the node remains the runtime authority."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import case, func, select
from sqlalchemy.orm import column_property

db = SQLAlchemy()


def new_id() -> str:
    return uuid.uuid4().hex


def iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class HubSetting(db.Model):
    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, onupdate=time.time, nullable=False)


class Host(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(120), nullable=False)
    platform = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    version = db.Column(db.String(120), default="")
    runtime_id = db.Column(db.String(100), nullable=True)
    last_seen_at = db.Column(db.Float, default=time.time)
    last_sync_at = db.Column(db.Float, nullable=True)
    sync_error = db.Column(db.Text, nullable=True)
    capabilities = db.Column(db.JSON, default=list, nullable=False)
    runtime_capabilities = db.Column(db.JSON, nullable=True)
    capabilities_updated_at = db.Column(db.Float, nullable=True)

    def as_dict(self, online=False):
        return dict(
            id=self.id,
            name=self.name,
            platform=self.platform,
            version=self.version,
            runtime_id=self.runtime_id,
            online=online,
            last_seen_at=iso(self.last_seen_at),
            last_sync_at=iso(self.last_sync_at),
            sync_error=self.sync_error,
            capabilities=self.capabilities or [],
        )


class Pairing(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    code_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.Float, nullable=False)
    consumed = db.Column(db.Boolean, default=False, nullable=False)


class Project(db.Model):
    __table_args__ = (db.UniqueConstraint("host_id", "cwd_key"),)
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    host_id = db.Column(db.String(32), db.ForeignKey("host.id"), nullable=False, index=True)
    name = db.Column(db.String(240), nullable=False)
    cwd = db.Column(db.Text, nullable=False)
    cwd_key = db.Column(db.String(64), nullable=False)
    execution_settings = db.Column(db.JSON, default=dict, nullable=False)
    created_at = db.Column(db.Float, default=time.time, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self, thread_count=None, last_activity_at=None):
        if thread_count is None:
            thread_count = Thread.query.filter_by(project_id=self.id).count()
        if last_activity_at is None:
            inputs = latest_thread_inputs()
            last_activity_at = (
                db.session.query(func.max(thread_activity_at(inputs.c.created_at)))
                .select_from(Thread)
                .outerjoin(inputs, inputs.c.thread_id == Thread.id)
                .filter(Thread.project_id == self.id)
                .scalar()
            ) or self.created_at
        return dict(
            id=self.id,
            host_id=self.host_id,
            name=self.name,
            cwd=self.cwd,
            execution_settings=self.execution_settings or {},
            thread_count=thread_count,
            created_at=iso(self.created_at),
            updated_at=iso(self.updated_at),
            last_activity_at=iso(last_activity_at),
        )


class Thread(db.Model):
    __table_args__ = (db.UniqueConstraint("host_id", "native_id"),)
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    host_id = db.Column(db.String(32), db.ForeignKey("host.id"), nullable=False, index=True)
    project_id = db.Column(db.String(32), db.ForeignKey("project.id"), nullable=True, index=True)
    native_id = db.Column(db.String(200), nullable=True)
    title = db.Column(db.String(240), nullable=False)
    cwd = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), default="starting", nullable=False)
    active_turn_id = db.Column(db.String(200), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    imported = db.Column(db.Boolean, default=False, nullable=False)
    history_loaded = db.Column(db.Boolean, default=True, nullable=False)
    native_updated_at = db.Column(db.Float, nullable=True)
    history_native_updated_at = db.Column(db.Float, nullable=True)
    history_synced_at = db.Column(db.Float, nullable=True)
    history_stale = db.Column(db.Boolean, default=False, nullable=False)
    history_cursor = db.Column(db.Text, nullable=True)
    history_complete = db.Column(db.Boolean, default=False, nullable=False)
    history_generation = db.Column(db.String(64), nullable=True)
    native_fingerprint = db.Column(db.String(64), nullable=True)
    history_fingerprint = db.Column(db.String(64), nullable=True)
    execution_settings = db.Column(db.JSON, default=dict, nullable=False)
    effective_settings = db.Column(db.JSON, default=dict, nullable=False)
    settings_revision = db.Column(db.Integer, default=0, nullable=False)
    effective_settings_revision = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.Float, default=time.time, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, nullable=False, index=True)

    def as_dict(self):
        return dict(
            id=self.id,
            host_id=self.host_id,
            project_id=self.project_id,
            native_id=self.native_id,
            title=self.title,
            cwd=self.cwd,
            status=self.status,
            active_turn_id=self.active_turn_id,
            last_error=self.last_error,
            imported=self.imported,
            history_loaded=self.history_loaded,
            native_updated_at=self.native_updated_at,
            history_native_updated_at=self.history_native_updated_at,
            history_synced_at=iso(self.history_synced_at),
            history_stale=self.history_stale,
            history_cursor=self.history_cursor,
            history_complete=self.history_complete,
            history_generation=self.history_generation,
            execution_settings=self.execution_settings or {},
            effective_settings=self.effective_settings or {},
            settings_revision=self.settings_revision or 0,
            effective_settings_revision=self.effective_settings_revision,
            created_at=iso(self.created_at),
            updated_at=iso(self.updated_at),
            last_activity_at=iso(self.last_activity_at),
        )


class Command(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    host_id = db.Column(db.String(32), db.ForeignKey("host.id"), nullable=False, index=True)
    thread_id = db.Column(db.String(32), db.ForeignKey("thread.id"), nullable=True)
    kind = db.Column(db.String(60), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    idempotency_key = db.Column(db.String(250), nullable=True, unique=True)
    status = db.Column(db.String(24), default="queued", nullable=False)
    result = db.Column(db.JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    runtime_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.Float, default=time.time, nullable=False)
    updated_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            id=self.id,
            host_id=self.host_id,
            thread_id=self.thread_id,
            kind=self.kind,
            status=self.status,
            result=self.result,
            error=self.error,
            created_at=iso(self.created_at),
            updated_at=iso(self.updated_at),
        )

    def wire(self):
        return dict(id=self.id, kind=self.kind, thread_id=self.thread_id, payload=self.payload)


# Native versions describe conversation activity; updated_at also changes while
# loading history. A locally queued input must become recent before native
# metadata catches up. Keep this derived so existing databases need no migration.
def thread_activity_at(latest_input_at):
    native = case(
        (Thread.native_updated_at > Thread.history_native_updated_at, Thread.native_updated_at),
        else_=func.coalesce(
            Thread.history_native_updated_at,
            Thread.native_updated_at,
            Thread.updated_at,
            Thread.created_at,
        ),
    )
    return case((latest_input_at > native, latest_input_at), else_=native)


def latest_thread_inputs():
    """Scan the command journal once for a list, rather than once per thread."""
    return (
        select(Command.thread_id, func.max(Command.created_at).label("created_at"))
        .where(Command.kind.in_(["turn.start", "turn.steer"]))
        .group_by(Command.thread_id)
        .subquery()
    )


_latest_input_at = (
    select(func.max(Command.created_at))
    .where(Command.thread_id == Thread.id, Command.kind.in_(["turn.start", "turn.steer"]))
    .correlate_except(Command)
    .scalar_subquery()
)
Thread.last_activity_at = column_property(
    thread_activity_at(_latest_input_at), deferred=True
)


class Item(db.Model):
    __table_args__ = (
        db.UniqueConstraint("thread_id", "native_id"),
        db.Index("ix_item_thread_position", "thread_id", "position", "id"),
    )
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    native_id = db.Column(db.String(250), nullable=False)
    thread_id = db.Column(db.String(32), db.ForeignKey("thread.id"), nullable=False, index=True)
    turn_id = db.Column(db.String(200), nullable=True)
    type = db.Column(db.String(100), nullable=False)
    data = db.Column(db.JSON, nullable=False)
    position = db.Column(db.BigInteger, default=0, nullable=False)
    created_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            id=self.id,
            native_id=self.native_id,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            type=self.type,
            data=self.data,
            position=self.position,
            created_at=iso(self.created_at),
        )


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_key = db.Column(db.String(200), nullable=True, unique=True)
    host_id = db.Column(db.String(32), nullable=True, index=True)
    thread_id = db.Column(db.String(32), nullable=True, index=True)
    method = db.Column(db.String(200), nullable=False)
    params = db.Column(db.JSON, nullable=False, default=dict)
    runtime_id = db.Column(db.String(100), nullable=True)
    seq = db.Column(db.BigInteger, nullable=True)
    created_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            type="event",
            id=self.id,
            thread_id=self.thread_id,
            method=self.method,
            params=self.params,
        )


class Interaction(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    host_id = db.Column(db.String(32), nullable=False)
    thread_id = db.Column(db.String(32), db.ForeignKey("thread.id"), nullable=True, index=True)
    runtime_id = db.Column(db.String(100), nullable=False)
    rpc_id = db.Column(db.JSON, nullable=False)
    method = db.Column(db.String(200), nullable=False)
    params = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(24), default="pending", nullable=False)
    created_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            id=self.id,
            thread_id=self.thread_id,
            method=self.method,
            params=self.params,
            status=self.status,
        )


class Asset(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    host_id = db.Column(db.String(32), nullable=True)
    thread_id = db.Column(db.String(32), nullable=True, index=True)
    native_item_id = db.Column(db.String(250), nullable=True)
    name = db.Column(db.String(250), nullable=False)
    mime = db.Column(db.String(80), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    path = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            id=self.id, name=self.name, mime=self.mime, size=self.size, url=f"/api/assets/{self.id}"
        )


class Snapshot(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    kind = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.Text, nullable=True)
    thread_id = db.Column(db.String(32), nullable=True)
    item_id = db.Column(db.String(250), nullable=True)
    excerpt = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.Float, default=time.time, nullable=False)

    def as_dict(self):
        return dict(
            kind=self.kind,
            title=self.title,
            url=self.url,
            thread_id=self.thread_id,
            item_id=self.item_id,
            snapshot_id=self.id,
            excerpt=self.excerpt,
        )
