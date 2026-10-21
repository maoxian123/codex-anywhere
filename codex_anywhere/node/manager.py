"""Process-local node lifetime for the local management page.

HTTP handlers only submit work. The worker owns its asyncio loop, SQLite
connection, profile lock and Codex process until an explicit stop or shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import threading
import uuid
from pathlib import Path

from .config import load_config, node_lock, validate_server
from .service import NodeService, pair_node

CLOSE_TIMEOUT_SECONDS = 20.0
MAX_CONFIG_BYTES = 64 * 1024


class NodeManager:
    def __init__(self, state_dir: Path, codex_bin: str = "codex"):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._service: NodeService | None = None
        self._closed = False
        self._stop_requested = False
        self._cancel_sent = False
        self._phase = "unpaired"
        self._last_error: str | None = None
        self._active_state: Path | None = self.state_dir
        self._identity: dict | None = None
        self._pending: dict | None = None
        self._codex_bin = self._binary(codex_bin)
        self._allow_insecure = False
        try:
            self._private_directory(self.state_dir)
            self._load_launcher(codex_bin)
            if self._active_state and (self._active_state / "node.json").exists():
                self._identity = self._read_identity(self._active_state)
                self._phase = "stopped"
        except (OSError, ValueError, TypeError, KeyError):
            self._active_state = None
            self._phase = "error"
            self._last_error = "Saved node configuration is invalid; check the local configuration or pair again"

    @staticmethod
    def _binary(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 32768:
            raise ValueError("A valid Codex executable path is required")
        return value.strip()

    @staticmethod
    def _private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)

    @staticmethod
    def _json(path: Path) -> dict:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("Configuration is too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Configuration must be an object")
        return result

    @classmethod
    def _read_identity(cls, directory: Path) -> dict:
        config = cls._json(directory / "node.json")
        if not all(isinstance(config.get(key), str) and config[key] for key in ("server", "token", "host_id", "name")):
            raise ValueError("Invalid saved node identity")
        # Permit displaying an existing HTTP profile, but still reject URLs with
        # embedded credentials. Starting it requires the explicit preference.
        server = validate_server(config["server"], allow_insecure=True)
        return {"server": server, "name": config["name"], "host_id": config["host_id"]}

    def _contained_state(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("active_state must be relative")
        candidate = (self.state_dir / relative).resolve()
        if not candidate.is_relative_to(self.state_dir):
            raise ValueError("active_state must remain inside the state directory")
        return candidate

    def _load_launcher(self, supplied_binary: str) -> None:
        path = self.state_dir / "launcher.json"
        if not path.exists():
            return
        if not path.resolve().is_relative_to(self.state_dir):
            raise ValueError("Launcher configuration must remain inside the state directory")
        config = self._json(path)
        self._active_state = self._contained_state(config.get("active_state", "."))
        saved_binary = config.get("codex_bin", self._codex_bin)
        self._codex_bin = self._binary(saved_binary if supplied_binary == "codex" else supplied_binary)
        insecure = config.get("allow_insecure", False)
        if type(insecure) is not bool:
            raise ValueError("allow_insecure must be a boolean")
        self._allow_insecure = insecure

    def _write_launcher(self, active_state: Path, binary: str, insecure: bool) -> None:
        relative = active_state.relative_to(self.state_dir).as_posix()
        if self._contained_state(relative) != active_state:
            raise ValueError("Invalid active profile")
        self._private_directory(self.state_dir)
        temporary = self.state_dir / f".launcher-{uuid.uuid4().hex}.tmp"
        data = {
            "version": 1,
            "active_state": relative,
            "codex_bin": binary,
            "allow_insecure": insecure,
        }
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.state_dir / "launcher.json")
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _status_locked(self) -> dict:
        phase = self._phase
        running = bool(self._worker and phase in {"starting", "stopping"})
        service = self._service
        connected = bool(running and service is not None and service.ws is not None and phase != "stopping")
        if phase == "starting" and service is not None:
            if connected:
                phase = "connected"
            elif service.runtime is not None:
                phase = "reconnecting"
        identity = self._pending if self._phase == "pairing" else self._identity
        identity = identity or {}
        return {
            "paired": self._identity is not None,
            "running": running,
            "connected": connected,
            "status": phase,
            "server": identity.get("server", ""),
            "name": identity.get("name", ""),
            "host_id": (self._identity or {}).get("host_id"),
            "codex_bin": identity.get("codex_bin", self._codex_bin),
            "allow_insecure": identity.get("allow_insecure", self._allow_insecure),
            "last_error": self._last_error,
            "platform": platform.system().lower(),
        }

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("The node manager has been closed")

    def pair(
        self,
        server: str,
        name: str,
        code: str,
        codex_bin: str = "codex",
        allow_insecure: bool = False,
        replace_pairing: bool = False,
    ) -> dict:
        if type(allow_insecure) is not bool or type(replace_pairing) is not bool:
            raise ValueError("Pairing preferences must be booleans")
        try:
            server = validate_server(server, allow_insecure)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Use a valid HTTPS server URL, or explicitly allow insecure HTTP") from None
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(char) < 32 for char in name):
            raise ValueError("A node name of 1 to 120 characters is required")
        if not isinstance(code, str) or not code.strip() or len(code) > 512:
            raise ValueError("A valid pairing code is required")
        binary = self._binary(codex_bin)
        with self._lock:
            self._ensure_open()
            if self._worker is not None:
                raise ValueError("Stop the node and wait for the current operation before pairing")
            if self._identity is not None and not replace_pairing:
                raise ValueError("Replacing an existing pairing requires replace_pairing=true")
            profile = self._contained_state(f"profiles/{uuid.uuid4().hex}")
            self._pending = {
                "server": server, "name": name.strip(), "codex_bin": binary,
                "allow_insecure": allow_insecure,
            }
            self._phase = "pairing"
            self._last_error = None
            self._stop_requested = False
            self._cancel_sent = False
            self._worker = threading.Thread(
                target=self._worker_main,
                args=(profile, binary, allow_insecure, (server, name.strip(), code.strip())),
                name="codex-node-manager", daemon=True,
            )
            self._worker.start()
            return self._status_locked()

    def start(self, codex_bin: str | None = None, allow_insecure: bool | None = None) -> dict:
        if allow_insecure is not None and type(allow_insecure) is not bool:
            raise ValueError("allow_insecure must be a boolean")
        binary = self._binary(codex_bin) if codex_bin is not None else None
        with self._lock:
            self._ensure_open()
            if self._phase in {"pairing", "stopping"}:
                raise ValueError("Wait for the current node operation to finish before starting")
            if self._worker is not None:
                return self._status_locked()
            if self._identity is None or self._active_state is None:
                raise ValueError("Pair this node before starting it")
            binary = binary or self._codex_bin
            insecure = self._allow_insecure if allow_insecure is None else allow_insecure
            try:
                self._write_launcher(self._active_state, binary, insecure)
            except (OSError, ValueError):
                self._phase = "error"
                self._last_error = "Could not save node preferences; check the state directory permissions"
                return self._status_locked()
            self._codex_bin, self._allow_insecure = binary, insecure
            self._stop_requested = False
            self._cancel_sent = False
            self._last_error = None
            self._phase = "starting"
            self._worker = threading.Thread(
                target=self._worker_main, args=(self._active_state, binary, insecure, None),
                name="codex-node-manager", daemon=True,
            )
            self._worker.start()
            return self._status_locked()

    def stop(self) -> dict:
        with self._lock:
            self._ensure_open()
            if self._phase == "pairing":
                raise ValueError("Wait for pairing to finish before stopping the node")
            if self._worker is None:
                self._phase = "stopped" if self._identity else "unpaired"
                return self._status_locked()
            self._stop_requested = True
            self._phase = "stopping"
            self._cancel_service_locked()
            return self._status_locked()

    def _cancel_service_locked(self) -> None:
        if not self._cancel_sent and self._loop is not None and self._task is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._task.cancel)
                self._cancel_sent = True

    def _worker_main(self, profile: Path, binary: str, insecure: bool, pairing: tuple | None) -> None:
        context = "pairing" if pairing is not None else "starting"
        failed = False
        try:
            if pairing is not None:
                self._private_directory(profile.parent)
                self._private_directory(profile)
            with node_lock(profile):
                if pairing is not None:
                    with contextlib.ExitStack() as previous_profile:
                        # A separately launched `node run` can own the previous
                        # profile even though this controller is stopped. Do not
                        # consume a code or replace that active pairing.
                        if self._identity is not None and self._active_state is not None:
                            previous_profile.enter_context(node_lock(self._active_state))
                        server, name, code = pairing
                        pair_node(server, name, profile, code, allow_insecure=insecure)
                        identity = self._read_identity(profile)
                        with self._lock:
                            if self._closed or self._stop_requested:
                                return
                            self._write_launcher(profile, binary, insecure)
                            self._active_state, self._identity = profile, identity
                            self._codex_bin, self._allow_insecure = binary, insecure
                            self._pending = None
                            self._phase = "starting"
                    context = "starting"
                self._run_service(profile, binary, insecure)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - never expose remote errors or saved credentials
            failed = True
            with self._lock:
                self._phase = "error"
                if isinstance(exc, ValueError) and str(exc) == "Another node process is already using this state directory":
                    self._last_error = "Another node process is already using this profile; stop it first"
                elif context == "pairing":
                    self._last_error = "Pairing failed; check the server and code, then try again"
                elif isinstance(exc, FileNotFoundError):
                    self._last_error = "Codex executable was not found; check its path"
                else:
                    self._last_error = "Node could not run; check the Codex path, connection settings and state directory"
        finally:
            with self._lock:
                self._loop = self._task = self._service = None
                self._worker = None
                self._pending = None
                if self._closed or self._stop_requested:
                    self._phase = "stopped" if self._identity else "unpaired"
                elif not failed:
                    self._phase = "error"
                    self._last_error = "Node stopped unexpectedly; start it again when ready"

    def _run_service(self, profile: Path, binary: str, insecure: bool) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._service_main(profile, binary, insecure))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    async def _service_main(self, profile: Path, binary: str, insecure: bool) -> None:
        with self._lock:
            if self._closed or self._stop_requested:
                return
        config = load_config(profile)
        service = NodeService(config, profile, codex_bin=binary, allow_insecure=insecure)
        with self._lock:
            if self._closed or self._stop_requested:
                # No coroutine has started, so its finally block cannot own the
                # just-created SQLite connection yet. Close it on this thread.
                service.store.close()
                return
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            self._service = service
        await service.run()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_requested = True
            worker = self._worker
            if worker is not None:
                self._phase = "stopping"
                self._cancel_service_locked()
        if worker is not None and worker is not threading.current_thread():
            worker.join(CLOSE_TIMEOUT_SECONDS)
            if worker.is_alive():
                with self._lock:
                    self._phase = "error"
                    self._last_error = "Node shutdown is still completing; this manager cannot start another node"
