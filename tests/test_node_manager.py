"""The local controller owns a fake node on real background threads."""

import asyncio
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from codex_anywhere.node import manager as manager_module
from codex_anywhere.node.config import node_lock, save_config
from codex_anywhere.node.manager import NodeManager


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("Background node did not reach the expected state")


def saved_identity(state_dir, *, host_id="old-host", server="http://127.0.0.1:9876"):
    save_config(
        state_dir,
        {"server": server, "name": "Existing node", "host_id": host_id, "token": "old-secret-token"},
    )


@pytest.fixture
def fake_service(monkeypatch):
    class Store:
        def __init__(self, directory):
            self.creator = threading.get_ident()
            self.closed_on = None
            self.connection = sqlite3.connect(directory / "node.sqlite3")
            self.connection.execute("CREATE TABLE IF NOT EXISTS fake_journal (value TEXT)")
            self.connection.execute("INSERT INTO fake_journal VALUES ('new-profile-command')")
            self.connection.commit()

        def close(self):
            self.closed_on = threading.get_ident()
            self.connection.close()

    class Service:
        instances = []
        startup_gate = threading.Event()
        connected_gate = threading.Event()
        cleanup_gate = threading.Event()
        failure = None

        def __init__(self, config, state_dir, codex_bin="codex", allow_insecure=False):
            self.config = config
            self.directory = state_dir
            self.codex_bin = codex_bin
            self.allow_insecure = allow_insecure
            self.creator = threading.get_ident()
            self.store = Store(state_dir)
            self.ws = None
            self.runtime = None
            self.cleanup_started = threading.Event()
            self.closed = threading.Event()
            self.__class__.instances.append(self)

        async def run(self):
            try:
                while not self.startup_gate.is_set():
                    await asyncio.sleep(0.005)
                if self.failure:
                    raise self.failure
                self.runtime = SimpleNamespace(process=object())
                while True:
                    self.ws = object() if self.connected_gate.is_set() else None
                    await asyncio.sleep(0.005)
            finally:
                self.cleanup_started.set()
                while not self.cleanup_gate.is_set():
                    await asyncio.sleep(0.005)
                self.ws = None
                self.store.close()
                self.closed.set()

    Service.startup_gate.set()
    Service.connected_gate.set()
    Service.cleanup_gate.set()
    monkeypatch.setattr(manager_module, "NodeService", Service)
    return Service


@pytest.fixture
def pair_peer():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append((self.path, body))
            self.server.received.set()
            self.server.proceed.wait(3)
            data = json.dumps(self.server.response).encode()
            self.send_response(self.server.response_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.requests = []
    server.received = threading.Event()
    server.proceed = threading.Event()
    server.proceed.set()
    server.response_status = 200
    server.response = {"host": {"id": "new-host"}, "token": "new-secret-token"}
    server.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.proceed.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_pair_runs_in_background_and_autostarts_without_a_browser(tmp_path, fake_service, pair_peer):
    pair_peer.proceed.clear()
    manager = NodeManager(tmp_path)
    try:
        assert manager.status()["status"] == "unpaired"
        accepted = manager.pair(pair_peer.url, "My node", "test-pair-code", codex_bin="custom-codex")
        assert accepted["status"] == "pairing"
        assert pair_peer.received.wait(2)
        assert not fake_service.instances
        with pytest.raises(ValueError, match="current operation"):
            manager.pair(pair_peer.url, "My node", "test-pair-code")
        assert len(pair_peer.requests) == 1
        snapshot = json.dumps(manager.status())
        assert "test-pair-code" not in snapshot and "secret-token" not in snapshot
        pair_peer.proceed.set()
        wait_for(lambda: manager.status()["connected"])
        status = manager.status()
        assert status["paired"] and status["running"]
        assert status["host_id"] == "new-host"
        assert status["codex_bin"] == "custom-codex"
        service = fake_service.instances[0]
        assert service.creator != threading.get_ident()
        assert service.store.creator == service.creator
        assert service.directory.is_relative_to(tmp_path / "profiles")
        launcher = json.loads((tmp_path / "launcher.json").read_text())
        assert launcher["active_state"] == service.directory.relative_to(tmp_path).as_posix()
        assert launcher["codex_bin"] == "custom-codex"
        assert "test-pair-code" not in json.dumps(launcher)
        assert "secret-token" not in json.dumps(launcher)
        assert not (tmp_path / "node.json").exists()
        if os.name != "nt":
            assert (tmp_path.stat().st_mode & 0o777) == 0o700
            assert ((tmp_path / "launcher.json").stat().st_mode & 0o777) == 0o600
            assert (service.directory.parent.stat().st_mode & 0o777) == 0o700
        time.sleep(0.03)
        assert manager.status()["connected"]
    finally:
        pair_peer.proceed.set()
        manager.close()
    assert service.closed.is_set()
    assert service.store.closed_on == service.creator


def test_existing_profile_start_stop_restart_and_reconnect(tmp_path, fake_service):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    try:
        assert manager.status()["paired"] and manager.status()["status"] == "stopped"
        fake_service.startup_gate.clear()
        first = manager.start(codex_bin="chosen-codex", allow_insecure=True)
        assert first["status"] == "starting" and first["running"]
        wait_for(lambda: len(fake_service.instances) == 1)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: manager.start(), range(24)))
        assert all(result["running"] for result in results)
        assert len(fake_service.instances) == 1
        fake_service.connected_gate.clear()
        fake_service.startup_gate.set()
        wait_for(lambda: manager.status()["status"] == "reconnecting")
        fake_service.connected_gate.set()
        wait_for(lambda: manager.status()["connected"])
        fake_service.connected_gate.clear()
        wait_for(lambda: not manager.status()["connected"])
        assert manager.status()["status"] == "reconnecting"
        assert len(fake_service.instances) == 1
        fake_service.cleanup_gate.clear()
        stopping = manager.stop()
        assert stopping["status"] == "stopping"
        assert fake_service.instances[0].cleanup_started.wait(1)
        manager.stop()  # A repeated stop must not interrupt the cleanup itself.
        with pytest.raises(ValueError, match="current node operation"):
            manager.start()
        with pytest.raises(ValueError, match="current operation"):
            manager.pair("http://127.0.0.1:1234", "Replacement", "code", replace_pairing=True)
        fake_service.cleanup_gate.set()
        wait_for(lambda: manager.status()["status"] == "stopped")
        assert fake_service.instances[0].store.closed_on == fake_service.instances[0].creator
        manager.start()
        wait_for(lambda: len(fake_service.instances) == 2)
        assert fake_service.instances[1].codex_bin == "chosen-codex"
        assert fake_service.instances[1].allow_insecure is True
    finally:
        fake_service.startup_gate.set()
        fake_service.cleanup_gate.set()
        manager.close()


def test_repair_preserves_old_credentials_and_journal_in_a_separate_profile(tmp_path, fake_service, pair_peer):
    saved_identity(tmp_path)
    old_config = (tmp_path / "node.json").read_bytes()
    with sqlite3.connect(tmp_path / "node.sqlite3") as connection:
        connection.execute("CREATE TABLE old_journal (command TEXT)")
        connection.execute("INSERT INTO old_journal VALUES ('must-never-replay-to-new-hub')")
    manager = NodeManager(tmp_path)
    try:
        with pytest.raises(ValueError, match="replace_pairing"):
            manager.pair(pair_peer.url, "Replacement", "test-code")
        manager.pair(pair_peer.url, "Replacement", "test-code", replace_pairing=True)
        wait_for(lambda: manager.status()["connected"])
        service = fake_service.instances[0]
        assert service.directory != tmp_path
        assert (tmp_path / "node.json").read_bytes() == old_config
        with sqlite3.connect(tmp_path / "node.sqlite3") as connection:
            assert connection.execute("SELECT command FROM old_journal").fetchone()[0] == "must-never-replay-to-new-hub"
        with sqlite3.connect(service.directory / "node.sqlite3") as connection:
            assert not connection.execute("SELECT name FROM sqlite_master WHERE name='old_journal'").fetchall()
        assert service.config["token"] == "new-secret-token"
        manager.stop()
        wait_for(lambda: manager.status()["status"] == "stopped")
    finally:
        manager.close()
    restored = NodeManager(tmp_path)
    try:
        assert restored.status()["host_id"] == "new-host"
        assert restored.status()["status"] == "stopped"
    finally:
        restored.close()


def test_failed_pair_never_switches_active_profile_or_retries(tmp_path, fake_service, pair_peer, caplog):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    pair_peer.response_status = 403
    pair_peer.response = {"error": "remote failure contains test-pair-code new-secret-token"}
    try:
        manager.pair(pair_peer.url, "Replacement", "test-pair-code", replace_pairing=True)
        wait_for(lambda: manager.status()["status"] == "error")
        status = manager.status()
        assert status["paired"] and status["host_id"] == "old-host"
        assert status["server"] == "http://127.0.0.1:9876"
        assert not (tmp_path / "launcher.json").exists()
        assert not fake_service.instances
        assert len(pair_peer.requests) == 1
        assert "test-pair-code" not in json.dumps(status) + caplog.text
        assert "new-secret-token" not in json.dumps(status) + caplog.text
        manager.start()
        wait_for(lambda: manager.status()["connected"])
        assert fake_service.instances[0].config["token"] == "old-secret-token"
    finally:
        manager.close()


def test_close_during_pairing_does_not_commit_a_late_result(tmp_path, fake_service, pair_peer, monkeypatch):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    pair_peer.proceed.clear()
    monkeypatch.setattr(manager_module, "CLOSE_TIMEOUT_SECONDS", 0.05)
    manager.pair(pair_peer.url, "Replacement", "late-code", replace_pairing=True)
    assert pair_peer.received.wait(2)
    started = time.monotonic()
    manager.close()
    assert time.monotonic() - started < 0.5
    assert not (tmp_path / "launcher.json").exists()
    pair_peer.proceed.set()
    wait_for(lambda: manager._worker is None)
    assert manager.status()["host_id"] == "old-host"
    assert not fake_service.instances
    assert not (tmp_path / "launcher.json").exists()
    with pytest.raises(ValueError, match="closed"):
        manager.start()


def test_close_is_bounded_and_does_not_recancel_cleanup(tmp_path, fake_service, monkeypatch):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    manager.start()
    wait_for(lambda: manager.status()["connected"])
    fake_service.cleanup_gate.clear()
    monkeypatch.setattr(manager_module, "CLOSE_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    manager.close()
    assert time.monotonic() - started < 0.5
    assert manager.status()["status"] == "error"
    assert not fake_service.instances[0].closed.is_set()
    fake_service.cleanup_gate.set()
    wait_for(lambda: fake_service.instances[0].closed.is_set())
    manager.close()
    assert fake_service.instances[0].store.closed_on == fake_service.instances[0].creator


def test_profile_lock_prevents_another_node_writer(tmp_path, fake_service):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    try:
        with node_lock(tmp_path):
            manager.start()
            wait_for(lambda: manager.status()["status"] == "error")
            assert "Another node process" in manager.status()["last_error"]
            assert not fake_service.instances
        manager.start()
        wait_for(lambda: manager.status()["connected"])
    finally:
        manager.close()


def test_repair_rejects_a_separately_running_previous_profile_before_consuming_code(
    tmp_path, fake_service, pair_peer
):
    saved_identity(tmp_path)
    manager = NodeManager(tmp_path)
    try:
        with node_lock(tmp_path):
            manager.pair(pair_peer.url, "Replacement", "unused-code", replace_pairing=True)
            wait_for(lambda: manager.status()["status"] == "error")
            assert "Another node process" in manager.status()["last_error"]
            assert manager.status()["host_id"] == "old-host"
            assert not pair_peer.requests
            assert not fake_service.instances
        manager.pair(pair_peer.url, "Replacement", "unused-code", replace_pairing=True)
        wait_for(lambda: manager.status()["connected"])
        assert manager.status()["host_id"] == "new-host"
        assert len(pair_peer.requests) == 1
        # The preserved profile becomes available after the new identity commits.
        with node_lock(tmp_path):
            pass
    finally:
        manager.close()


@pytest.mark.parametrize("active_state", ["../outside", "/outside"])
def test_active_profile_cannot_escape_state_directory(tmp_path, active_state):
    (tmp_path / "launcher.json").write_text(json.dumps({"active_state": active_state}))
    manager = NodeManager(tmp_path)
    try:
        status = manager.status()
        assert status["status"] == "error"
        assert status["paired"] is False
        with pytest.raises(ValueError, match="Pair this node"):
            manager.start()
    finally:
        manager.close()


def test_saved_preferences_and_legacy_state_are_loaded_without_starting(tmp_path, fake_service):
    saved_identity(tmp_path)
    (tmp_path / "launcher.json").write_text(json.dumps({
        "active_state": ".", "codex_bin": "saved-codex", "allow_insecure": True,
    }))
    manager = NodeManager(tmp_path)
    try:
        assert manager.status()["codex_bin"] == "saved-codex"
        assert manager.status()["allow_insecure"] is True
        assert not fake_service.instances
        manager.start()
        wait_for(lambda: manager.status()["connected"])
        assert fake_service.instances[0].codex_bin == "saved-codex"
    finally:
        manager.close()


def test_native_start_error_does_not_expose_exception_credentials(tmp_path, fake_service, caplog):
    saved_identity(tmp_path)
    fake_service.failure = FileNotFoundError("do not display old-secret-token")
    manager = NodeManager(tmp_path)
    try:
        manager.start()
        wait_for(lambda: manager.status()["status"] == "error")
        assert "Codex executable was not found" in manager.status()["last_error"]
        assert "old-secret-token" not in json.dumps(manager.status()) + caplog.text
        assert fake_service.instances[0].closed.is_set()
    finally:
        manager.close()
