import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command as migration_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from codex_anywhere.hub.models import Command, Item, Thread, db
from codex_anywhere.hub.transport import handle_node_message


def sent_command(host, command_id):
    return next(
        json.loads(raw)["command"]
        for raw in reversed(host["socket"].messages)
        if json.loads(raw).get("type") == "command"
        and json.loads(raw)["command"]["id"] == command_id
    )


def test_pairing_and_host_response_no_longer_require_or_expose_roots(client, api):
    code = api("POST", "/api/pairings", json={}).get_json()["code"]
    paired = client.post(
        "/api/hosts/pair", json={"code": code, "name": "Unrestricted", "platform": "test"}
    )
    assert paired.status_code == 200
    assert "roots" not in paired.get_json()["host"]
    assert "roots" not in api("GET", "/api/hosts").get_json()["hosts"][0]


def test_project_open_is_a_durable_node_command(api, paired_host):
    host = paired_host()
    response = api(
        "POST",
        "/api/projects",
        json={
            "host_id": host["host"]["id"],
            "cwd": "/arbitrary/new/project",
            "name": "Project",
            "create_directory": True,
        },
    )
    assert response.status_code == 202
    command = response.get_json()["command"]
    wire = sent_command(host, command["id"])
    assert wire["kind"] == "project.open"
    assert wire["payload"] == {
        "cwd": "/arbitrary/new/project",
        "name": "Project",
        "create_directory": True,
    }
    assert command["status"] == "sent"


def test_task_project_grouping_and_project_id_creation(api, paired_host, create_thread):
    host = paired_host()
    first = create_thread(host)
    second = api(
        "POST", "/api/threads", json={"project_id": first["project_id"], "title": "Second"}
    )
    assert second.status_code == 202
    assert second.get_json()["thread"]["cwd"] == first["cwd"]
    projects = api("GET", f"/api/projects?host_id={host['host']['id']}").get_json()["projects"]
    assert len(projects) == 1
    assert projects[0]["thread_count"] == 2
    assert first["history_loaded"] is True and first["imported"] is False


def test_project_cannot_be_used_with_another_host(api, paired_host, create_thread):
    first, second = paired_host("first"), paired_host("second")
    thread = create_thread(first)
    response = api(
        "POST",
        "/api/threads",
        json={
            "host_id": second["host"]["id"],
            "project_id": thread["project_id"],
            "title": "Mismatch",
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/system/another/place", "Z:\\unrelated\\project"])
def test_arbitrary_absolute_directories_are_accepted(api, paired_host, path):
    host = paired_host()
    result = api("POST", "/api/threads", json={"host_id": host["host"]["id"], "cwd": path})
    assert result.status_code == 202
    assert result.get_json()["thread"]["project_id"]


def test_directory_browser_and_catalog_are_sent_to_the_node(api, paired_host):
    host = paired_host()
    host_id = host["host"]["id"]
    directory = api("POST", f"/api/hosts/{host_id}/directories", json={"path": "/etc"})
    assert directory.status_code == 202
    wire = sent_command(host, directory.get_json()["command"]["id"])
    assert wire["kind"] == "directory.list" and wire["payload"] == {"path": "/etc"}
    catalog = api(
        "POST",
        f"/api/hosts/{host_id}/catalog",
        json={
            "limit": 12,
            "cursor": "next-page",
            "archived": True,
            "search": "login",
            "cwd": "/etc",
        },
    )
    assert catalog.status_code == 202
    wire = sent_command(host, catalog.get_json()["command"]["id"])
    assert wire["kind"] == "thread.list"
    assert wire["payload"] == {
        "limit": 12,
        "cursor": "next-page",
        "archived": True,
        "search": "login",
        "cwd": "/etc",
    }


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("directories", {"path": "relative"}),
        ("catalog", {"limit": 201}),
        ("catalog", {"limit": 101}),
        ("catalog", {"archived": "false"}),
        ("catalog", {"cursor": {"invalid": "cursor"}}),
        ("imports", {"native_thread_id": []}),
    ],
)
def test_invalid_project_history_command_input_is_rejected(api, paired_host, endpoint, payload):
    host = paired_host()
    response = api("POST", f"/api/hosts/{host['host']['id']}/{endpoint}", json=payload)
    assert response.status_code == 400


def test_import_placeholder_is_reused_and_not_ready_on_rpc_summary(app, api, paired_host):
    host = paired_host()
    endpoint = f"/api/hosts/{host['host']['id']}/imports"
    payload = {"native_thread_id": "existing-native", "title": "CLI history", "cwd": host["root"]}
    first = api("POST", endpoint, json=payload)
    assert first.status_code == 202
    thread, command = first.get_json()["thread"], first.get_json()["command"]
    assert thread["status"] == "importing" and thread["history_loaded"] is False
    assert thread["imported"] is True and thread["project_id"]
    repeated = api("POST", endpoint, json=payload).get_json()
    assert repeated["thread"]["id"] == thread["id"]
    assert repeated["command"]["id"] == command["id"]
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "command_result", "id": command["id"], "result": {"itemCount": 0}},
    )
    detail = api("GET", f"/api/threads/{thread['id']}").get_json()
    assert detail["thread"]["status"] == "importing"
    assert detail["thread"]["history_loaded"] is False
    assert api("POST", endpoint, json=payload).get_json()["command"]["id"] == command["id"]
    blocked = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Not ready", "client_request_id": "import-blocked"},
    )
    assert blocked.status_code == 409


@pytest.mark.parametrize("status", ["active", "waiting"])
def test_import_does_not_overwrite_a_live_managed_task(
    app, api, paired_host, create_thread, status
):
    host = paired_host()
    thread = create_thread(host)
    with app.app_context():
        db.session.get(Thread, thread["id"]).status = status
        db.session.commit()
    response = api(
        "POST",
        f"/api/hosts/{host['host']['id']}/imports",
        json={"native_thread_id": thread["native_id"]},
    )
    assert response.status_code == 409
    with app.app_context():
        assert Command.query.filter_by(kind="thread.import").count() == 0


def test_thread_pagination_is_stable_and_filterable(app, api, paired_host, create_thread):
    host = paired_host()
    tasks = [create_thread(host, f"Task {index}") for index in range(4)]
    other = create_thread(paired_host("other"), "Other host")
    with app.app_context():
        for task in tasks:
            db.session.get(Thread, task["id"]).updated_at = 10
        db.session.commit()
    base = f"/api/threads?host_id={host['host']['id']}&project_id={tasks[0]['project_id']}&limit=2"
    first = api("GET", base).get_json()
    second = api("GET", base + f"&offset={first['next_offset']}").get_json()
    ids = [row["id"] for row in first["threads"] + second["threads"]]
    assert ids == sorted([task["id"] for task in tasks], reverse=True)
    assert other["id"] not in ids
    assert first["has_more"] is True and second["has_more"] is False
    assert second["next_offset"] is None
    assert api("GET", "/api/threads?limit=201").status_code == 400


def test_history_uses_stable_position_instead_of_creation_time(
    app, api, paired_host, create_thread
):
    thread = create_thread(paired_host())
    with app.app_context():
        for native, position, created in [("later", 20, 1), ("first", 2, 999)]:
            db.session.add(
                Item(
                    thread_id=thread["id"],
                    native_id=native,
                    type="agentMessage",
                    data={"text": native},
                    position=position,
                    created_at=created,
                )
            )
        db.session.commit()
    items = api("GET", f"/api/threads/{thread['id']}").get_json()["items"]
    assert [item["native_id"] for item in items] == ["first", "later"]


def test_upgrade_groups_windows_paths_and_preserves_related_history(app):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "codex_anywhere" / "migrations"),
    )
    with app.app_context():
        db.drop_all()
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            migration_command.upgrade(config, "0002_thread_last_error")
            connection.execute(
                sa.text(
                    "INSERT INTO host (id,name,platform,roots,token_hash) VALUES ('windows','PC','windows','[]','hash')"
                )
            )
            for ident, cwd in [("task-a", "C:\\Work\\Project"), ("task-b", "c:\\work\\project")]:
                connection.execute(
                    sa.text(
                        "INSERT INTO thread (id,host_id,title,cwd,status,created_at,updated_at) VALUES (:id,'windows','Old task',:cwd,'idle',1,1)"
                    ),
                    dict(id=ident, cwd=cwd),
                )
            for ident in ("item-b", "item-a"):
                connection.execute(
                    sa.text(
                        "INSERT INTO item (id,native_id,thread_id,type,data,created_at) VALUES (:id,:id,'task-a','agentMessage','{}',1)"
                    ),
                    dict(id=ident),
                )
            connection.execute(
                sa.text(
                    "INSERT INTO command (id,host_id,thread_id,kind,payload,status,created_at,updated_at) VALUES ('command','windows','task-a','turn.start','{}','completed',1,1)"
                )
            )
            migration_command.upgrade(config, "head")
            assert connection.execute(sa.text("SELECT count(*) FROM project")).scalar_one() == 1
            project_ids = (
                connection.execute(sa.text("SELECT project_id FROM thread")).scalars().all()
            )
            assert project_ids[0] == project_ids[1] and project_ids[0]
            assert dict(connection.execute(sa.text("SELECT id,position FROM item")).all()) == {
                "item-a": 0,
                "item-b": 1,
            }
            assert connection.execute(sa.text("SELECT count(*) FROM command")).scalar_one() == 1
            assert "roots" not in [
                column["name"] for column in sa.inspect(connection).get_columns("host")
            ]
            assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
            migration_command.downgrade(config, "0002_thread_last_error")
            assert connection.execute(sa.text("SELECT count(*) FROM command")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT count(*) FROM item")).scalar_one() == 2
            migration_command.upgrade(config, "head")
            assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
