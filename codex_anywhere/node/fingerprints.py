"""Read-only history invalidation, including changes within one native second."""

from __future__ import annotations

import hashlib
import json
import os


def history_fingerprint(thread: dict) -> str:
    path = thread.get("path") or thread.get("rolloutPath")
    source = None
    if isinstance(path, str) and path and "\x00" not in path:
        try:
            stat = os.stat(path)
            source = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        except OSError:
            pass  # Optional acceleration; RPC metadata remains the baseline.
    snapshot = {
        "id": thread.get("id"),
        "updatedAt": thread.get("updatedAt"),
        "path": path,
        "source": source,
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
