"""Conversation organization changes only the durable hub projection."""

from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Event, Host, Project, Thread, db
from codex_anywhere.hub.projects import ensure_project
from codex_anywhere.hub.transport import handle_node_message


@pytest.fixture
def conversations(app, paired_host):
    host = paired_host()
    with app.app_context():
        project = ensure_project(db.session.get(Host, host["host"]["id"]), host["root"])
        rows = []
        for index in range(4):
            thread = Thread(
                host_id=project.host_id,
                project_id=project.id,
                native_id=f"native-{index}",
                title=f"Original {index}",
                cwd=host["root"],
                status="idle",
                created_at=100 + index,
                updated_at=200 + index,
            )
            db.session.add(thread)
            db.session.flush()
            rows.append(thread.as_dict())
        db.session.commit()
        project_id = project.id
    return host, project_id, rows


def test_preferences_require_authentication_and_csrf(app, logged_in, conversations):
    thread = conversations[2][0]
    path = f"/api/threads/{thread['id']}/preferences"
    assert app.test_client().patch(path, json={"pinned": True}).status_code == 401
    assert logged_in.patch(path, json={"pinned": True}).status_code == 403
    with app.app_context():
        assert not db.session.get(Thread, thread["id"]).pinned


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"title": "Wrong field"},
        {"unknown": True, "pinned": True},
        {"display_title": 123},
        {"display_title": " "},
        {"display_title": "a" * 241},
        {"display_title": "line\nbreak"},
        {"display_title": "null\x00byte"},
        {"pinned": 1},
        {"pinned": "false"},
        {"pinned": None},
        {"archived": 0},
        {"archived": "true"},
        {"archived": None},
        {"display_title": "valid", "archived": []},
    ],
)
def test_invalid_preferences_are_atomic(app, api, conversations, changes):
    thread = conversations[2][0]
    response = api("PATCH", f"/api/threads/{thread['id']}/preferences", json=changes)
    assert response.status_code == 400
    with app.app_context():
        stored = db.session.get(Thread, thread["id"])
        assert stored.display_title is None and not stored.pinned and not stored.archived
        assert not Event.query.filter_by(method="anywhere/thread_preferences").count()


def test_offline_preferences_are_durable_without_runtime_commands(app, api, conversations):
    host, project_id, threads = conversations
    thread = threads[0]
    app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    sent = len(host["socket"].messages)
    response = api(
        "PATCH",
        f"/api/threads/{thread['id']}/preferences",
        json={
            "display_title": "  自定义会话名称  ",
            "pinned": True,
            "archived": True,
        },
    )
    assert response.status_code == 200
    updated = response.get_json()["thread"]
    assert updated["title"] == thread["title"]
    assert updated["display_title"] == "自定义会话名称"
    assert updated["pinned"] and updated["archived"]
    assert updated["status"] == "idle" and updated["native_id"] == thread["native_id"]
    assert updated["updated_at"] == thread["updated_at"]
    assert updated["last_activity_at"] == thread["last_activity_at"]
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"] == updated
    with app.app_context():
        assert Command.query.count() == 0
        event = Event.query.filter_by(method="anywhere/thread_preferences").one()
        assert event.thread_id == thread["id"] and event.host_id == host["host"]["id"]
        assert event.params == {
            "threadId": thread["id"],
            "display_title": "自定义会话名称",
            "pinned": True,
            "archived": True,
        }
        assert db.session.get(Project, project_id).as_dict()["thread_count"] == 3
    assert len(host["socket"].messages) == sent
    # Saving the same state is idempotent and does not emit duplicate refreshes.
    api("PATCH", f"/api/threads/{thread['id']}/preferences", json={"pinned": True})
    with app.app_context():
        assert Event.query.filter_by(method="anywhere/thread_preferences").count() == 1
    assert (
        api("PATCH", "/api/threads/missing/preferences", json={"pinned": True}).status_code == 404
    )


def test_archive_search_pagination_restore_and_project_counts(app, api, conversations):
    _, project_id, threads = conversations
    oldest, _, archived, newest = threads
    api(
        "PATCH",
        f"/api/threads/{oldest['id']}/preferences",
        json={
            "display_title": "Find custom label",
            "pinned": True,
        },
    )
    api("PATCH", f"/api/threads/{archived['id']}/preferences", json={"archived": True})
    first = api("GET", "/api/threads?limit=2").get_json()
    assert [t["id"] for t in first["threads"]] == [oldest["id"], newest["id"]]
    assert first["has_more"] and first["next_offset"] == 2
    last = api("GET", "/api/threads?limit=2&offset=2").get_json()
    assert [t["id"] for t in last["threads"]] == [threads[1]["id"]]
    assert not last["has_more"] and last["next_offset"] is None
    assert len(api("GET", "/api/threads?archived=false").get_json()["threads"]) == 3
    assert api("GET", "/api/threads?archived=true").get_json()["threads"][0]["id"] == archived["id"]
    assert len(api("GET", "/api/threads?archived=all").get_json()["threads"]) == 4
    for search in ("custom label", oldest["title"]):
        result = api("GET", "/api/threads", query_string={"q": search}).get_json()["threads"]
        assert [t["id"] for t in result] == [oldest["id"]]
    assert not api("GET", "/api/threads?q=Original+2").get_json()["threads"]
    assert api("GET", "/api/threads?archived=true&q=Original+2").get_json()["threads"]
    assert api("GET", "/api/threads?archived=invalid").status_code == 400
    projects = api("GET", "/api/projects").get_json()["projects"]
    assert next(p for p in projects if p["id"] == project_id)["thread_count"] == 3
    api("PATCH", f"/api/threads/{archived['id']}/preferences", json={"archived": False})
    api(
        "PATCH",
        f"/api/threads/{oldest['id']}/preferences",
        json={
            "display_title": None,
            "pinned": False,
        },
    )
    restored = api("GET", "/api/threads").get_json()["threads"]
    assert [t["id"] for t in restored] == [t["id"] for t in reversed(threads)]
    assert restored[-1]["display_title"] is None
    assert api("GET", "/api/projects").get_json()["projects"][0]["thread_count"] == 4
    # Even a project containing only archived threads remains selectable as a directory.
    for thread in threads:
        api("PATCH", f"/api/threads/{thread['id']}/preferences", json={"archived": True})
    project = api("GET", "/api/projects").get_json()["projects"][0]
    assert project["thread_count"] == 0 and project["last_activity_at"] == project["created_at"]


def test_catalog_sync_preserves_preferences_and_native_payload(app, api, conversations):
    host, _, threads = conversations
    thread = threads[0]
    api(
        "PATCH",
        f"/api/threads/{thread['id']}/preferences",
        json={
            "display_title": "My label",
            "pinned": True,
            "archived": True,
        },
    )
    native = {
        "id": thread["native_id"],
        "cwd": host["root"],
        "name": "Changed native title",
        "createdAt": 100,
        "updatedAt": 400,
        "unknown": {"preserve": True},
    }
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "event",
                "event_id": uuid4().hex,
                "runtime_id": host["runtime_id"],
                "method": "anywhere/catalog_sync",
                "params": {"threads": [native]},
            },
        )
        assert Event.query.filter_by(method="anywhere/catalog_sync").one().params == {
            "threads": [native]
        }
    saved = api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]
    assert saved["title"] == native["name"] and saved["native_updated_at"] == 400
    assert saved["display_title"] == "My label" and saved["pinned"] and saved["archived"]


@pytest.mark.parametrize("status", ["sent", "running", "completed", "failed", "unknown"])
def test_creation_request_id_reuses_original_command_even_offline(app, api, paired_host, status):
    host = paired_host()
    payload = {
        "host_id": host["host"]["id"],
        "cwd": host["root"],
        "title": "First message",
        "client_request_id": str(uuid4()),
    }
    first_response = api("POST", "/api/threads", json=payload)
    assert first_response.status_code == 202
    first = first_response.get_json()
    with app.app_context():
        command = db.session.get(Command, first["command"]["id"])
        command.status = status
        db.session.commit()
    sent = len(host["socket"].messages)
    app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    replay = api("POST", "/api/threads", json=payload)
    assert replay.status_code == 202
    assert replay.get_json()["thread"]["id"] == first["thread"]["id"]
    assert replay.get_json()["command"]["id"] == first["command"]["id"]
    assert replay.get_json()["command"]["status"] == status
    assert api("POST", "/api/threads", json={**payload, "title": "Different"}).status_code == 409
    with app.app_context():
        assert Command.query.count() == 1 and Thread.query.count() == 1
    assert len(host["socket"].messages) == sent


@pytest.mark.parametrize("client_id", [None, 42, "", "a" * 201])
def test_creation_rejects_invalid_request_id(api, client_id):
    assert api("POST", "/api/threads", json={"client_request_id": client_id}).status_code == 400
