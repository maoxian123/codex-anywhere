"""Register native projects and safely complete the desktop sidebar projection."""

from __future__ import annotations

import asyncio
import hashlib
import math

from .desktop_state import DesktopState, directory_key
from .runtime import RpcError, RuntimeGone
from .store import NodeStore

MAX_PROJECT_PAGES = 100
RPC_TIMEOUT_SECONDS = 15


def _queue_key(cwd: str) -> str:
    return hashlib.sha256(directory_key(cwd).encode("utf-8")).hexdigest()


def _valid_project(project: object) -> bool:
    if not isinstance(project, dict):
        return False
    roots = project.get("roots")
    return (
        isinstance(project.get("id"), str) and bool(project["id"])
        and isinstance(project.get("name"), str)
        and isinstance(roots, list) and bool(roots)
        and all(isinstance(root, dict) and isinstance(root.get("path"), str)
                and bool(root["path"]) for root in roots)
        and all(type(project.get(key)) in {int, float} and math.isfinite(project[key])
                for key in ("createdAt", "updatedAt"))
    )


def _matches(project: dict, cwd: str) -> bool:
    target = directory_key(cwd)
    return any(directory_key(root["path"]) == target for root in project["roots"])


class DesktopProjects:
    def __init__(self, store: NodeStore, state: DesktopState | None = None):
        self.store = store
        self.state = state if state is not None else DesktopState()
        self._lock = asyncio.Lock()

    async def register(self, runtime, cwd: str, name: str) -> dict:
        """An explicit request may recover a previous uncertain registration."""
        async with self._lock:
            check = await asyncio.to_thread(self.state.check, cwd)
            key = _queue_key(cwd)
            if check["status"] not in {"ready", "pending"}:
                if self.store.desktop_project(key):
                    status = "complete" if check["status"] == "already_registered" else "error"
                    self.store.update_desktop_project(key, status, result=check)
                return check
            if check["status"] == "ready" and runtime is None:
                return {"status": "unavailable", "reason": "runtime_unavailable"}
            row = self.store.queue_desktop_project(key, cwd, name)
            if check["status"] == "pending":
                self.store.update_desktop_project(key, "pending", result=check)
                return check
            return await self._complete(runtime, row)

    async def flush(self, runtime) -> None:
        """Only queued known-safe work runs automatically, independently of browsers."""
        if runtime is None:
            return
        async with self._lock:
            for row in self.store.pending_desktop_projects():
                check = await asyncio.to_thread(self.state.check, row["cwd"])
                if check["status"] == "ready":
                    await self._complete(runtime, row)
                else:
                    status = (
                        "pending" if check["status"] == "pending" else
                        "complete" if check["status"] == "already_registered" else "error"
                    )
                    self.store.update_desktop_project(row["cwd_key"], status, result=check)
                await asyncio.sleep(0)

    async def _request(self, runtime, method: str, params: dict) -> dict:
        return await asyncio.wait_for(runtime.request(method, params), RPC_TIMEOUT_SECONDS)

    async def _find(self, runtime, cwd: str) -> dict | None:
        cursor = None
        cursors = set()
        for _ in range(MAX_PROJECT_PAGES):
            result = await self._request(runtime, "project/list", {"cursor": cursor, "limit": 100})
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("invalid_response")
            for project in result["data"]:
                if not _valid_project(project):
                    raise ValueError("invalid_response")
                if _matches(project, cwd):
                    return project
            cursor = result.get("nextCursor")
            if cursor is None:
                return None
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("catalog_incomplete")
            cursors.add(cursor)
        raise ValueError("catalog_incomplete")

    async def _complete(self, runtime, row: dict) -> dict:
        key, cwd = row["cwd_key"], row["cwd"]
        project = row["native_project"]
        creating = False
        self.store.update_desktop_project(key, "started")
        try:
            if project is not None:
                if not _valid_project(project) or not _matches(project, cwd):
                    raise ValueError("invalid_response")
            else:
                project = await self._find(runtime, cwd)
                if project is None:
                    creating = True
                    result = await self._request(runtime, "project/create", {
                        "name": row["name"], "roots": [{"path": cwd}],
                        "metadata": {}, "idempotencyKey": "codex-anywhere:" + key,
                    })
                    project = result.get("project") if isinstance(result, dict) else None
                    if not _valid_project(project) or not _matches(project, cwd):
                        return self._finish(key, "unknown", {
                            "status": "error", "reason": "outcome_unknown",
                        })
            # Persist the known native result before touching the separate desktop
            # cache. A deferred merge or node restart must never repeat creation.
            self.store.update_desktop_project(key, "pending", native_project=project)
            result = await asyncio.to_thread(self.state.merge, cwd, project)
            status = (
                "pending" if result["status"] == "pending" else
                "complete" if result["status"] in {"registered", "already_registered"} else "error"
            )
            return self._finish(key, status, result)
        except RpcError as exc:
            result = (
                {"status": "unavailable", "reason": "native_unsupported"}
                if exc.code in {-32601, -32602} else
                {"status": "error", "reason": "registration_failed"}
            )
            return self._finish(key, "error", result)
        except (RuntimeGone, TimeoutError):
            result = (
                {"status": "error", "reason": "outcome_unknown"} if creating else
                {"status": "unavailable", "reason": "runtime_unavailable"}
            )
            return self._finish(key, "unknown" if creating else "error", result)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "registration_failed"
            if reason not in {"invalid_response", "catalog_incomplete"}:
                reason = "registration_failed"
            return self._finish(key, "error", {"status": "error", "reason": reason})

    def _finish(self, key: str, status: str, result: dict) -> dict:
        self.store.update_desktop_project(key, status, result=result)
        return result
