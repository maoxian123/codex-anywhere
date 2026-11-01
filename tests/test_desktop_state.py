from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from codex_anywhere.node import desktop_state
from codex_anywhere.node.desktop_state import DesktopState, directory_key
from codex_anywhere.node.ownership import _try_lock, _unlock


@pytest.fixture
def state(tmp_path, monkeypatch):
    result = DesktopState(tmp_path)
    result.path.write_text(
        json.dumps({"local-projects": {}, "project-order": [], "selected-project": None}),
        encoding="utf-8",
    )
    monkeypatch.setattr(result, "_desktop_running", lambda: False)
    return result


def native(cwd="/work/project", project_id="opaque-project-id"):
    return {
        "id": project_id, "name": "Project", "roots": [{"path": cwd}],
        "createdAt": 123, "updatedAt": 456,
    }


def contents(state):
    return json.loads(state.path.read_bytes())


def test_merge_preserves_unknown_keys_and_active_selection(state):
    old = {
        "local-projects": {}, "project-order": ["other"],
        "selected-project": {"type": "remote", "projectId": "other"},
        "active-workspace-roots": ["/another"], "unknown": {"nested": [1, True, None]},
        "electron-saved-workspace-roots": ["/legacy"],
    }
    state.path.write_text(json.dumps(old), encoding="utf-8")
    original = state.path.read_bytes()
    native_backup = Path(f"{state.path}.bak")
    native_backup.write_bytes(b"retain the desktop backup")
    assert state.check("/work/project") == {"status": "ready"}
    assert state.merge("/work/project", native()) == {
        "status": "registered", "refresh_required": True,
    }
    actual = contents(state)
    assert actual["local-projects"] == {
        "opaque-project-id": {
            "id": "opaque-project-id", "name": "Project", "rootPaths": ["/work/project"],
            "createdAt": 123000, "updatedAt": 456000,
        }
    }
    assert actual["project-order"] == ["opaque-project-id", "other"]
    for key in old.keys() - {"local-projects", "project-order"}:
        assert actual[key] == old[key]
    backup = Path(f"{state.path}.codex-anywhere.bak")
    assert backup.read_bytes() == original
    assert native_backup.read_bytes() == b"retain the desktop backup"
    assert not list(state.home.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(state.path.stat().st_mode) == 0o600


def test_existing_windows_root_keeps_name_id_and_unknown_fields(state):
    old = {
        "local-projects": {
            "existing": {
                "id": "existing", "name": "Keep this name", "rootPaths": [r"F:\Work\Project"],
                "createdAt": 1, "updatedAt": 2, "unknown": "keep",
            },
        },
        "project-order": ["existing"],
    }
    state.path.write_text(json.dumps(old), encoding="utf-8")
    original = state.path.read_bytes()
    expected = {"status": "already_registered", "refresh_required": False}
    assert state.check("f:/work/PROJECT/") == expected
    assert state.merge("f:/work/PROJECT/", native("f:/work/PROJECT/")) == expected
    assert state.path.read_bytes() == original
    assert not Path(f"{state.path}.codex-anywhere.bak").exists()


@pytest.mark.parametrize(("path", "equivalent"), [
    (r"\\?\F:\Work\..\Project" + "\\", "f:/project"),
    (r"\\?\UNC\server\share\Folder", r"\\SERVER\SHARE\folder"),
    ("/work/../project/", "/project"),
])
def test_directory_keys(path, equivalent):
    assert directory_key(path) == directory_key(equivalent)


def test_posix_directory_keys_are_case_sensitive():
    assert directory_key("/Work/Project") != directory_key("/work/project")


def test_extended_unc_prefix_is_case_insensitive():
    assert directory_key(r"\\?\unc\SERVER\Share\folder") == directory_key(
        r"\\server\share\FOLDER"
    )


def test_new_windows_root_uses_native_separators(state):
    assert state.merge("F:/work/project", native("F:/work/project"))["status"] == "registered"
    assert contents(state)["local-projects"]["opaque-project-id"]["rootPaths"] == [
        r"F:\work\project",
    ]


@pytest.mark.parametrize("raw", [
    b"{", b"[]", b"{}", b'{"local-projects": [], "project-order": []}',
    b'{"local-projects": {}, "project-order": [1]}',
    b'{"local-projects": {}, "project-order": null}',
    b'{"local-projects": {}, "project-order": [], "extra": NaN}',
    b'{"local-projects": {}, "local-projects": {}, "project-order": []}',
    b'{"local-projects": {"bad": {}}, "project-order": []}',
])
def test_invalid_state_is_never_replaced(state, raw):
    state.path.write_bytes(raw)
    expected = {"status": "error", "reason": "state_invalid"}
    assert state.check("/work/project") == expected
    assert state.merge("/work/project", native()) == expected
    assert state.path.read_bytes() == raw


def test_missing_state_is_not_created(tmp_path):
    state = DesktopState(tmp_path / "missing")
    expected = {"status": "unavailable", "reason": "desktop_not_found"}
    assert state.check("/work/project") == expected
    assert state.merge("/work/project", native()) == expected
    assert not state.home.exists()


def test_oversized_state_is_rejected_without_reading_truncated_json(state, monkeypatch):
    original = state.path.read_bytes()
    monkeypatch.setattr(desktop_state, "MAX_STATE_BYTES", len(original) - 1)
    assert state.check("/work/project") == {"status": "error", "reason": "state_invalid"}
    assert state.merge("/work/project", native()) == {
        "status": "error", "reason": "state_invalid",
    }
    assert state.path.read_bytes() == original


def test_modern_invalid_schema_does_not_fall_back_to_legacy(state):
    state.path.write_text(json.dumps({
        "local-projects": [], "project-order": [], "electron-saved-workspace-roots": [],
    }), encoding="utf-8")
    assert state.check("/work/project") == {"status": "error", "reason": "state_invalid"}


def test_legacy_only_is_explicitly_unsupported(state):
    state.path.write_text(json.dumps({"electron-saved-workspace-roots": []}), encoding="utf-8")
    assert state.check("/work/project") == {
        "status": "unavailable", "reason": "unsupported_state",
    }


@pytest.mark.parametrize(("running", "reason"), [
    (True, "desktop_running"), (None, "desktop_state_unverified"),
])
def test_running_or_unverified_desktop_is_pending(state, monkeypatch, running, reason):
    monkeypatch.setattr(state, "_desktop_running", lambda: running)
    expected = {"status": "pending", "reason": reason}
    assert state.check("/work/project") == expected
    assert state.merge("/work/project", native()) == expected
    assert contents(state)["local-projects"] == {}


def test_desktop_starting_during_merge_does_not_write_state(state, monkeypatch):
    calls = iter([False, True])
    monkeypatch.setattr(state, "_desktop_running", lambda: next(calls))
    original = state.path.read_bytes()
    assert state.merge("/work/project", native()) == {
        "status": "pending", "reason": "desktop_running",
    }
    assert state.path.read_bytes() == original
    assert not Path(f"{state.path}.codex-anywhere.bak").exists()


def test_external_edit_during_merge_is_preserved(state, monkeypatch):
    original_temp = state._temp
    external = json.dumps({**contents(state), "external": "preserve"}).encode()

    def edit(contents):
        result = original_temp(contents)
        state.path.write_bytes(external)
        return result

    monkeypatch.setattr(state, "_temp", edit)
    assert state.merge("/work/project", native()) == {
        "status": "pending", "reason": "state_changed",
    }
    assert state.path.read_bytes() == external


def test_external_deletion_during_merge_is_pending(state, monkeypatch):
    original_temp = state._temp

    def remove(contents):
        result = original_temp(contents)
        state.path.unlink()
        return result

    monkeypatch.setattr(state, "_temp", remove)
    assert state.merge("/work/project", native()) == {
        "status": "pending", "reason": "state_changed",
    }
    assert not state.path.exists()


def test_desktop_starting_after_backup_still_prevents_state_replace(state, monkeypatch):
    calls = iter([False, False, True])
    monkeypatch.setattr(state, "_desktop_running", lambda: next(calls))
    original = state.path.read_bytes()
    assert state.merge("/work/project", native()) == {
        "status": "pending", "reason": "desktop_running",
    }
    assert state.path.read_bytes() == original
    assert Path(f"{state.path}.codex-anywhere.bak").read_bytes() == original


def test_readback_must_match_before_registration_is_reported(state, monkeypatch):
    actual_read = desktop_state._read_regular

    def read(path):
        result = actual_read(path)
        if path == state.path and b"opaque-project-id" in result:
            return result + b" "
        return result

    monkeypatch.setattr(desktop_state, "_read_regular", read)
    assert state.merge("/work/project", native()) == {
        "status": "pending", "reason": "state_changed",
    }


def test_cross_process_registration_lock_is_respected(state):
    path = state.home / ".codex-anywhere-desktop-state.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert _try_lock(fd)
        assert state.merge("/work/project", native()) == {
            "status": "pending", "reason": "state_changed",
        }
        assert contents(state)["local-projects"] == {}
    finally:
        _unlock(fd)
        os.close(fd)


def test_invalid_project_does_not_change_state(state):
    original = state.path.read_bytes()
    assert state.merge("/work/project", native("/unrelated")) == {
        "status": "error", "reason": "project_invalid",
    }
    assert state.path.read_bytes() == original


def test_state_directory_and_symlinks_are_rejected(state, tmp_path):
    state.path.unlink()
    state.path.mkdir()
    assert state.check("/work/project")["reason"] == "state_invalid"
    state.path.rmdir()
    target = tmp_path / "target.json"
    target.write_text('{"local-projects": {}, "project-order": []}', encoding="utf-8")
    try:
        state.path.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks requires additional Windows permissions")
    assert state.check("/work/project")["reason"] == "state_invalid"


def test_backup_symlink_is_rejected(state, tmp_path):
    target = tmp_path / "target.json"
    target.write_text("do not replace", encoding="utf-8")
    try:
        Path(f"{state.path}.codex-anywhere.bak").symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks requires additional Windows permissions")
    original = state.path.read_bytes()
    assert state.merge("/work/project", native())["reason"] == "state_invalid"
    assert target.read_text(encoding="utf-8") == "do not replace"
    assert state.path.read_bytes() == original


def processes(monkeypatch, items):
    monkeypatch.setattr(desktop_state.psutil, "Process", lambda: SimpleNamespace(username=lambda: "me"))
    monkeypatch.setattr(
        desktop_state.psutil, "process_iter",
        lambda *_args, **_kwargs: [SimpleNamespace(info=item) for item in items],
    )


def test_cli_and_unrelated_inaccessible_processes_do_not_block(tmp_path, monkeypatch):
    processes(monkeypatch, [
        {"name": "codex.exe", "exe": str(tmp_path / "bin" / "codex.exe"), "username": "me"},
        {"name": "system-service", "exe": None, "username": None},
        {"name": None, "exe": None, "username": None},
    ])
    assert DesktopState(tmp_path)._desktop_running() is False


def test_desktop_bundle_is_detected_only_for_same_account(tmp_path, monkeypatch):
    executable = tmp_path / "app" / "Codex.exe"
    resources = executable.parent / "resources"
    resources.mkdir(parents=True)
    (resources / "app.asar").touch()
    item = {"name": "codex.exe", "exe": str(executable), "username": "me"}
    processes(monkeypatch, [item])
    assert DesktopState(tmp_path)._desktop_running() is True
    processes(monkeypatch, [{**item, "username": "somebody-else"}])
    assert DesktopState(tmp_path)._desktop_running() is False
    processes(monkeypatch, [{**item, "username": None}])
    assert DesktopState(tmp_path)._desktop_running() is None


def test_unverified_codex_and_failed_process_snapshot_are_unknown(tmp_path, monkeypatch):
    processes(monkeypatch, [{"name": "codex.exe", "exe": None, "username": "me"}])
    assert DesktopState(tmp_path)._desktop_running() is None

    def denied(*_args, **_kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(desktop_state.psutil, "process_iter", denied)
    assert DesktopState(tmp_path)._desktop_running() is None


def test_mac_and_linux_bundle_layouts(tmp_path):
    mac = tmp_path / "Codex.app" / "Contents"
    (mac / "Resources").mkdir(parents=True)
    (mac / "Resources" / "app.asar").touch()
    assert desktop_state._desktop_executable(str(mac / "MacOS" / "Codex"))
    linux = tmp_path / "codex"
    (linux / "resources").mkdir(parents=True)
    (linux / "resources" / "app.asar").touch()
    assert desktop_state._desktop_executable(str(linux / "codex"))
