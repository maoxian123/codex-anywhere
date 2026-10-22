"""Administrator host inventory and deletion of a host's hub-side projection."""

from __future__ import annotations

import logging
import stat
from collections import defaultdict
from pathlib import Path

from flask import abort, jsonify
from sqlalchemy import func, or_, select

from .models import Asset, Command, Event, Host, Interaction, Item, Project, Snapshot, Thread, db
from .transport import emit

log = logging.getLogger(__name__)
BATCH_SIZE = 400


def _ids(value, key="id"):
    if not isinstance(value, list):
        return set()
    return {
        entry[key] for entry in value if isinstance(entry, dict) and isinstance(entry.get(key), str)
    }


def _references():
    """Read only small metadata fragments, never full native input/base64 JSON."""
    commands = db.session.query(
        Command.host_id,
        Command.thread_id,
        Command.payload["_assets"],
        Command.payload["_references"],
    ).yield_per(BATCH_SIZE)
    for host_id, thread_id, assets, references in commands:
        yield host_id, thread_id, _ids(assets), _ids(references, "snapshot_id")
    items = (
        db.session.query(
            Thread.host_id, Item.thread_id, Item.data["_assets"], Item.data["_references"]
        )
        .join(Thread, Item.thread_id == Thread.id)
        .yield_per(BATCH_SIZE)
    )
    for host_id, thread_id, assets, references in items:
        yield host_id, thread_id, _ids(assets), _ids(references, "snapshot_id")


def _batches(values):
    values = iter(values)
    while True:
        batch = []
        for _ in range(BATCH_SIZE):
            try:
                batch.append(next(values))
            except StopIteration:
                break
        if not batch:
            return
        yield batch


def _host_data():
    data = defaultdict(
        lambda: {
            "projects": 0,
            "threads": 0,
            "messages": 0,
            "attachments": 0,
            "attachment_bytes": 0,
        }
    )
    for host_id, count in db.session.query(Project.host_id, func.count(Project.id)).group_by(
        Project.host_id
    ):
        data[host_id]["projects"] = count
    for host_id, count in db.session.query(Thread.host_id, func.count(Thread.id)).group_by(
        Thread.host_id
    ):
        data[host_id]["threads"] = count
    for host_id, count in (
        db.session.query(Thread.host_id, func.count(Item.id))
        .join(Item, Item.thread_id == Thread.id)
        .group_by(Thread.host_id)
    ):
        data[host_id]["messages"] = count
    references = defaultdict(set)
    for host_id, _thread_id, assets, _snapshots in _references():
        for asset_id in assets:
            references[asset_id].add(host_id)
    assets = (
        db.session.query(Asset.id, Asset.host_id, Thread.host_id, Asset.size)
        .outerjoin(Thread, Asset.thread_id == Thread.id)
        .yield_per(BATCH_SIZE)
    )
    for asset_id, host_id, thread_host, size in assets:
        owners = references.pop(asset_id, set())
        owners.update(value for value in (host_id, thread_host) if value)
        for owner in owners:
            data[owner]["attachments"] += 1
            data[owner]["attachment_bytes"] += size
    return data


def _purge(host_id):
    thread_ids = set(db.session.scalars(select(Thread.id).where(Thread.host_id == host_id)))
    thread_query = select(Thread.id).where(Thread.host_id == host_id)
    projects = select(Project.id).where(Project.host_id == host_id)
    target_commands = or_(Command.host_id == host_id, Command.thread_id.in_(thread_query))
    target_assets = set()
    other_assets = set()
    target_snapshots = set()
    other_snapshots = set()
    for owner, thread_id, assets, snapshots in _references():
        if owner == host_id or thread_id in thread_ids:
            target_assets.update(assets)
            target_snapshots.update(snapshots)
        else:
            other_assets.update(assets)
            other_snapshots.update(snapshots)
    target_assets.update(
        db.session.scalars(
            select(Asset.id).where(or_(Asset.host_id == host_id, Asset.thread_id.in_(thread_query)))
        )
    )
    files = []
    for batch in _batches(target_assets):
        for asset in Asset.query.filter(Asset.id.in_(batch)).all():
            shared = (
                asset.id in other_assets
                or (asset.host_id is not None and asset.host_id != host_id)
                or (asset.thread_id is not None and asset.thread_id not in thread_ids)
            )
            if shared:
                if asset.host_id == host_id:
                    asset.host_id = None
                if asset.thread_id in thread_ids:
                    asset.thread_id = None
            else:
                files.append((asset.id, asset.path))
                db.session.delete(asset)
    db.session.flush()
    # Allocate a new global cursor before deleting any old event, including the
    # current maximum. SQLite without AUTOINCREMENT otherwise reuses its IDs.
    marker = emit("anywhere/host_deleted", {"host_id": host_id})
    db.session.flush()
    marker_id = marker.id
    Event.query.filter(or_(Event.host_id == host_id, Event.thread_id.in_(thread_query))).delete(
        synchronize_session=False
    )
    Interaction.query.filter(
        or_(Interaction.host_id == host_id, Interaction.thread_id.in_(thread_query))
    ).delete(synchronize_session=False)
    for batch in _batches(target_snapshots - other_snapshots):
        Snapshot.query.filter(Snapshot.id.in_(batch)).delete(synchronize_session=False)
    Snapshot.query.filter(Snapshot.thread_id.in_(thread_query)).delete(synchronize_session=False)
    Item.query.filter(Item.thread_id.in_(thread_query)).delete(synchronize_session=False)
    Command.query.filter(target_commands).delete(synchronize_session=False)
    Thread.query.filter(Thread.host_id != host_id, Thread.project_id.in_(projects)).update(
        {Thread.project_id: None}, synchronize_session=False
    )
    Thread.query.filter_by(host_id=host_id).delete(synchronize_session=False)
    Project.query.filter_by(host_id=host_id).delete(synchronize_session=False)
    Host.query.filter_by(id=host_id).delete(synchronize_session=False)
    db.session.commit()
    return thread_ids, files, marker_id


def _clean_files(directory, files):
    result = {"deleted": 0, "missing": 0, "retained": 0, "failed": 0}
    if not files:
        return result
    root = Path(directory).resolve()
    remaining = set()
    for (raw_path,) in db.session.query(Asset.path).yield_per(BATCH_SIZE):
        try:
            path = Path(raw_path)
            remaining.add((path if path.is_absolute() else root / path).resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    for asset_id, raw_path in files:
        try:
            path = Path(raw_path)
            path = path if path.is_absolute() else root / path
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or path.is_symlink():
                raise ValueError("unsafe attachment path")
            if resolved in remaining:
                result["retained"] += 1
                continue
            if not stat.S_ISREG(resolved.lstat().st_mode):
                raise ValueError("attachment is not a regular file")
            resolved.unlink()
            result["deleted"] += 1
        except FileNotFoundError:
            result["missing"] += 1
        except (OSError, ValueError, RuntimeError) as exc:
            result["failed"] += 1
            log.warning(
                "Deleted host attachment cleanup failed for asset %s (%s)",
                asset_id,
                type(exc).__name__,
            )
    return result


def register_host_routes(app, json_body, identifier):
    @app.get("/api/settings/hosts")
    def settings_hosts():
        with app.extensions["broker"].command_lock:
            data = _host_data()
            hosts = [
                {**host.as_dict(app.extensions["broker"].is_online(host.id)), "data": data[host.id]}
                for host in Host.query.order_by(Host.name, Host.id)
            ]
        return jsonify(hosts=hosts)

    @app.delete("/api/hosts/<host_id>")
    def delete_host(host_id):
        host_id = identifier(host_id, "host_id")
        body = json_body()
        with app.extensions["broker"].command_lock:
            host = db.session.get(Host, host_id)
            if host is None:
                abort(404, "节点不存在")
            if not isinstance(body.get("confirm_name"), str) or body["confirm_name"] != host.name:
                raise ValueError("请准确输入当前机器名称以确认删除")
            thread_ids, files, _marker = _purge(host_id)
            app.extensions["broker"].revoke(host_id)
            app.extensions["writer_sessions"].forget(thread_ids)
            cleanup = _clean_files(app.config["ASSET_DIR"], files)
        result = {"deleted": True, "host_id": host_id, "cleanup": cleanup}
        if cleanup["failed"]:
            result["warning"] = (
                f"机器记录已删除，{cleanup['failed']} 个附件文件未能清理，请检查 Hub 日志"
            )
        return jsonify(result)
