from uuid import uuid4

from sqlalchemy import event

from codex_anywhere.hub.models import Host, Project, Thread, db, iso
from codex_anywhere.hub.projects import ensure_project
from codex_anywhere.hub.transport import handle_node_message


def test_thread_pagination_uses_activity_before_slicing(app, api, paired_host):
    host = paired_host()
    with app.app_context():
        rows = []
        for index, (native, history, updated) in enumerate(
            [(10, None, 9000), (20, None, 1000), (15, 20, 2000), (None, None, 15)]
        ):
            thread = Thread(
                host_id=host["host"]["id"],
                native_id=f"native-{index}",
                cwd=host["root"],
                title=f"Task {index}",
                status="idle",
                imported=True,
                native_updated_at=native,
                history_native_updated_at=history,
                created_at=1,
                updated_at=updated,
            )
            db.session.add(thread)
            rows.append(thread)
        db.session.commit()
        ties = sorted([rows[1].id, rows[2].id], reverse=True)
        expected = [*ties, rows[3].id, rows[0].id]

    first = api("GET", "/api/threads?limit=2").get_json()
    second = api("GET", f"/api/threads?limit=2&offset={first['next_offset']}").get_json()
    assert [row["id"] for row in first["threads"] + second["threads"]] == expected
    assert [row["last_activity_at"] for row in first["threads"]] == [iso(20), iso(20)]
    assert second["has_more"] is False


def test_project_activity_includes_threads_outside_the_loaded_page(app, api, paired_host):
    host = paired_host()
    with app.app_context():
        record = db.session.get(Host, host["host"]["id"])
        projects = [ensure_project(record, f"/project-{index}") for index in range(4)]
        for project, created in zip(projects, [10, 10, 850, 50], strict=True):
            project.created_at = created
            project.updated_at = 10000
        for index, (project, native) in enumerate(
            [(projects[0], 300), (projects[0], 900), (projects[1], 700)]
        ):
            db.session.add(
                Thread(
                    host_id=record.id,
                    project_id=project.id,
                    native_id=f"native-{index}",
                    cwd=project.cwd,
                    title="Task",
                    status="idle",
                    native_updated_at=native,
                    updated_at=10000,
                )
            )
        db.session.commit()
        expected = [projects[index].id for index in [0, 2, 1, 3]]

    page = api("GET", "/api/threads?limit=1").get_json()
    assert page["threads"][0]["project_id"] == expected[0]
    before = api("GET", "/api/projects").get_json()["projects"]
    assert [project["id"] for project in before] == expected
    assert [project["last_activity_at"] for project in before] == [
        iso(900), iso(850), iso(700), iso(50)
    ]
    assert before[0]["thread_count"] == 2

    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "method": "anywhere/project_catalog",
            "params": {"projects": [{"cwd": "/project-3"}, {"cwd": "/project-1"}]},
        },
    )
    after = api("GET", "/api/projects").get_json()["projects"]
    assert [project["id"] for project in after] == expected
    assert [project["last_activity_at"] for project in after] == [
        project["last_activity_at"] for project in before
    ]


def test_local_input_and_live_completion_advance_imported_activity(
    app, api, paired_host, create_thread
):
    host = paired_host()
    task = create_thread(host)
    with app.app_context():
        thread = db.session.get(Thread, task["id"])
        thread.imported = True
        thread.native_updated_at = 10
        thread.history_native_updated_at = 10
        thread.updated_at = 10000
        db.session.commit()

    response = api(
        "POST",
        f"/api/threads/{task['id']}/messages",
        json={"text": "Continue", "client_request_id": uuid4().hex},
    )
    assert response.status_code == 202
    command = response.get_json()["command"]
    active = api("GET", f"/api/threads/{task['id']}").get_json()["thread"]
    assert active["last_activity_at"] == command["created_at"]
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "method": "turn/started",
            "params": {"threadId": task["native_id"], "turn": {"id": "live-turn"}},
        },
    )
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": host["runtime_id"],
            "method": "turn/completed",
            "params": {
                "threadId": task["native_id"],
                "turn": {"id": "live-turn", "status": "completed", "items": []},
            },
        },
    )
    completed = api("GET", f"/api/threads/{task['id']}").get_json()["thread"]
    assert completed["last_activity_at"] == iso(completed["history_native_updated_at"])
    assert completed["last_activity_at"] >= command["created_at"]
    assert completed["native_updated_at"] == 10
    projects = api("GET", "/api/projects").get_json()["projects"]
    assert projects[0]["last_activity_at"] == completed["last_activity_at"]
    with app.app_context():
        assert db.session.get(Project, task["project_id"]).as_dict()["last_activity_at"] == (
            completed["last_activity_at"]
        )


def test_loading_old_history_does_not_promote_a_conversation(
    app, api, paired_host, create_thread
):
    host = paired_host()
    old = create_thread(host, "Old conversation")
    newer = create_thread(host, "Newer conversation")
    with app.app_context():
        for task, version in [(old, 10), (newer, 20)]:
            thread = db.session.get(Thread, task["id"])
            thread.native_updated_at = version
            thread.imported = True
            thread.history_loaded = False
            thread.updated_at = 10000
        db.session.commit()

    reading = api("POST", f"/api/threads/{old['id']}/history", json={})
    assert reading.status_code == 202
    command = reading.get_json()["command"]
    for method, extra in [
        (
            "anywhere/import_metadata",
            {"thread": {"id": old["native_id"], "cwd": host["root"], "updatedAt": 10}},
        ),
        ("anywhere/import_complete", {"itemCount": 0}),
    ]:
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "event",
                "event_id": uuid4().hex,
                "runtime_id": host["runtime_id"],
                "method": method,
                "params": {"threadId": old["native_id"], "commandId": command["id"], **extra},
            },
        )
    rows = api("GET", "/api/threads").get_json()["threads"]
    assert [row["id"] for row in rows] == [newer["id"], old["id"]]
    assert rows[1]["history_loaded"] is True
    assert rows[1]["last_activity_at"] == iso(10)
    assert rows[1]["history_synced_at"] > rows[0]["last_activity_at"]


def test_list_queries_aggregate_the_journal_once_without_per_thread_reads(
    app, logged_in, paired_host, create_thread
):
    host = paired_host()
    create_thread(host)
    create_thread(host)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert logged_in.get("/api/threads").status_code == 200
        assert logged_in.get("/api/projects").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 2
    with engine.connect() as connection:
        for statement, parameters in statements:
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, parameters
            ).all()
            details = [row[3].upper() for row in plan]
            assert not any("CORRELATED" in detail for detail in details)
            assert sum("SCAN COMMAND" in detail for detail in details) == 1
