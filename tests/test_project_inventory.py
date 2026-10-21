import asyncio
import os
from uuid import uuid4

from codex_anywhere.hub.models import Project, Thread, db
from codex_anywhere.hub.transport import handle_node_message
from codex_anywhere.node.fingerprints import history_fingerprint
from codex_anywhere.node.history import list_threads
from codex_anywhere.node.runtime import RpcError
from codex_anywhere.node.store import NodeStore
from codex_anywhere.node.sync import CatalogSynchronizer, SyncSettings


def test_project_inventory_pages_active_and_archived_without_reading_histories(tmp_path):
    class Peer:
        runtime_id = "runtime"

        def __init__(self):
            self.calls = []

        async def request(self, method, params):
            assert method == "thread/list"
            self.calls.append(params)
            if params.get("cursor"):
                return {"data": [{"cwd": str(tmp_path / "older")}]}
            if params["archived"]:
                return {"data": [{"cwd": str(tmp_path / "archived")}]}
            return {"data": [{"cwd": str(tmp_path / "recent")}], "nextCursor": "older-page"}

    async def run():
        peer = Peer()
        store = NodeStore(tmp_path / "node.sqlite3")
        events = []

        def wake():
            for event in store.pending_events():
                events.append(event)
                store.acknowledge(event["event_id"])

        sync = CatalogSynchronizer(
            peer,
            store,
            wake,
            SyncSettings(include_projects=True),
            is_current=lambda: True,
            foreground_busy=lambda: False,
        )
        try:
            await sync._run_projects()
            assert len(peer.calls) == 3
            assert peer.calls[1]["cursor"] == "older-page"
            assert peer.calls[2]["archived"] is True
            assert [event["params"]["projects"][0]["cwd"] for event in events] == [
                str(tmp_path / value) for value in ("recent", "older", "archived")
            ]
            assert events[-1]["params"]["complete"] is True
        finally:
            store.close()

    asyncio.run(run())


def test_inventory_only_adds_projects_and_preserves_names(app, api, paired_host):
    host = paired_host()
    cwd = host["root"]
    event = {
        "type": "event",
        "runtime_id": host["runtime_id"],
        "event_id": uuid4().hex,
        "method": "anywhere/project_catalog",
        "params": {"projects": [{"cwd": cwd}]},
    }
    handle_node_message(app, host["host"]["id"], event)
    with app.app_context():
        project = Project.query.one()
        project.name = "My chosen name"
        db.session.commit()
        assert Thread.query.count() == 0
    handle_node_message(app, host["host"]["id"], dict(event, event_id=uuid4().hex))
    assert api("GET", "/api/projects").get_json()["projects"][0]["name"] == "My chosen name"


def test_source_fingerprint_detects_same_second_changes(tmp_path):
    source = tmp_path / "rollout.jsonl"
    source.write_text("first")
    thread = {"id": "native", "updatedAt": 100, "path": str(source)}
    first = history_fingerprint(thread)
    timestamp = source.stat().st_mtime_ns
    source.write_text("later")
    os.utime(source, ns=(timestamp, timestamp + 1_000_000))
    assert history_fingerprint(thread) != first
    assert thread["updatedAt"] == 100


def test_legacy_catalog_peer_can_reject_optional_index_fast_path():
    class Peer:
        def __init__(self):
            self.calls = []

        async def request(self, method, params):
            self.calls.append(dict(params))
            if "useStateDbOnly" in params:
                raise RpcError("unknown field useStateDbOnly", code=-32602)
            return {"data": []}

    async def run():
        peer = Peer()
        assert await list_threads(peer, {"indexed": True}) == {"data": []}
        assert peer.calls[0]["useStateDbOnly"] is True
        assert "useStateDbOnly" not in peer.calls[1]

    asyncio.run(run())
