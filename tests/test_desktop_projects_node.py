"""Native project registration must also confirm the desktop sidebar state."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from codex_anywhere.node.desktop_projects import DesktopProjects
from codex_anywhere.node.desktop_state import DesktopState
from codex_anywhere.node.runtime import RpcError, RuntimeGone
from codex_anywhere.node.service import NodeService
from codex_anywhere.node.store import NodeStore

UNSET = object()


def native_project(cwd, *, identifier="native-project", name="原生项目"):
    return {
        "id": identifier,
        "name": name,
        "roots": [{"path": cwd}],
        "metadata": {"preserve": "native metadata"},
        "createdAt": 1,
        "updatedAt": 2,
        "position": 0,
        "recencyAt": None,
    }


class State:
    def __init__(self, check=None, merge=None):
        self.check_result = check or {"status": "ready"}
        self.merge_result = merge or {"status": "registered", "refresh_required": True}
        self.checked = []
        self.merged = []

    def check(self, cwd):
        self.checked.append(cwd)
        return copy.deepcopy(self.check_result)

    def merge(self, cwd, project):
        self.merged.append((cwd, copy.deepcopy(project)))
        if self.merge_result["status"] in {"registered", "already_registered"}:
            self.check_result = {"status": "already_registered", "refresh_required": False}
        return copy.deepcopy(self.merge_result)


class Peer:
    """A strict RPC peer: unexpected inference/configuration calls fail the test."""

    def __init__(self, *, pages=None, created=UNSET, list_error=None, create_error=None):
        self.runtime_id = "desktop-runtime"
        self.process = SimpleNamespace(returncode=None)
        self.loaded_threads = set()
        self.server_requests = {}
        self.calls = []
        self.pages = pages or {None: {"data": [], "nextCursor": None}}
        self.created = created
        self.list_error = list_error
        self.create_error = create_error
        self.before_create = None

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "project/list":
            if self.list_error:
                raise self.list_error
            return copy.deepcopy(self.pages[params.get("cursor")])
        if method == "project/create":
            if self.before_create:
                self.before_create()
            if self.create_error:
                raise self.create_error
            project = native_project(params["roots"][0]["path"], name=params["name"])
            return {"project": project} if self.created is UNSET else copy.deepcopy(self.created)
        raise AssertionError(f"Unexpected RPC {method}")


@pytest.fixture
def project_path(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return str(path.resolve())


@pytest.fixture
def store(tmp_path):
    journal = NodeStore(tmp_path / "node.sqlite3")
    yield journal
    journal.close()


def only_row(store):
    rows = store.db.execute("SELECT cwd_key FROM desktop_projects").fetchall()
    assert len(rows) == 1
    return store.desktop_project(rows[0]["cwd_key"])


@pytest.mark.parametrize(
    "status,reason",
    [
        ("already_registered", None),
        ("pending", "desktop_running"),
        ("unavailable", "desktop_not_found"),
        ("error", "state_invalid"),
    ],
)
def test_desktop_state_short_circuits_native_calls(store, project_path, status, reason):
    async def scenario():
        check = {"status": status, **({"reason": reason} if reason else {})}
        state = State(check=check)
        peer = Peer()
        result = await DesktopProjects(store, state).register(peer, project_path, "Web name")
        assert result["status"] == status
        if reason:
            assert result["reason"] == reason
        assert peer.calls == []
        assert state.merged == []
        if status == "pending":
            assert only_row(store)["status"] == "pending"
            assert only_row(store)["native_project"] is None

    asyncio.run(scenario())


def test_missing_runtime_does_not_claim_desktop_registration(store, project_path):
    async def scenario():
        state = State()
        result = await DesktopProjects(store, state).register(None, project_path, "Web name")
        assert result == {"status": "unavailable", "reason": "runtime_unavailable"}
        assert state.merged == []

    asyncio.run(scenario())


def test_native_catalog_paginates_and_reuses_any_matching_root_without_renaming(
    store, project_path, tmp_path
):
    async def scenario():
        existing = native_project(str(tmp_path / "other-root"), name="Native custom name")
        existing["roots"].append({"path": project_path + os.sep + "."})
        existing["futureNativeField"] = {"keep": True}
        peer = Peer(
            pages={
                None: {
                    "data": [native_project(str(tmp_path / "unrelated"), identifier="other")],
                    "nextCursor": "second",
                },
                "second": {"data": [existing], "nextCursor": None},
            }
        )
        state = State()
        result = await DesktopProjects(store, state).register(peer, project_path, "Web override")
        assert result["status"] == "registered"
        assert [method for method, _ in peer.calls] == ["project/list", "project/list"]
        assert [params.get("cursor") for _, params in peer.calls] == [None, "second"]
        assert all(params["limit"] == 100 for _, params in peer.calls)
        assert state.merged == [(project_path, existing)]
        assert only_row(store)["native_project"] == existing
        assert only_row(store)["status"] == "complete"

    asyncio.run(scenario())


def test_create_is_journaled_before_rpc_and_sidebar_confirmation_controls_success(
    store, project_path, tmp_path
):
    async def scenario():
        state = State(merge={"status": "pending", "reason": "state_changed"})
        peer = Peer()

        def before_create():
            with sqlite3.connect(tmp_path / "node.sqlite3") as persisted:
                assert persisted.execute("SELECT status FROM desktop_projects").fetchone() == (
                    "started",
                )

        peer.before_create = before_create
        result = await DesktopProjects(store, state).register(peer, project_path, "项目名称")
        assert result == {"status": "pending", "reason": "state_changed"}
        creates = [params for method, params in peer.calls if method == "project/create"]
        assert len(creates) == 1
        assert set(creates[0]) == {"name", "roots", "metadata", "idempotencyKey"}
        assert creates[0]["name"] == "项目名称"
        assert creates[0]["roots"] == [{"path": project_path}]
        assert creates[0]["metadata"] == {}
        assert isinstance(creates[0]["idempotencyKey"], str) and creates[0]["idempotencyKey"]
        assert only_row(store)["status"] == "pending"
        assert only_row(store)["native_project"]["id"] == "native-project"

    asyncio.run(scenario())


def test_flush_reuses_known_native_project_after_merge_was_deferred(store, project_path):
    async def scenario():
        state = State(merge={"status": "pending", "reason": "desktop_running"})
        peer = Peer()
        registration = DesktopProjects(store, state)
        assert (await registration.register(peer, project_path, "Project"))["status"] == "pending"
        previous_calls = list(peer.calls)
        native = only_row(store)["native_project"]
        state.merge_result = {"status": "registered", "refresh_required": True}
        await registration.flush(peer)
        assert peer.calls == previous_calls
        assert state.merged[-1] == (project_path, native)
        assert only_row(store)["status"] == "complete"
        assert only_row(store)["result"]["status"] == "registered"
        await registration.flush(peer)
        assert peer.calls == previous_calls
        assert len(state.merged) == 2

    asyncio.run(scenario())


def test_pending_registration_survives_store_reopen_then_flushes_without_browser(
    tmp_path, project_path
):
    async def scenario():
        path = tmp_path / "durable.sqlite3"
        first = NodeStore(path)
        state = State(check={"status": "pending", "reason": "desktop_running"})
        peer = Peer()
        try:
            result = await DesktopProjects(first, state).register(peer, project_path, "Saved name")
            assert result["status"] == "pending" and peer.calls == []
        finally:
            first.close()
        second = NodeStore(path)
        try:
            state.check_result = {"status": "ready"}
            await DesktopProjects(second, state).flush(peer)
            assert only_row(second)["status"] == "complete"
            assert [method for method, _ in peer.calls] == ["project/list", "project/create"]
            assert state.merged[0][1]["name"] == "Saved name"
        finally:
            second.close()

    asyncio.run(scenario())


def test_known_native_project_survives_restart_without_repeating_create(tmp_path, project_path):
    async def scenario():
        path = tmp_path / "restart.sqlite3"
        state = State(merge={"status": "pending", "reason": "state_changed"})
        first = NodeStore(path)
        try:
            result = await DesktopProjects(first, state).register(Peer(), project_path, "Project")
            assert result["status"] == "pending"
            native = only_row(first)["native_project"]
        finally:
            first.close()
        second = NodeStore(path)
        peer = Peer()
        try:
            state.merge_result = {"status": "registered", "refresh_required": True}
            await DesktopProjects(second, state).flush(peer)
            assert peer.calls == []
            assert state.merged[-1] == (project_path, native)
            assert only_row(second)["status"] == "complete"
        finally:
            second.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeGone("secret native diagnostic"), TimeoutError()])
def test_uncertain_create_never_retries_from_background(store, project_path, failure):
    async def scenario():
        state = State()
        peer = Peer(create_error=failure)
        registration = DesktopProjects(store, state)
        result = await registration.register(peer, project_path, "Project")
        assert result == {"status": "error", "reason": "outcome_unknown"}
        assert only_row(store)["status"] == "unknown"
        assert state.merged == []
        calls = list(peer.calls)
        await registration.flush(peer)
        await DesktopProjects(store, state).flush(peer)
        assert peer.calls == calls
        assert "secret" not in str(result)

    asyncio.run(scenario())


def test_explicit_retry_after_unknown_reuses_stable_cwd_idempotency_key(store, project_path):
    async def scenario():
        state = State()
        peer = Peer(create_error=RuntimeGone("unknown"))
        registration = DesktopProjects(store, state)
        await registration.register(peer, project_path, "First name")
        key = next(
            params["idempotencyKey"] for method, params in peer.calls if method == "project/create"
        )
        peer.create_error = None
        result = await registration.register(peer, project_path, "Updated name")
        creates = [params for method, params in peer.calls if method == "project/create"]
        assert result["status"] == "registered"
        assert len(creates) == 2
        assert creates[-1]["idempotencyKey"] == key
        assert creates[-1]["name"] == "Updated name"

    asyncio.run(scenario())


def test_recover_uncertain_excludes_started_rows_from_background_retry(store, project_path):
    async def scenario():
        store.queue_desktop_project("cwd-key", project_path, "Project")
        store.update_desktop_project("cwd-key", "started")
        store.recover_uncertain()
        assert store.desktop_project("cwd-key")["status"] == "unknown"
        peer = Peer()
        state = State()
        await DesktopProjects(store, state).flush(peer)
        assert peer.calls == [] and state.merged == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,reason",
    [
        (RpcError("Method not found", code=-32601), "native_unsupported"),
        (RpcError("SECRET PATH AND TOKEN", code=-32000), "registration_failed"),
    ],
)
def test_definitive_native_failure_is_sanitized_and_not_background_retried(
    store, project_path, failure, reason
):
    async def scenario():
        state = State()
        peer = Peer(list_error=failure)
        registration = DesktopProjects(store, state)
        result = await registration.register(peer, project_path, "Project")
        assert result["status"] in {"unavailable", "error"}
        assert result["reason"] == reason
        assert "SECRET" not in str(result)
        assert state.merged == []
        calls = list(peer.calls)
        await registration.flush(peer)
        assert peer.calls == calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "page",
    [{}, {"data": None}, {"data": {}}, {"data": [None]}, {"data": [{"id": "missing-roots"}]}],
)
def test_invalid_catalog_cannot_trigger_project_create(store, project_path, page):
    async def scenario():
        peer = Peer(pages={None: page})
        state = State()
        result = await DesktopProjects(store, state).register(peer, project_path, "Project")
        assert result["status"] == "error"
        assert result["reason"] in {"invalid_response", "catalog_incomplete"}
        assert [method for method, _ in peer.calls] == ["project/list"]
        assert state.merged == []

    asyncio.run(scenario())


def test_repeated_catalog_cursor_is_incomplete_and_never_creates(store, project_path):
    async def scenario():
        peer = Peer(
            pages={
                None: {"data": [], "nextCursor": "loop"},
                "loop": {"data": [], "nextCursor": "loop"},
            }
        )
        state = State()
        result = await DesktopProjects(store, state).register(peer, project_path, "Project")
        assert result == {"status": "error", "reason": "catalog_incomplete"}
        assert len(peer.calls) <= 3
        assert all(method == "project/list" for method, _ in peer.calls)
        assert state.merged == []

    asyncio.run(scenario())


@pytest.mark.parametrize("created", [{}, {"project": None}, {"project": {"id": "incomplete"}}])
def test_invalid_created_project_is_never_reported_as_registered(store, project_path, created):
    async def scenario():
        peer = Peer(created=created)
        state = State()
        result = await DesktopProjects(store, state).register(peer, project_path, "Project")
        # The create may have taken effect even when its response cannot be
        # verified. Retain that uncertainty instead of retrying automatically.
        assert result == {"status": "error", "reason": "outcome_unknown"}
        assert only_row(store)["status"] == "unknown"
        assert state.merged == []
        assert sum(method == "project/create" for method, _ in peer.calls) == 1
        calls = list(peer.calls)
        await DesktopProjects(store, state).flush(peer)
        assert peer.calls == calls

    asyncio.run(scenario())


def test_concurrent_explicit_registrations_create_and_merge_only_once(store, project_path):
    async def scenario():
        state = State()
        peer = Peer()
        registration = DesktopProjects(store, state)
        results = await asyncio.gather(
            registration.register(peer, project_path, "Project"),
            registration.register(peer, project_path, "Project"),
        )
        assert all(result["status"] in {"registered", "already_registered"} for result in results)
        assert sum(method == "project/create" for method, _ in peer.calls) == 1
        assert len(state.merged) == 1

    asyncio.run(scenario())


def test_project_open_succeeds_while_desktop_is_running_and_persists_registration(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"},
            tmp_path / "node",
        )
        state = State(check={"status": "pending", "reason": "desktop_running"})
        peer = Peer()
        service.runtime = peer
        service.desktop_projects = DesktopProjects(service.store, state)
        path = tmp_path / "new-project"
        try:
            result = await service._execute(
                {
                    "id": "open",
                    "kind": "project.open",
                    "payload": {"cwd": str(path), "name": "Web name", "create_directory": True},
                }
            )
            assert path.is_dir()
            assert result["cwd"] == str(path.resolve())
            assert result["name"] == "Web name"
            assert result["desktop_registration"] == {
                "status": "pending",
                "reason": "desktop_running",
            }
            assert only_row(service.store)["status"] == "pending"
            assert peer.calls == []
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_project_desktop_resolves_existing_directory_and_never_creates_missing_one(tmp_path):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"},
            tmp_path / "node",
        )
        state = State(check={"status": "already_registered", "refresh_required": False})
        service.desktop_projects = DesktopProjects(service.store, state)
        service.runtime = Peer()
        existing = tmp_path / "existing"
        existing.mkdir()
        missing = tmp_path / "missing"
        try:
            result = await service._execute(
                {
                    "id": "sync",
                    "kind": "project.desktop",
                    "payload": {"cwd": str(existing / "."), "name": "Saved name"},
                }
            )
            assert result["cwd"] == str(existing.resolve())
            assert result["desktop_registration"]["status"] == "already_registered"
            with pytest.raises((ValueError, OSError)):
                await service._execute(
                    {
                        "id": "missing",
                        "kind": "project.desktop",
                        "payload": {
                            "cwd": str(missing),
                            "name": "Saved name",
                            "create_directory": True,
                        },
                    }
                )
            assert not missing.exists()
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_service_waits_for_desktop_close_then_confirms_real_sidebar_file(tmp_path, monkeypatch):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"},
            tmp_path / "node",
        )
        home = tmp_path / "desktop-home"
        home.mkdir()
        state_path = home / ".codex-global-state.json"
        original = {"local-projects": {}, "project-order": [], "custom-preference": "preserve"}
        state_path.write_text(json.dumps(original), encoding="utf-8")
        initial_bytes = state_path.read_bytes()
        state = DesktopState(home)
        running = True
        monkeypatch.setattr(state, "_desktop_running", lambda: running)
        service.desktop_projects = DesktopProjects(service.store, state)
        peer = Peer()
        service.runtime = peer
        project = tmp_path / "created-project"
        try:
            opened = await service._execute(
                {
                    "id": "open",
                    "kind": "project.open",
                    "payload": {
                        "cwd": str(project),
                        "name": "Web project",
                        "create_directory": True,
                    },
                }
            )
            assert project.is_dir()
            assert opened["desktop_registration"] == {
                "status": "pending",
                "reason": "desktop_running",
            }
            assert state_path.read_bytes() == initial_bytes
            assert peer.calls == []

            running = False
            finished = asyncio.Event()
            flush = service.desktop_projects.flush

            async def flush_and_signal(runtime):
                await flush(runtime)
                finished.set()

            monkeypatch.setattr(service.desktop_projects, "flush", flush_and_signal)
            assert service.ws is None
            worker = asyncio.create_task(service._sync_desktop_projects())
            try:
                await asyncio.wait_for(finished.wait(), 3)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            assert saved["custom-preference"] == "preserve"
            assert saved["project-order"] == ["native-project"]
            entry = saved["local-projects"]["native-project"]
            assert entry["name"] == "Web project"
            assert entry["rootPaths"] == [str(project.resolve())]
            assert only_row(service.store)["result"]["status"] == "registered"
            calls = list(peer.calls)
            repeated = await service._execute(
                {
                    "id": "sync",
                    "kind": "project.desktop",
                    "payload": {"cwd": str(project), "name": "Unwanted rename"},
                }
            )
            assert repeated["desktop_registration"]["status"] == "already_registered"
            assert peer.calls == calls
            assert json.loads(state_path.read_text(encoding="utf-8")) == saved
        finally:
            service.store.close()

    asyncio.run(scenario())
