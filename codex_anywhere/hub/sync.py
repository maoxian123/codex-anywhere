"""Project bounded local catalogs without fetching message bodies or starting Codex."""

from __future__ import annotations

import math
import ntpath
import posixpath
import time

from .models import Thread, db
from .projects import ensure_project

CATALOG_LIMIT = 50
CATALOG_INTERVAL = 60
PROTECTED_STATES = {"active", "waiting", "starting", "importing"}


def native_version(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 < value < 253402300799:
            return float(value)
    return None


def _valid_metadata(value):
    if not isinstance(value, dict):
        return False
    native_id, cwd = value.get("id"), value.get("cwd")
    if not isinstance(native_id, str) or not 1 <= len(native_id) <= 200 or "\x00" in native_id:
        return False
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return False
    pathmod = ntpath if ntpath.splitdrive(cwd)[0] else posixpath
    return pathmod.isabs(cwd)


def project_catalog(host, params):
    """Apply metadata only. Leaving the recent window never deletes cached history."""
    entries = params.get("threads")
    if not isinstance(entries, list):
        raise ValueError("Automatic catalog must contain a thread list")
    seen = set()
    invalid = 0
    now = time.time()
    for native in entries[:CATALOG_LIMIT]:
        if not _valid_metadata(native):
            invalid += 1
            continue
        native_id = native["id"]
        if native_id in seen:
            continue
        seen.add(native_id)
        version = native_version(native.get("updatedAt"))
        fingerprint = native.get("_history_fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            fingerprint = None
        thread = Thread.query.filter_by(host_id=host.id, native_id=native_id).first()
        if thread is not None and version is not None and thread.native_updated_at is not None:
            if version < thread.native_updated_at:
                continue
        if thread is None:
            thread = Thread(
                host_id=host.id,
                native_id=native_id,
                status="idle",
                imported=True,
                history_loaded=False,
                history_stale=False,
                cwd=native["cwd"],
                title="Codex conversation",
                created_at=native_version(native.get("createdAt")) or version or now,
                updated_at=version or now,
            )
            db.session.add(thread)
        if version is not None:
            thread.native_updated_at = version
        if fingerprint:
            thread.native_fingerprint = fingerprint
        # Discovery must not relabel/move a running task or clear its diagnostic.
        if thread.status not in PROTECTED_STATES:
            title = native.get("name") or native.get("title") or native.get("preview")
            if isinstance(title, str) and title.strip():
                thread.title = title.strip()[:240]
            thread.cwd = native["cwd"]
            thread.project_id = ensure_project(host, thread.cwd).id
            if version is not None:
                thread.updated_at = max(thread.updated_at or 0, version)
            if thread.history_loaded and version is not None:
                if (
                    thread.history_native_updated_at is None
                    or version > thread.history_native_updated_at
                ):
                    thread.history_stale = True
            if (
                thread.history_loaded
                and fingerprint
                and thread.history_fingerprint
                and fingerprint != thread.history_fingerprint
            ):
                thread.history_stale = True
    host.last_sync_at = now
    host.sync_error = f"Skipped {invalid} invalid local conversation entries" if invalid else None


def project_directories(host, params):
    """Older inventory contributes directories, never downloads/creates thread bodies."""
    projects = params.get("projects")
    if not isinstance(projects, list) or len(projects) > 100:
        raise ValueError("Invalid background project inventory")
    for entry in projects:
        if isinstance(entry, dict) and _valid_metadata({"id": "project", "cwd": entry.get("cwd")}):
            ensure_project(host, entry["cwd"])
