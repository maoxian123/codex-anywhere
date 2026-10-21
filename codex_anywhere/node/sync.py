"""Negotiated, bounded background discovery of native Codex thread metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .fingerprints import history_fingerprint
from .history import list_threads
from .runtime import CodexRuntime, RpcError, RuntimeGone
from .store import NodeStore

log = logging.getLogger(__name__)
MAX_PENDING_EVENTS = 100
MAX_ERROR_CHARS = 2000
BACKPRESSURE_DELAY = 5.0
FOREGROUND_DELAY = 1.0


@dataclass(frozen=True)
class SyncSettings:
    recent_limit: int = 50
    interval_seconds: int = 60
    include_projects: bool = False


def negotiated_settings(welcome: dict) -> SyncSettings | None:
    capabilities = welcome.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    config = capabilities.get("catalogSync")
    if not isinstance(config, dict):
        return None
    limit = config.get("recentLimit", 50)
    interval = config.get("intervalSeconds", 60)
    if type(limit) is not int or not 1 <= limit <= 100:
        return None
    if type(interval) is not int or not 5 <= interval <= 3600:
        return None
    return SyncSettings(limit, interval, config.get("includeProjects") is True)


class CatalogSynchronizer:
    def __init__(
        self,
        runtime: CodexRuntime,
        store: NodeStore,
        wake: Callable[[], None],
        settings: SyncSettings,
        *,
        is_current: Callable[[], bool],
        foreground_busy: Callable[[], bool],
    ):
        self.runtime = runtime
        self.store = store
        self.wake = wake
        self.settings = settings
        self.is_current = is_current
        self.foreground_busy = foreground_busy
        self.fingerprints: dict[str, str] = {}
        self._pending_sync_event: str | None = None
        self._initial_catalog = asyncio.Event()

    def _backpressured(self) -> bool:
        # Count through the existing partial index without loading image-bearing
        # event bodies merely to check the connection's bounded window.
        pending = self.store.db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM events WHERE acknowledged=0 LIMIT ?)",
            (MAX_PENDING_EVENTS,),
        ).fetchone()[0]
        if pending >= MAX_PENDING_EVENTS:
            return True
        if self._pending_sync_event:
            row = self.store.db.execute(
                "SELECT acknowledged FROM events WHERE event_id=?", (self._pending_sync_event,)
            ).fetchone()
            if row and not row[0]:
                return True
        return False

    def _record(self, method: str, params: dict) -> None:
        event = self.store.record_event(self.runtime.runtime_id, method, params)
        self._pending_sync_event = event["event_id"]
        self.wake()

    async def scan_once(self) -> bool:
        """Return false when discovery yields to foreground work or backpressure."""
        if not self.is_current() or self.foreground_busy() or self._backpressured():
            return False
        catalog = await list_threads(
            self.runtime,
            {
                "limit": self.settings.recent_limit,
                "archived": False,
                "indexed": self._initial_catalog.is_set(),
            },
        )
        if not self.is_current():
            return False
        threads = catalog.get("data")
        if not isinstance(threads, list):
            raise ValueError("Codex returned an invalid thread catalog")
        threads = threads[: self.settings.recent_limit]
        changed = []
        current = {}
        for thread in threads:
            if (
                not isinstance(thread, dict)
                or not isinstance(thread.get("id"), str)
                or not thread["id"]
            ):
                raise ValueError("Codex returned invalid thread metadata")
            metadata = {key: value for key, value in thread.items() if key != "turns"}
            metadata["_history_fingerprint"] = history_fingerprint(thread)
            fingerprint = hashlib.sha256(
                json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            current[metadata["id"]] = fingerprint
            if self.fingerprints.get(metadata["id"]) != fingerprint:
                changed.append(metadata)
        # A foreground turn/import can fill the outbox while this read was in
        # flight. Discard this snapshot and try later rather than adding load.
        if self._backpressured():
            return False
        self._record(
            "anywhere/catalog_sync",
            {
                "threads": changed,
                "scanned_count": len(threads),
                "limit": self.settings.recent_limit,
                "syncedAt": time.time(),
            },
        )
        # Bound memory to the current recent window. Departures do not emit
        # deletion events; the hub retains conversations outside this window.
        self.fingerprints = current
        self._initial_catalog.set()
        return True

    async def run(self) -> None:
        if self.settings.include_projects:
            await asyncio.gather(self._run_recent(), self._projects_after_initial_catalog())
        else:
            await self._run_recent()

    async def _projects_after_initial_catalog(self):
        await self._initial_catalog.wait()
        await self._run_projects()

    async def _run_projects(self) -> None:
        """Discover older/archived project directories without importing their tasks."""
        cursor = None
        archived = False
        seen = set()
        pages = 0
        while self.is_current() and pages < 1000 and len(seen) < 10_000:
            if self.foreground_busy() or self._backpressured():
                await asyncio.sleep(FOREGROUND_DELAY)
                continue
            try:
                payload = {"limit": 100, "archived": archived, "indexed": True}
                if cursor:
                    payload["cursor"] = cursor
                catalog = await list_threads(self.runtime, payload)
                if not self.is_current():
                    return
                if self.foreground_busy() or self._backpressured():
                    await asyncio.sleep(FOREGROUND_DELAY)
                    continue  # No cursor advance until its event is durable.
                projects = []
                for thread in catalog.get("data", []):
                    cwd = thread.get("cwd") if isinstance(thread, dict) else None
                    if isinstance(cwd, str) and os.path.isabs(cwd) and "\x00" not in cwd:
                        key = os.path.normcase(os.path.normpath(cwd))
                        if key not in seen:
                            projects.append({"cwd": cwd})
                            seen.add(key)
                next_cursor = catalog.get("nextCursor")
                if next_cursor is not None and (
                    not isinstance(next_cursor, str) or next_cursor == cursor
                ):
                    raise ValueError("Invalid project catalog pagination cursor")
                self._record(
                    "anywhere/project_catalog",
                    {
                        "projects": projects,
                        "syncedAt": time.time(),
                        "complete": archived and not next_cursor,
                    },
                )
                pages += 1
                cursor = next_cursor
                if not cursor:
                    if archived:
                        return
                    archived = True
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - optional inventory must not stop recent discovery
                log.warning("Background project discovery paused; it will restart on reconnect")
                return

    async def _run_recent(self) -> None:
        while self.is_current():
            # A newly accepted connection may already have foreground commands
            # queued. Let its reader dispatch them before starting discovery.
            await asyncio.sleep(0)
            if self.foreground_busy():
                await asyncio.sleep(FOREGROUND_DELAY)
                continue
            try:
                scanned = await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - discovery must never end the chat connection
                if self.is_current():
                    if isinstance(error, (RpcError, RuntimeGone, ValueError, OSError)):
                        detail = str(error).strip() or type(error).__name__
                    else:
                        detail = f"Catalog synchronization failed ({type(error).__name__})"
                    try:
                        if not self._backpressured():
                            self._record(
                                "anywhere/catalog_sync_error", {"error": detail[:MAX_ERROR_CHARS]}
                            )
                    except Exception:  # noqa: BLE001 - persistence can fail independently
                        log.error("Could not persist catalog synchronization status")
                scanned = True
            await asyncio.sleep(self.settings.interval_seconds if scanned else BACKPRESSURE_DELAY)
