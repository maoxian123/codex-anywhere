from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codex_anywhere.node import ownership
from codex_anywhere.node.ownership import WriterOwnership, _Identity, _Owners, _parse_linux_lock

THREAD = "01a15d58-78c7-7dc0-a370-8308596dcb8c"
OTHER_THREAD = "01a15d58-78c7-7dc0-a370-8308596dcb8d"


@pytest.fixture
def writer(tmp_path):
    return WriterOwnership(tmp_path)


@pytest.fixture
def held_lock(writer):
    """Only our disposable child process is ever signalled by these tests."""
    writer.directory.mkdir(parents=True)
    path = writer.directory / f"{THREAD}.lock"
    code = (
        "import os, sys; from codex_anywhere.node.ownership import _try_lock; "
        "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
        "assert _try_lock(fd); print(os.getpid(), flush=True); sys.stdin.read()"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        process.writer_pid = int(process.stdout.readline().strip())
        yield process, path
    finally:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def _fake_native(writer, monkeypatch, process, *, kind="lock_holder"):
    actual, _ = writer._identity(process.writer_pid)
    native = replace(actual, name="codex", executable=str(Path("/native/codex")))
    monkeypatch.setattr(writer, "_owners", lambda *_: _Owners((native,), kind, True))
    original_identity = writer._identity

    def identity(pid):
        info, peer = original_identity(pid)
        return replace(info, name="codex", executable=native.executable), peer

    monkeypatch.setattr(writer, "_identity", identity)
    return native


def test_inspection_does_not_create_a_thread_lock(writer):
    result = writer.inspect(THREAD)
    assert result["state"] == "available"
    assert result["can_terminate"] is False
    assert not (writer.directory / f"{THREAD}.lock").exists()
    assert (writer.directory / ".coordination.lock").is_file()


@pytest.mark.parametrize("value", ["../outside", "", "not-a-thread", None, 12])
def test_invalid_thread_ids_never_reach_filesystem(writer, value):
    with pytest.raises(ValueError, match="Invalid native thread"):
        writer.inspect(value)
    assert not writer.directory.exists()


def test_stale_lock_is_available_and_preserved(writer):
    writer.directory.mkdir(parents=True)
    path = writer.directory / f"{THREAD}.lock"
    path.write_bytes(b"leave existing lock contents alone")
    before = path.stat()
    assert writer.inspect(THREAD)["state"] == "available"
    assert path.read_bytes() == b"leave existing lock contents alone"
    assert path.stat().st_ino == before.st_ino


def test_real_os_lock_is_busy_and_available_after_child_exits(writer, held_lock):
    process, path = held_lock
    with writer._coordination():
        busy, key = writer._probe(THREAD)
    assert busy is True
    assert key == (path.stat().st_dev, path.stat().st_ino)
    process.stdin.close()
    process.wait(timeout=5)
    assert writer.inspect(THREAD)["state"] == "available"
    assert path.exists()  # We never delete a native thread lock.


def test_managed_thread_does_not_offer_process_termination(writer, held_lock):
    assert writer.inspect(THREAD, managed=True) == {
        "state": "managed", "owners": [], "can_terminate": False,
        "owner_token": None, "owner_kind": "managed",
    }


def test_unverified_or_non_codex_owner_is_never_terminable(writer, held_lock, monkeypatch):
    process, _ = held_lock
    identity, _ = writer._identity(process.pid)
    monkeypatch.setattr(writer, "_owners", lambda *_: _Owners((identity,), "lock_holder", True))
    result = writer.inspect(THREAD)
    assert result["state"] == "external"
    assert result["owners"][0]["pid"] == process.pid
    assert result["can_terminate"] is False
    assert result["owner_token"] is None


def test_unknown_owner_cannot_receive_a_termination_grant(writer, held_lock, monkeypatch):
    monkeypatch.setattr(writer, "_owners", lambda *_: _Owners((), "lock_holder", False))
    result = writer.inspect(THREAD)
    assert result["state"] == "unknown"
    assert result["owner_token"] is None
    assert result["can_terminate"] is False


def test_permission_failure_is_unknown(writer, monkeypatch):
    def denied(*_):
        raise PermissionError("Writer lock permission denied")

    monkeypatch.setattr(writer, "_probe", denied)
    result = writer.inspect(THREAD)
    assert result["state"] == "unknown"
    assert "permission denied" in result["reason"]


def test_confirmed_disposable_process_is_ended_and_lock_released(writer, held_lock, monkeypatch):
    process, path = held_lock
    _fake_native(writer, monkeypatch, process)
    result = writer.inspect(THREAD)
    assert result["can_terminate"] is True
    assert result["affects_other_threads"] is True
    writer.terminate(THREAD, result["owner_token"])
    process.wait(timeout=5)
    assert writer.inspect(THREAD)["state"] == "available"
    assert path.exists()
    with pytest.raises(ValueError, match="expired"):
        writer.terminate(THREAD, result["owner_token"])


@pytest.mark.parametrize("changed_field", ["pid", "started_at", "executable", "username"])
def test_changed_process_identity_is_rejected(writer, held_lock, monkeypatch, changed_field):
    process, _ = held_lock
    native = _fake_native(writer, monkeypatch, process)
    result = writer.inspect(THREAD)
    value = getattr(native, changed_field)
    changed = replace(native, **{changed_field: value + 1 if isinstance(value, (int, float)) else "x"})
    monkeypatch.setattr(writer, "_owners", lambda *_: _Owners((changed,), "lock_holder", True))
    with pytest.raises(ValueError, match="changed"):
        writer.terminate(THREAD, result["owner_token"])
    assert process.poll() is None


def test_changed_lock_inode_is_rejected_before_signalling(writer, held_lock, monkeypatch):
    process, _ = held_lock
    _fake_native(writer, monkeypatch, process)
    result = writer.inspect(THREAD)
    original = writer._probe

    def changed(thread_id):
        busy, (device, inode) = original(thread_id)
        return busy, (device, inode + 1)

    monkeypatch.setattr(writer, "_probe", changed)
    with pytest.raises(ValueError, match="lock changed"):
        writer.terminate(THREAD, result["owner_token"])
    assert process.poll() is None


def test_token_bound_to_thread_and_cannot_be_reused(writer, held_lock, monkeypatch):
    process, _ = held_lock
    _fake_native(writer, monkeypatch, process)
    token = writer.inspect(THREAD)["owner_token"]
    with pytest.raises(ValueError):
        writer.terminate(OTHER_THREAD, token)
    with pytest.raises(ValueError):
        writer.terminate(THREAD, token)
    assert process.poll() is None


def test_expired_confirmation_does_not_terminate_process(writer, held_lock, monkeypatch):
    process, _ = held_lock
    _fake_native(writer, monkeypatch, process)
    token = writer.inspect(THREAD)["owner_token"]
    writer._grants[token] = replace(writer._grants[token], expires_at=time.monotonic() - 1)
    with pytest.raises(ValueError, match="expired"):
        writer.terminate(THREAD, token)
    assert process.poll() is None


def test_waits_for_actual_unlock_and_reports_timeout(writer, held_lock, monkeypatch):
    process, _ = held_lock
    native = _fake_native(writer, monkeypatch, process)
    token = writer.inspect(THREAD)["owner_token"]

    class IgnoringProcess:
        def terminate(self):
            pass

    monkeypatch.setattr(writer, "_identity", lambda *_: (native, IgnoringProcess()))
    monkeypatch.setattr(writer, "TERMINATION_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError, match="did not release"):
        writer.terminate(THREAD, token)
    assert process.poll() is None


def test_available_thread_does_not_signal_a_previous_owner(writer, held_lock, monkeypatch):
    process, _ = held_lock
    _fake_native(writer, monkeypatch, process)
    token = writer.inspect(THREAD)["owner_token"]
    monkeypatch.setattr(writer, "_probe", lambda *_: (False, None))
    writer.terminate(THREAD, token)
    assert process.poll() is None


def test_coordination_is_bounded_and_not_mistaken_for_thread_occupancy(writer, monkeypatch):
    writer.directory.mkdir(parents=True)
    path = writer.directory / ".coordination.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert ownership._try_lock(fd)
        monkeypatch.setattr(writer, "COORDINATION_TIMEOUT_SECONDS", 0.05)
        started = time.monotonic()
        result = writer.inspect(THREAD)
        assert result["state"] == "unknown"
        assert "coordination is busy" in result["reason"]
        assert time.monotonic() - started < 2
    finally:
        ownership._unlock(fd)
        os.close(fd)


def test_linux_parser_distinguishes_holders_from_waiters(monkeypatch):
    monkeypatch.setattr(os, "makedev", lambda major, minor: (major << 8) | minor, raising=False)
    assert _parse_linux_lock("7: FLOCK ADVISORY WRITE 42 08:01:9001 0 EOF") == (
        "FLOCK", "WRITE", 42, (2049, 9001), "0", "EOF",
    )
    assert _parse_linux_lock("7: -> FLOCK ADVISORY WRITE 99 08:01:9001 0 EOF") is None
    assert _parse_linux_lock("lock:\t1: FLOCK ADVISORY WRITE 42 08:01:9001 0 EOF")[2] == 42
    assert _parse_linux_lock("pos:\t0") is None


def test_windows_candidate_start_time_must_match_kernel_record(writer, monkeypatch):
    monkeypatch.setattr(ownership.sys, "platform", "win32")
    identity = _Identity(9876, 1234, "codex.exe", "C:/native/codex.exe", writer._username)
    monkeypatch.setattr(ownership, "_restart_manager_candidates", lambda *_: {9876: 9999})
    monkeypatch.setattr(writer, "_identity", lambda *_: (identity, None))
    owners = writer._owners(THREAD, (1, 1))
    assert not owners.complete
    assert owners.kind == "candidate"


def test_windows_multiple_resource_users_are_not_terminable(writer, monkeypatch):
    monkeypatch.setattr(ownership.sys, "platform", "win32")
    monkeypatch.setattr(ownership, "_restart_manager_candidates", lambda *_: {9876: 12, 9877: 13})
    monkeypatch.setattr(writer, "_identity", lambda pid: (
        _Identity(pid, pid - 9864, "codex", str(Path("/native/codex")), writer._username), None,
    ))
    owners = writer._owners(THREAD, (1, 1))
    assert not owners.complete
    assert "multiple resource users" in owners.reason


def test_windows_single_native_candidate_is_explicitly_labelled(writer, monkeypatch):
    monkeypatch.setattr(ownership.sys, "platform", "win32")
    monkeypatch.setattr(ownership, "_restart_manager_candidates", lambda *_: {9876: 12})
    monkeypatch.setattr(writer, "_identity", lambda *_: (
        _Identity(9876, 12, "codex", str(Path("/native/codex")), writer._username), None,
    ))
    owners = writer._owners(THREAD, (1, 1))
    assert owners.complete
    assert owners.kind == "candidate"
    assert "not a proven lock holder" in owners.reason
    assert "other conversations" in owners.reason


@pytest.fixture
def linux_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    original_path = Path

    def proc_path(value):
        value = str(value)
        if value == "/proc" or value.startswith("/proc/"):
            return proc / value.removeprefix("/proc").lstrip("/")
        return original_path(value)

    monkeypatch.setattr(ownership, "Path", proc_path)
    monkeypatch.setattr(os, "makedev", lambda major, minor: (major << 8) | minor, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: proc.stat().st_uid, raising=False)
    return proc


def test_linux_inherited_locked_descriptors_are_all_identified(writer, linux_proc):
    line = "7: FLOCK ADVISORY WRITE 101 08:01:9001 0 EOF"
    (linux_proc / "locks").write_text(line)
    for pid in (101, 102):
        fdinfo = linux_proc / str(pid) / "fdinfo"
        fdinfo.mkdir(parents=True)
        (fdinfo / "3").write_text(f"pos:\t0\nlock:\t{line}\n")
    # An unrelated descriptor for the same inode is not proof of ownership.
    unowned = linux_proc / "103" / "fdinfo"
    unowned.mkdir(parents=True)
    (unowned / "3").write_text("pos:\t0\nino:\t9001\n")
    assert writer._linux_holders((2049, 9001)) == ({101, 102}, True)


def test_linux_surviving_inherited_descriptor_can_outlive_original_pid(writer, linux_proc):
    line = "7: FLOCK ADVISORY WRITE -1 08:01:9001 0 EOF"
    (linux_proc / "locks").write_text(line)
    fdinfo = linux_proc / "102" / "fdinfo"
    fdinfo.mkdir(parents=True)
    (fdinfo / "3").write_text(f"lock:\t{line}\n")
    assert writer._linux_holders((2049, 9001)) == ({102}, True)


def test_linux_proc_scan_cutoff_cannot_authorize_termination(writer, linux_proc, monkeypatch):
    (linux_proc / "locks").write_text("7: FLOCK ADVISORY WRITE 101 08:01:9001 0 EOF")
    (linux_proc / "101" / "fdinfo").mkdir(parents=True)
    calls = iter((0.0, 5.0))
    monkeypatch.setattr(ownership.time, "monotonic", lambda: next(calls))
    assert writer._linux_holders((2049, 9001)) == (set(), False)


def test_linux_unsupported_ofd_lock_cannot_authorize_a_pid(writer, linux_proc):
    (linux_proc / "locks").write_text("7: OFDLCK ADVISORY WRITE -1 08:01:9001 0 EOF")
    assert writer._linux_holders((2049, 9001)) == (set(), False)


def test_restart_manager_timeout_is_reported_without_terminating_any_candidates(monkeypatch):
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 2
        assert args[0][0] == getattr(sys, "_base_executable", sys.executable)
        raise subprocess.TimeoutExpired(args[0], 2)

    monkeypatch.setattr(ownership.subprocess, "run", timed_out)
    with pytest.raises(OSError, match="timed out or failed"):
        ownership._restart_manager_candidates(Path("unused"))


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows Restart Manager")
def test_windows_real_resource_user_is_only_a_candidate(writer, held_lock):
    process, _ = held_lock
    result = writer.inspect(THREAD)
    assert result["state"] in {"external", "unknown"}
    assert result["can_terminate"] is False  # Our disposable process is Python, not Codex.
    if result["owners"]:
        assert process.writer_pid in {owner["pid"] for owner in result["owners"]}
        assert result["owner_kind"] == "candidate"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Requires Linux /proc locks")
def test_linux_real_flock_holder_is_verified(writer, held_lock):
    process, path = held_lock
    key = (path.stat().st_dev, path.stat().st_ino)
    pids, complete = writer._linux_holders(key)
    assert complete
    assert process.pid in pids
    result = writer.inspect(THREAD)
    assert result["owner_kind"] == "lock_holder"
    assert result["can_terminate"] is False  # Python is deliberately excluded.
