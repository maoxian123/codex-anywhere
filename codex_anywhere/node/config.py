"""Node identity and filesystem path normalization."""

from __future__ import annotations

import ipaddress
import json
import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def validate_server(server: str, allow_insecure: bool = False) -> str:
    parsed = urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Server must be an http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Server URL cannot contain credentials, query, or fragment")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not loopback and not allow_insecure:
        raise ValueError(
            "Remote servers require HTTPS; use --allow-insecure explicitly for testing"
        )
    # Parse port now rather than letting a later connection fail obscurely.
    _ = parsed.port
    return server.rstrip("/")


def websocket_url(server: str) -> str:
    parsed = urlsplit(server)
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path.rstrip("/") + "/ws/node",
            "",
            "",
        )
    )


def resolve_path(
    value: str, *, directory: bool = False, base: str | None = None, must_exist: bool = True
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A filesystem path is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if base is None:
            raise ValueError("Path must be absolute")
        candidate = Path(base) / candidate
    candidate = candidate.resolve(strict=must_exist)
    if must_exist and directory and not candidate.is_dir():
        raise ValueError("Working directory is not a directory")
    if must_exist and not directory and not candidate.is_file():
        raise ValueError("Artifact is not a file")
    return candidate


def save_config(state_dir: Path, config: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        state_dir.chmod(0o700)
    path = state_dir / "node.json"
    temporary = state_dir / "node.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_config(state_dir: Path) -> dict:
    path = state_dir / "node.json"
    if not path.exists():
        raise ValueError("This node has not been paired; run node pair first")
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if "roots" in config:
        config.pop("roots")
        save_config(state_dir, config)
    return config


@contextmanager
def node_lock(state_dir: Path):
    """One process owns a node journal and Codex process at any point in time."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "node.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another node process is already using this state directory") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
