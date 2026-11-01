"""Register native projects in a stopped desktop's existing sidebar state.

The desktop caches this file in memory and does not reload external edits. Never
merge while a same-account desktop may be running. The native project remains
authoritative; this module only adds the desktop's separate sidebar projection.
"""

from __future__ import annotations

import json
import math
import ntpath
import os
import posixpath
import re
import stat
import tempfile
from pathlib import Path

import psutil

from codex_anywhere.node.ownership import _try_lock, _unlock

MAX_STATE_BYTES = 16 * 1024 * 1024


class _InvalidState(ValueError):
    pass


class _StateChanged(OSError):
    pass


def _native_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Invalid directory")
    if path[:8].casefold() == "\\\\?\\unc\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    windows = (
        bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")
        or (os.name == "nt" and path.startswith("//"))
    )
    if windows:
        return ntpath.normpath(path)
    if not posixpath.isabs(path):
        raise ValueError("An absolute directory is required")
    return posixpath.normpath(path)


def directory_key(path: str) -> str:
    """Compare Windows paths on any host without interpreting POSIX paths there."""
    normalized = _native_path(path)
    if re.match(r"^[A-Za-z]:[\\/]", normalized) or normalized.startswith("\\\\"):
        return ntpath.normcase(normalized)
    return normalized


def _result(status: str, reason: str | None = None, **extra) -> dict:
    return {"status": status, **({"reason": reason} if reason else {}), **extra}


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _read_regular(path: Path) -> bytes:
    before = path.lstat()
    if not _regular(before):
        raise _InvalidState("State must be a regular file")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        if not _regular(opened) or (before.st_dev, before.st_ino) != (
            opened.st_dev, opened.st_ino
        ):
            raise _StateChanged()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            contents = stream.read(MAX_STATE_BYTES + 1)
        if len(contents) > MAX_STATE_BYTES:
            raise _InvalidState()
        after = path.lstat()
        if not _regular(after) or (opened.st_dev, opened.st_ino) != (
            after.st_dev, after.st_ino
        ):
            raise _StateChanged()
        return contents
    finally:
        os.close(fd)


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidState("Duplicate state key")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise _InvalidState()


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate(contents: bytes) -> dict:
    try:
        state = json.loads(
            contents, object_pairs_hook=_json_object, parse_constant=_invalid_constant
        )
        if not isinstance(state, dict):
            raise _InvalidState()
        if "local-projects" not in state:
            roots = state.get("electron-saved-workspace-roots")
            if isinstance(roots, list) and all(isinstance(root, str) for root in roots):
                raise LookupError("unsupported_state")
            raise _InvalidState()
        projects, order = state["local-projects"], state.get("project-order")
        if not isinstance(projects, dict) or not isinstance(order, list):
            raise _InvalidState()
        if not all(isinstance(item, str) for item in order):
            raise _InvalidState()
        for key, project in projects.items():
            if not isinstance(project, dict) or not isinstance(project.get("id"), str):
                raise _InvalidState()
            if project["id"] != key or not isinstance(project.get("name"), str):
                raise _InvalidState()
            roots = project.get("rootPaths")
            if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
                raise _InvalidState()
            if not _number(project.get("createdAt")) or not _number(project.get("updatedAt")):
                raise _InvalidState()
        return state
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise _InvalidState() from exc


def _contains(state: dict, key: str) -> bool:
    for project in state["local-projects"].values():
        for root in project["rootPaths"]:
            try:
                if directory_key(root) == key:
                    return True
            except ValueError:
                continue
    return False


def _desktop_executable(executable: str) -> bool:
    path = Path(executable)
    if path.name.casefold() not in {"codex", "codex.exe", "chatgpt", "chatgpt.exe"}:
        return False
    parent = path.parent
    if parent.name.casefold() == "macos" and parent.parent.name.casefold() == "contents":
        bundle = parent.parent / "Resources" / "app.asar"
    else:
        bundle = parent / "resources" / "app.asar"
    try:
        return stat.S_ISREG(bundle.stat().st_mode)
    except FileNotFoundError:
        return False


class DesktopState:
    def __init__(self, codex_home: Path | None = None):
        self.home = Path(
            codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser().absolute()
        self.path = self.home / ".codex-global-state.json"

    def _desktop_running(self) -> bool | None:
        """Unknown access to an identifiable desktop blocks; unrelated processes do not."""
        try:
            username = psutil.Process().username().casefold()
            unknown = False
            for process in psutil.process_iter(["name", "exe", "username"], ad_value=None):
                info = process.info
                name = (info.get("name") or "").casefold()
                executable = info.get("exe")
                owner = info.get("username")
                if owner and owner.casefold() != username:
                    continue
                try:
                    if executable:
                        if not _desktop_executable(executable):
                            continue
                    elif name not in {"codex", "codex.exe", "chatgpt", "chatgpt.exe"}:
                        continue
                    else:
                        unknown = True
                        continue
                except OSError:
                    unknown = True
                    continue
                if owner:
                    return True
                unknown = True
            return None if unknown else False
        except (OSError, psutil.Error):
            return None

    def _running_result(self) -> dict | None:
        running = self._desktop_running()
        if running is not False:
            return _result(
                "pending", "desktop_running" if running else "desktop_state_unverified"
            )
        return None

    def _load(self) -> tuple[bytes, dict]:
        if self.home.is_symlink() or not self.home.is_dir():
            if not self.home.exists():
                raise FileNotFoundError()
            raise _InvalidState()
        contents = _read_regular(self.path)
        return contents, _validate(contents)

    def _unchanged(self, original: bytes) -> None:
        try:
            if _read_regular(self.path) != original:
                raise _StateChanged()
        except FileNotFoundError as exc:
            raise _StateChanged() from exc

    @staticmethod
    def _failure(exc: Exception) -> dict:
        if isinstance(exc, FileNotFoundError):
            return _result("unavailable", "desktop_not_found")
        if isinstance(exc, _StateChanged):
            return _result("pending", "state_changed")
        if isinstance(exc, _InvalidState):
            return _result("error", "state_invalid")
        if isinstance(exc, LookupError):
            return _result("unavailable", "unsupported_state")
        return _result("error", "state_io_failed")

    def check(self, cwd: str) -> dict:
        try:
            key = directory_key(cwd)
        except ValueError:
            return _result("error", "directory_invalid")
        try:
            _, state = self._load()
            if _contains(state, key):
                return _result("already_registered", refresh_required=False)
            return self._running_result() or _result("ready")
        except (OSError, ValueError, LookupError) as exc:
            return self._failure(exc)

    def _temp(self, contents: bytes) -> Path:
        fd, name = tempfile.mkstemp(prefix=".codex-anywhere-state-", suffix=".tmp", dir=self.home)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o600)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _replaceable(path: Path) -> None:
        try:
            if not _regular(path.lstat()):
                raise _InvalidState()
        except FileNotFoundError:
            pass

    def merge(self, cwd: str, project: dict) -> dict:
        try:
            key = directory_key(cwd)
        except ValueError:
            return _result("error", "directory_invalid")
        lock_fd = None
        locked = False
        temporary: list[Path] = []
        try:
            original, state = self._load()
            if _contains(state, key):
                return _result("already_registered", refresh_required=False)
            if result := self._running_result():
                return result
            try:
                if not isinstance(project, dict) or not isinstance(project["roots"], list):
                    raise ValueError()
                roots = [_native_path(root["path"]) for root in project["roots"]]
                project_id = project["id"]
                if (
                    not isinstance(project_id, str) or not project_id
                    or not isinstance(project["name"], str)
                    or key not in {directory_key(root) for root in roots}
                    or not _number(project["createdAt"]) or not _number(project["updatedAt"])
                ):
                    raise ValueError()
                entry = {
                    "id": project_id, "name": project["name"], "rootPaths": roots,
                    "createdAt": project["createdAt"] * 1000,
                    "updatedAt": project["updatedAt"] * 1000,
                }
                if not _number(entry["createdAt"]) or not _number(entry["updatedAt"]):
                    raise ValueError()
            except (KeyError, TypeError, ValueError, OverflowError):
                return _result("error", "project_invalid")
            if project_id in state["local-projects"]:
                return _result("error", "state_conflict")
            lock_path = self.home / ".codex-anywhere-desktop-state.lock"
            self._replaceable(lock_path)
            lock_fd = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            opened = os.fstat(lock_fd)
            current = lock_path.lstat()
            if not _regular(opened) or not _regular(current):
                raise _InvalidState()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise _StateChanged()
            locked = _try_lock(lock_fd)
            if not locked:
                return _result("pending", "state_changed")
            self._unchanged(original)
            state["local-projects"][project_id] = entry
            state["project-order"] = [
                project_id, *(item for item in state["project-order"] if item != project_id)
            ]
            encoded = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
            new_path = self._temp(encoded)
            temporary.append(new_path)
            backup = Path(f"{self.path}.codex-anywhere.bak")
            self._replaceable(backup)
            if result := self._running_result():
                return result
            self._unchanged(original)
            backup_path = self._temp(original)
            temporary.append(backup_path)
            self._replaceable(backup)
            os.replace(backup_path, backup)
            if result := self._running_result():
                return result
            self._unchanged(original)
            os.replace(new_path, self.path)
            readback = _read_regular(self.path)
            if readback != encoded or not _contains(_validate(readback), key):
                raise _StateChanged()
            return _result("registered", refresh_required=True)
        except (OSError, ValueError, LookupError) as exc:
            return self._failure(exc)
        finally:
            for path in temporary:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    if locked:
                        _unlock(lock_fd)
                finally:
                    os.close(lock_fd)
