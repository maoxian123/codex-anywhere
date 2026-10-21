"""Browse directories and open projects using the node account's OS access."""

from __future__ import annotations

import os
from pathlib import Path

from .config import resolve_path

MAX_DIRECTORY_ENTRIES = 2000
MAX_DIRECTORIES = 500


def locations() -> list[dict]:
    result = [{"name": "Home", "path": str(Path.home().resolve())}]
    if os.name == "nt":
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                drive = f"{chr(ord('A') + index)}:\\"
                result.append({"name": drive, "path": drive})
    else:
        result.append({"name": "/", "path": "/"})
    return result


def list_directories(payload: dict) -> dict:
    path = resolve_path(payload.get("path") or str(Path.home()), directory=True)
    directories = []
    truncated = False
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DIRECTORY_ENTRIES or len(directories) >= MAX_DIRECTORIES:
                truncated = True
                break
            try:
                if entry.is_dir():
                    directories.append(
                        {"name": entry.name, "path": str(Path(entry.path).resolve())}
                    )
            except OSError:
                continue
    directories.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "home": str(Path.home().resolve()),
        "locations": locations(),
        "directories": directories,
        "truncated": truncated,
    }


def open_project(payload: dict) -> dict:
    create = payload.get("create_directory", False)
    if not isinstance(create, bool):
        raise ValueError("create_directory must be a boolean")
    if create:
        path = resolve_path(payload["cwd"], directory=True, must_exist=False)
        path.mkdir(parents=True, exist_ok=True)
    path = resolve_path(payload["cwd"], directory=True)
    name = payload.get("name") or path.name or path.anchor
    if not isinstance(name, str):
        raise ValueError("Project name must be text")
    return {"cwd": str(path), "name": name.strip()[:240] or path.name or str(path)}
