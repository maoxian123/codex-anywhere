"""Inspect native writer locks without loading, deleting, or creating a thread lock.

Codex serializes lock-file creation/removal with .coordination.lock. Probing must
use that same lock: otherwise an apparently idle inode can be unlinked while a
new writer opens another inode at the same path. A successful probe is only a
snapshot; thread/resume remains the authoritative acquisition operation.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Iterator

import psutil


@dataclass(frozen=True)
class _Identity:
    pid: int
    started_at: float
    name: str
    executable: str
    username: str

    def public(self) -> dict:
        return {"pid": self.pid, "name": self.name, "started_at": self.started_at}


@dataclass(frozen=True)
class _Owners:
    identities: tuple[_Identity, ...]
    kind: str
    complete: bool
    reason: str | None = None


@dataclass(frozen=True)
class _Grant:
    thread_id: str
    file_key: tuple[int, int]
    identities: tuple[_Identity, ...]
    kind: str
    expires_at: float


_LOCK_LINE = re.compile(
    r"^\s*(?:lock:\s*)?\d+:\s+(FLOCK|POSIX|OFDLCK)\s+\S+\s+"
    r"(WRITE|READ)\s+(-?\d+)\s+([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)\s+(\S+)\s+(\S+)"
)


def _parse_linux_lock(line: str) -> tuple[str, str, int, tuple[int, int], str, str] | None:
    """Ignore blocked waiters (``->``); they do not hold the lock."""
    match = _LOCK_LINE.match(line)
    if not match:
        return None
    kind, mode, pid, major, minor, inode, start, end = match.groups()
    device = os.makedev(int(major, 16), int(minor, 16))
    return kind, mode, int(pid), (device, int(inode)), start, end


def _try_lock(fd: int) -> bool:
    """Match Rust std::fs::File::try_lock (flock / LockFileEx)."""
    if os.name != "nt":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    kernel, overlapped, handle = _windows_lock_args(fd)
    if kernel.LockFileEx(handle, 3, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
        return True
    error = ctypes.get_last_error()
    if error == 33:  # ERROR_LOCK_VIOLATION
        return False
    raise ctypes.WinError(error)


def _unlock(fd: int) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    kernel, overlapped, handle = _windows_lock_args(fd)
    if not kernel.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_lock_args(fd: int):
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(Overlapped),
    ]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    return kernel, Overlapped(), msvcrt.get_osfhandle(fd)


def _restart_manager_candidates(path: Path) -> dict[int, float]:
    # RM can wait on a system registry mutex. A private helper process gives
    # this otherwise synchronous API a hard timeout without stranding threads.
    # Use the base interpreter: Windows venv launchers create another process,
    # which subprocess timeout would otherwise leave running.
    script = (
        "import json,sys; sys.path[:]=json.loads(sys.argv[1]); "
        "from pathlib import Path; "
        "from codex_anywhere.node.ownership import _restart_manager_query; "
        "print(json.dumps(_restart_manager_query(Path(sys.argv[2]))))"
    )
    try:
        completed = subprocess.run(
            [getattr(sys, "_base_executable", sys.executable), "-c", script,
             json.dumps(sys.path), str(path)],
            capture_output=True, text=True, timeout=2, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {int(pid): float(started) for pid, started in json.loads(completed.stdout).items()}
    except (subprocess.SubprocessError, ValueError) as exc:
        raise OSError("Windows writer resource inspection timed out or failed; inspect again") from exc


def _restart_manager_query(path: Path) -> dict[int, float]:
    """Return resource users, never claim Restart Manager proves lock ownership.

    Start times are supplied by Windows, and are checked against psutil before a
    candidate can be offered for explicitly confirmed process termination.
    """
    from ctypes import wintypes

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", UniqueProcess), ("app_name", wintypes.WCHAR * 256),
            ("service_name", wintypes.WCHAR * 64), ("application_type", ctypes.c_int),
            ("app_status", wintypes.ULONG), ("session_id", wintypes.DWORD),
            ("restartable", wintypes.BOOL),
        ]

    manager = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
    manager.RmStartSession.argtypes = [
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR,
    ]
    manager.RmRegisterResources.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.POINTER(UniqueProcess),
        wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
    ]
    manager.RmGetList.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(ProcessInfo), ctypes.POINTER(wintypes.DWORD),
    ]
    manager.RmEndSession.argtypes = [wintypes.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(manager, name).restype = wintypes.DWORD
    session = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(33)
    error = manager.RmStartSession(ctypes.byref(session), 0, session_key)
    if error:
        raise OSError(error, "Restart Manager could not start")
    try:
        paths = (wintypes.LPCWSTR * 1)(str(path))
        error = manager.RmRegisterResources(session, 1, paths, 0, None, 0, None)
        if error:
            raise OSError(error, "Restart Manager could not register writer lock")
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        for _ in range(4):
            entries = (ProcessInfo * count.value)() if count.value else None
            error = manager.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(count), entries,
                ctypes.byref(reasons),
            )
            if error == 234:  # ERROR_MORE_DATA: resource users changed during the query.
                if needed.value > 512:
                    raise OSError("Too many writer resource users to verify safely")
                count.value = needed.value
                continue
            if error:
                raise OSError(error, "Restart Manager could not identify resource users")
            result = {}
            for entry in (entries or ())[:count.value]:
                started = entry.process.started
                ticks = (started.dwHighDateTime << 32) | started.dwLowDateTime
                result[entry.process.pid] = ticks / 10_000_000 - 11_644_473_600
            return result
        raise OSError("Writer lock resource users kept changing; inspect again")
    finally:
        manager.RmEndSession(session)


class WriterOwnership:
    TOKEN_TTL_SECONDS = 60.0
    COORDINATION_TIMEOUT_SECONDS = 1.0
    TERMINATION_TIMEOUT_SECONDS = 5.0

    def __init__(self, codex_home: Path | None = None):
        self.home = Path(
            codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser().resolve()
        self.directory = self.home / "thread-writer-locks"
        self._grants: dict[str, _Grant] = {}
        self._mutex = threading.RLock()
        self._username = psutil.Process().username().casefold()

    @staticmethod
    def _thread_id(thread_id: str) -> str:
        try:
            value = str(uuid.UUID(thread_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid native thread ID") from exc
        if value != thread_id.lower():
            raise ValueError("Invalid native thread ID")
        return value

    @contextmanager
    def _coordination(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ".coordination.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        acquired = False
        try:
            deadline = time.monotonic() + self.COORDINATION_TIMEOUT_SECONDS
            while not (acquired := _try_lock(fd)):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex writer coordination is busy; inspect again")
                time.sleep(0.025)
            yield
        finally:
            if acquired:
                _unlock(fd)
            os.close(fd)

    def _probe(self, thread_id: str) -> tuple[bool, tuple[int, int] | None]:
        """Caller holds coordination. Never create or unlink a thread lock."""
        path = self.directory / f"{thread_id}.lock"
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return False, None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise OSError("Writer lock is not a regular file")
            key = (info.st_dev, info.st_ino)
            if not _try_lock(fd):
                return True, key
            _unlock(fd)
            return False, key
        finally:
            os.close(fd)

    @staticmethod
    def _identity(pid: int) -> tuple[_Identity, psutil.Process]:
        process = psutil.Process(pid)
        identity = _Identity(
            pid, process.create_time(), process.name(), process.exe(),
            process.username().casefold(),
        )
        return identity, process

    def _permitted(self, identity: _Identity) -> bool:
        # Node/npm wrappers are deliberately excluded: their names do not prove
        # they are Codex, and stopping them need not stop the native writer.
        name = Path(identity.executable).name.casefold()
        return (
            identity.pid != os.getpid()
            and identity.username == self._username
            and name in {"codex", "codex.exe"}
            and identity.name.casefold() in {"codex", "codex.exe"}
        )

    def _linux_holders(self, file_key: tuple[int, int]) -> tuple[set[int], bool]:
        deadline = time.monotonic() + 2.0
        with Path("/proc/locks").open() as stream:
            contents = stream.read(1_048_577)
        if len(contents) > 1_048_576:
            return set(), False
        records = [
            record for line in contents.splitlines()
            if (record := _parse_linux_lock(line)) and record[3] == file_key
        ]
        if not records or any(
            kind != "FLOCK" or mode != "WRITE" or start != "0" or end != "EOF"
            for kind, mode, pid, _, start, end in records
        ):
            return set(), False
        # /proc/locks records the acquiring PID. fdinfo also identifies inherited
        # and passed copies of the locked open-file description. Include all of
        # them; killing only the original PID may leave the writer lock alive.
        holders: set[int] = set()
        complete = True
        acquiring_pids = {record[2] for record in records if record[2] > 0}
        proc = Path("/proc")
        # Find the kernel-reported acquiring PIDs first, then inherited holders.
        directories = (proc / str(pid) for pid in acquiring_pids)
        acquiring_names = {str(pid) for pid in acquiring_pids}
        remaining = (path for path in proc.iterdir() if path.name not in acquiring_names)
        process_count = descriptor_count = 0
        for process_dir in chain(directories, remaining):
            if not process_dir.name.isdecimal():
                continue
            process_count += 1
            if process_count > 2048 or time.monotonic() >= deadline:
                return holders, False
            try:
                if process_dir.stat().st_uid != os.geteuid():
                    if int(process_dir.name) in acquiring_pids:
                        complete = False
                    continue
                for fdinfo in (process_dir / "fdinfo").iterdir():
                    descriptor_count += 1
                    if descriptor_count > 32768 or time.monotonic() >= deadline:
                        return holders, False
                    try:
                        with fdinfo.open() as stream:
                            contents = stream.read(65537)
                        if len(contents) > 65536:
                            complete = False
                        lines = contents.splitlines()
                    except FileNotFoundError:
                        continue  # Process closed the descriptor during the snapshot.
                    for line in lines:
                        record = _parse_linux_lock(line) if line.startswith("lock:") else None
                        if record and record[0] == "FLOCK" and record[3] == file_key:
                            holders.add(int(process_dir.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                complete = False
        return holders, complete and bool(holders)

    def _owners(self, thread_id: str, file_key: tuple[int, int]) -> _Owners:
        kind = "lock_holder" if sys.platform.startswith("linux") else "candidate"
        if sys.platform.startswith("linux"):
            pids, complete = self._linux_holders(file_key)
            expected_starts = {}
        elif sys.platform == "win32":
            expected_starts = _restart_manager_candidates(self.directory / f"{thread_id}.lock")
            pids, complete = set(expected_starts), bool(expected_starts)
        else:
            return _Owners((), kind, False, "Writer owner identification is unsupported on this OS")
        identities = []
        for pid in sorted(pids):
            try:
                identity, _ = self._identity(pid)
                identities.append(identity)
                if pid in expected_starts and abs(expected_starts[pid] - identity.started_at) > 0.01:
                    complete = False
            except (psutil.Error, OSError):
                complete = False
        reason = None
        if not identities or not complete:
            reason = "The writer owner could not be fully verified; exit Codex locally and inspect again"
        elif not all(self._permitted(identity) for identity in identities):
            reason = "Only verified native Codex processes belonging to the node account can be ended"
        elif kind == "candidate" and len(identities) != 1:
            complete = False
            reason = "Windows found multiple resource users; the actual writer cannot be identified safely"
        elif kind == "candidate":
            reason = (
                "Windows identified one Codex resource user, not a proven lock holder. "
                "Ending this entire process may interrupt its other conversations"
            )
        return _Owners(tuple(identities), kind, complete, reason)

    def inspect(self, thread_id: str, *, managed: bool = False) -> dict:
        thread_id = self._thread_id(thread_id)
        result = {"state": "unknown", "owners": [], "can_terminate": False, "owner_token": None}
        if managed:
            return {**result, "state": "managed", "owner_kind": "managed"}
        with self._mutex:
            self._grants = {
                token: grant for token, grant in self._grants.items()
                if grant.expires_at > time.monotonic()
            }
            try:
                with self._coordination():
                    busy, file_key = self._probe(thread_id)
                    if not busy:
                        return {**result, "state": "available"}
                    owners = self._owners(thread_id, file_key)
            except (OSError, psutil.Error) as exc:
                return {**result, "reason": str(exc)}
            result.update(
                state="external" if owners.identities else "unknown",
                owners=[identity.public() for identity in owners.identities],
                owner_kind=owners.kind,
                affects_other_threads=True,
                affected_threads_complete=False,
            )
            if owners.reason:
                result["reason"] = owners.reason
            if owners.complete and owners.identities and all(
                self._permitted(identity) for identity in owners.identities
            ):
                token = secrets.token_urlsafe(32)
                # Bound memory even when many tabs repeatedly inspect a busy thread.
                if len(self._grants) >= 128:
                    self._grants.pop(next(iter(self._grants)))
                self._grants[token] = _Grant(
                    thread_id, file_key, owners.identities, owners.kind,
                    time.monotonic() + self.TOKEN_TTL_SECONDS,
                )
                result.update(can_terminate=True, owner_token=token)
            return result

    def terminate(self, thread_id: str, owner_token: str) -> None:
        """End only the exact processes represented by a recent server-side grant.

        A caller must explicitly confirm whole-process termination. On Windows
        the confirmation must identify these as candidates, not proven holders.
        Never escalate to killing unrelated processes if a lock remains busy.
        """
        thread_id = self._thread_id(thread_id)
        with self._mutex:
            grant = self._grants.pop(owner_token, None) if isinstance(owner_token, str) else None
            if not grant or grant.thread_id != thread_id or grant.expires_at <= time.monotonic():
                raise ValueError("Writer ownership confirmation expired; inspect again")
            with self._coordination():
                busy, file_key = self._probe(thread_id)
                if not busy:
                    return
                if file_key != grant.file_key:
                    raise ValueError("Writer lock changed after inspection; inspect again")
                owners = self._owners(thread_id, file_key)
                if (
                    not owners.complete or owners.kind != grant.kind
                    or owners.identities != grant.identities
                    or not owners.identities
                    or not all(self._permitted(identity) for identity in owners.identities)
                ):
                    raise ValueError("Writer processes changed after inspection; inspect again")
                processes = []
                for expected in grant.identities:
                    current, process = self._identity(expected.pid)
                    if current != expected:
                        raise ValueError("Writer process identity changed; inspect again")
                    processes.append(process)
                for process in processes:
                    process.terminate()  # psutil also guards against PID reuse.
            deadline = time.monotonic() + self.TERMINATION_TIMEOUT_SECONDS
            while True:
                with self._coordination():
                    busy, current_key = self._probe(thread_id)
                if not busy:
                    return
                if current_key != grant.file_key:
                    raise RuntimeError("Another writer acquired the thread; inspect again")
                if time.monotonic() >= deadline:
                    raise TimeoutError("The confirmed process did not release the writer lock")
                time.sleep(0.05)
