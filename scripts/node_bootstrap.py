"""Local, foreground node launcher; deliberately uses only the Python stdlib."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MINIMUM_PYTHON = (3, 11)
MANIFEST = ".node-bootstrap.json"
RUNTIME_IMPORTS = (
    "flask", "flask_sock", "flask_sqlalchemy", "alembic", "psycopg", "dotenv",
    "websockets", "httpx", "PIL", "psutil", "click", "codex_anywhere.cli",
)


class BootstrapError(Exception):
    pass


def _bootstrap_lock_path(root: Path) -> Path:
    user = str(os.getuid()) if hasattr(os, "getuid") else hashlib.sha256(
        str(Path.home()).encode("utf-8")
    ).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / f"codex-anywhere-bootstrap-{user}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        hasattr(os, "getuid") and info.st_uid != os.getuid()
    ):
        raise BootstrapError("The per-user bootstrap lock directory is unavailable.")
    key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode("utf-8")).hexdigest()
    return directory / f"{key}.lock"


@contextmanager
def bootstrap_lock(root: Path, *, timeout: float = 300.0) -> Iterator[None]:
    """Serialize environment changes across double-clicks; leave the inode intact."""
    path = _bootstrap_lock_path(root)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    announced = False
    deadline = time.monotonic() + timeout
    try:
        # Both flock and Windows CRT locking can lock beyond EOF. Do not write
        # an initialization byte before locking: a simultaneous first launch
        # could already hold that byte when the competing write occurs.
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise BootstrapError(
                        "Another launch is still preparing this project; this node manager "
                        "was not started. Wait for the other launcher to finish, then try again."
                    ) from exc
                if not announced:
                    print(
                        f"Another launch is preparing this project; waiting up to {timeout:g} seconds...",
                        flush=True,
                    )
                    announced = True
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def environment_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256(b"codex-anywhere-node-bootstrap-v1\0")
    for name in ("requirements.lock", "pyproject.toml"):
        try:
            contents = (root / name).read_bytes()
        except OSError as exc:
            raise BootstrapError(f"{name} is missing or unreadable; restore the project files.") from exc
        digest.update(name.encode() + b"\0" + contents + b"\0")
    return digest.hexdigest()


def probe_python(python: Path, root: Path, *, dependencies: bool = False) -> dict | None:
    modules = list(RUNTIME_IMPORTS) if dependencies else []
    if dependencies and os.name != "nt":
        modules.append("gunicorn")
    code = (
        "import importlib,json,sys\n"
        "missing=[]\n"
        "for name in json.loads(sys.argv[1]):\n"
        " try: importlib.import_module(name)\n"
        " except Exception: missing.append(name)\n"
        "print(json.dumps({'version':list(sys.version_info[:3]),'missing':missing}))\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code, json.dumps(modules)], cwd=root,
            capture_output=True, text=True, timeout=20, check=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if (
            not isinstance(value, dict) or not isinstance(value.get("version"), list)
            or len(value["version"]) != 3 or not isinstance(value.get("missing"), list)
        ):
            return None
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def valid_python(probe: dict | None) -> bool:
    return bool(probe and tuple(probe["version"][:2]) >= MINIMUM_PYTHON)


def run_foreground(command: list[str], root: Path) -> int:
    """Remain attached until the child exits, including while it handles Ctrl+C.

    All processes share the foreground console/process group. Ctrl+C therefore
    reaches the CLI as well as this waiter; letting the CLI shut down its runtime
    avoids leaving a child running after the launcher has returned.
    """
    child = subprocess.Popen(command, cwd=root)
    interrupted = False
    while True:
        try:
            code = child.wait()
            return 130 if interrupted else code
        except KeyboardInterrupt:
            if not interrupted:
                print("Stopping; waiting for the foreground process to finish...", flush=True)
            interrupted = True


def _write_manifest(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".node-bootstrap-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_environment(root: Path) -> Path:
    # Probe only after locking: a concurrent launch may have just finished pip.
    with bootstrap_lock(root):
        return _prepare_environment_locked(root)


def _prepare_environment_locked(root: Path) -> Path:
    python = environment_python(root)
    current = probe_python(python, root) if python.is_file() else None
    if not valid_python(current):
        print("Preparing the project's Python 3.11+ virtual environment...", flush=True)
        command = [sys.executable, "-m", "venv"]
        if (root / ".venv").exists():
            command.append("--upgrade")
        command.append(str(root / ".venv"))
        if run_foreground(command, root) != 0:
            raise BootstrapError(
                "Virtual environment setup failed. Install Python's venv/pip support "
                "(for example python3-venv on Debian/Ubuntu), then try again."
            )
        current = probe_python(python, root)
        if not valid_python(current):
            raise BootstrapError(
                "The project virtual environment is unusable. Check your Python installation; "
                "recreate only this project's .venv folder if necessary."
            )
    expected = {"fingerprint": fingerprint(root), "python": current["version"]}
    manifest = root / ".venv" / MANIFEST
    try:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if cached == expected:
        ready = probe_python(python, root, dependencies=True)
        if valid_python(ready) and not ready["missing"]:
            return python
    manifest.unlink(missing_ok=True)
    print("Installing project dependencies (first launch or project update)...", flush=True)
    commands = [
        [str(python), "-m", "pip", "install", "-r", str(root / "requirements.lock")],
        [str(python), "-m", "pip", "install", "--no-deps", "-e", str(root)],
    ]
    for command in commands:
        if run_foreground(command, root) != 0:
            raise BootstrapError(
                "Dependency installation failed; the node manager was not started. "
                "Check the pip error above and your network/proxy, then run the launcher again. "
                "An already prepared environment can be used without internet."
            )
    ready = probe_python(python, root, dependencies=True)
    if not valid_python(ready) or ready["missing"]:
        missing = ", ".join(ready["missing"]) if ready else "Python runtime"
        raise BootstrapError(
            f"Dependencies are still missing or unusable: {missing}. "
            "Check the installation errors above and run the launcher again."
        )
    _write_manifest(manifest, expected)
    return python


def _offline_help() -> None:
    print(
        "Usage: start-node [OPTIONS]\n\n"
        "Start the local Codex Anywhere node management page in the foreground.\n"
        "The browser opens automatically; an already paired node connects automatically.\n\n"
        "  --port INTEGER       Local management port (default: 5780).\n"
        "  --state-dir PATH     Node state directory (default: ~/.codex-anywhere).\n"
        "  --codex-bin PATH     Existing Codex executable (default: codex).\n"
        "  --no-open            Do not open the browser.\n"
        "  --help               Show help without installing dependencies.\n\n"
        "Install Python 3.11+ and Codex first. This launcher installs project dependencies\n"
        "in .venv on its first normal run; it does not install system Python or Codex."
    )


def launch(root: Path, arguments: list[str]) -> int:
    root = root.resolve()
    if "--help" in arguments or "-h" in arguments:
        python = environment_python(root)
        if not python.is_file():
            python = Path(sys.executable)
        ready = probe_python(python, root, dependencies=True)
        if not valid_python(ready) or ready["missing"]:
            _offline_help()
            return 0
    else:
        python = prepare_environment(root)
        print("Starting the local node management page. Keep this window open.", flush=True)
    return run_foreground(
        [str(python), "-m", "codex_anywhere.cli", "node", "web", *arguments], root
    )


def main(arguments: list[str] | None = None) -> int:
    if sys.version_info < MINIMUM_PYTHON:
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 1
    try:
        return launch(Path(__file__).resolve().parents[1], list(sys.argv[1:] if arguments is None else arguments))
    except (BootstrapError, OSError) as exc:
        print(f"Node launcher: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
