"""Saved-project desktop commands and bounded registration projections."""

import json

import pytest

from codex_anywhere.hub.models import Command, Project, db
from codex_anywhere.hub.projects import ensure_project
from codex_anywhere.hub.transport import handle_node_message


def announce(app, host, capabilities=("desktopProjects",)):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "hello",
            "runtime_id": host["runtime_id"],
            "capabilities": list(capabilities),
        },
    )
    return host


def saved_project(app, host, *, cwd="/canonical/project", name="保存的名称"):
    from codex_anywhere.hub.models import Host

    with app.app_context():
        project = ensure_project(db.session.get(Host, host["host"]["id"]), cwd, name)
        db.session.commit()
        return {"id": project.id, "cwd": project.cwd, "name": project.name}


def endpoint(project):
    return f"/api/projects/{project['id']}/desktop"


def commands(host, kind="project.desktop"):
    return [
        frame["command"]
        for raw in host["socket"].messages
        if (frame := json.loads(raw)).get("type") == "command" and frame["command"]["kind"] == kind
    ]


def complete(app, host, command, result):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "command_result",
            "id": command["id"],
            "result": result,
        },
    )


def test_desktop_registration_is_durable_and_uses_saved_project_and_owning_host(
    app, api, paired_host, monkeypatch
):
    host = announce(app, paired_host("owner"))
    other = announce(app, paired_host("other"))
    project = saved_project(app, host)
    original_send = app.extensions["broker"].send_command
    observed = []

    def send_command(message):
        wire = message["wire"]
        with app.app_context():
            durable = db.session.get(Command, wire["id"])
            assert durable is not None and durable.status == "queued"
            assert durable.payload == {"cwd": project["cwd"], "name": project["name"]}
            assert durable.host_id == host["host"]["id"]
            assert durable.runtime_id == host["runtime_id"]
            assert durable.thread_id is None
        observed.append(wire["id"])
        return original_send(message)

    monkeypatch.setattr(app.extensions["broker"], "send_command", send_command)
    response = api("POST", endpoint(project), json={})
    assert response.status_code == 202
    command = response.get_json()["command"]
    assert observed == [command["id"]]
    assert commands(host) == [
        {
            "id": command["id"],
            "kind": "project.desktop",
            "thread_id": None,
            "payload": {"cwd": project["cwd"], "name": project["name"]},
        }
    ]
    assert commands(other) == []


def test_desktop_registration_requires_login_and_csrf(app, api, logged_in, paired_host):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    assert app.test_client().post(endpoint(project), json={}).status_code == 401
    assert logged_in.post(endpoint(project), json={}).status_code == 403
    with app.app_context():
        assert Command.query.filter_by(kind="project.desktop").count() == 0


@pytest.mark.parametrize(
    "data",
    [
        {"host_id": "another-host"},
        {"cwd": "/arbitrary"},
        {"name": "override"},
        {"pid": 123},
        {"project_id": "another-project"},
    ],
)
def test_desktop_registration_rejects_arbitrary_command_parameters(app, api, paired_host, data):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    assert api("POST", endpoint(project), json=data).status_code == 400
    assert commands(host) == []


def test_desktop_registration_missing_project_is_not_found(api):
    assert api("POST", "/api/projects/missing/desktop", json={}).status_code == 404


@pytest.mark.parametrize("capabilities", [[], ["projectInventory"], ["runtimeSettings"]])
def test_desktop_registration_requires_independent_capability(app, api, paired_host, capabilities):
    host = announce(app, paired_host(), capabilities)
    project = saved_project(app, host)
    assert api("POST", endpoint(project), json={}).status_code == 409
    assert commands(host) == []


def test_desktop_registration_does_not_use_another_online_host(app, api, paired_host):
    owner = announce(app, paired_host("owner"))
    other = announce(app, paired_host("online"))
    project = saved_project(app, owner)
    app.extensions["broker"].detach(owner["host"]["id"], owner["socket"])
    assert api("POST", endpoint(project), json={}).status_code == 409
    assert commands(other) == []


@pytest.mark.parametrize("status", ["queued", "sent", "running"])
def test_desktop_registration_reuses_identical_pending_current_runtime_command(
    app, api, paired_host, status
):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    with app.app_context():
        command = Command(
            host_id=host["host"]["id"],
            runtime_id=host["runtime_id"],
            kind="project.desktop",
            status=status,
            payload={"cwd": project["cwd"], "name": project["name"]},
        )
        db.session.add(command)
        db.session.commit()
        identifier = command.id
    response = api("POST", endpoint(project), json={})
    assert response.status_code == 202
    assert response.get_json()["command"]["id"] == identifier
    assert commands(host) == []
    with app.app_context():
        assert Command.query.filter_by(kind="project.desktop").count() == 1


def test_desktop_registration_repeated_clicks_share_one_delivery(app, api, paired_host):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    first = api("POST", endpoint(project), json={}).get_json()["command"]
    second = api("POST", endpoint(project), json={}).get_json()["command"]
    assert first["id"] == second["id"]
    assert len(commands(host)) == 1


@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_desktop_registration_never_automatically_retries_uncertain_or_failed_commands(
    app, api, paired_host, status
):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    with app.app_context():
        old = Command(
            host_id=host["host"]["id"],
            runtime_id=host["runtime_id"],
            kind="project.desktop",
            status=status,
            payload={"cwd": project["cwd"], "name": project["name"]},
        )
        db.session.add(old)
        db.session.commit()
        identifier = old.id
    announce(app, host)
    api("GET", "/api/projects")
    assert commands(host) == []
    explicit = api("POST", endpoint(project), json={}).get_json()["command"]
    assert explicit["id"] != identifier
    assert [wire["id"] for wire in commands(host)] == [explicit["id"]]
    with app.app_context():
        assert db.session.get(Command, identifier).status == status


def test_desktop_registration_does_not_reuse_old_runtime_or_different_payload(
    app, api, paired_host
):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    identifiers = []
    with app.app_context():
        for runtime_id, name in [("old-runtime", project["name"]), (host["runtime_id"], "旧名称")]:
            command = Command(
                host_id=host["host"]["id"],
                runtime_id=runtime_id,
                kind="project.desktop",
                status="sent",
                payload={"cwd": project["cwd"], "name": name},
            )
            db.session.add(command)
            db.session.flush()
            identifiers.append(command.id)
        db.session.commit()
    fresh = api("POST", endpoint(project), json={}).get_json()["command"]
    assert fresh["id"] not in identifiers
    assert [wire["id"] for wire in commands(host)] == [fresh["id"]]


@pytest.mark.parametrize(
    "status", ["registered", "already_registered", "pending", "unavailable", "error"]
)
def test_project_open_preserves_desktop_registration_alongside_canonical_project(
    app, api, paired_host, status
):
    host = paired_host()
    command = api(
        "POST", "/api/projects", json={"host_id": host["host"]["id"], "cwd": "/requested/project"}
    ).get_json()["command"]
    registration = {"status": status, "reason": "desktop_state", "refresh_required": True}
    complete(
        app,
        host,
        command,
        {"cwd": "/canonical/project", "name": "项目", "desktop_registration": registration},
    )
    result = api("GET", f"/api/commands/{command['id']}").get_json()["command"]
    assert result["status"] == "completed"
    assert result["result"]["desktop_registration"] == registration
    assert result["result"]["project"]["cwd"] == "/canonical/project"
    with app.app_context():
        assert Project.query.count() == 1
        assert not hasattr(Project.query.one(), "desktop_registration")


@pytest.mark.parametrize("wrapped", [False, True])
def test_project_open_legacy_results_stay_compatible(app, api, paired_host, wrapped):
    host = paired_host()
    command = api(
        "POST", "/api/projects", json={"host_id": host["host"]["id"], "cwd": "/project"}
    ).get_json()["command"]
    directory = {"cwd": "/project", "name": "Legacy"}
    complete(app, host, command, {"project": directory} if wrapped else directory)
    result = api("GET", f"/api/commands/{command['id']}").get_json()["command"]["result"]
    assert set(result) == {"project"}
    assert result["project"]["name"] == "Legacy"


def test_desktop_result_sanitizes_registration_and_drops_unrelated_native_fields(
    app, api, paired_host
):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    command = api("POST", endpoint(project), json={}).get_json()["command"]
    complete(
        app,
        host,
        command,
        {
            "cwd": project["cwd"],
            "native_state": "private",
            "desktop_registration": {
                "status": "unavailable",
                "reason": "native_unsupported",
                "refresh_required": False,
                "private_state": {"token": "private"},
            },
        },
    )
    result = api("GET", f"/api/commands/{command['id']}").get_json()["command"]["result"]
    assert result == {
        "cwd": project["cwd"],
        "desktop_registration": {
            "status": "unavailable",
            "reason": "native_unsupported",
            "refresh_required": False,
        },
    }


@pytest.mark.parametrize(
    "registration", [None, [], "invalid", {}, {"status": []}, {"status": "future"}]
)
def test_malformed_desktop_registration_does_not_break_project_projection(
    app, api, paired_host, registration
):
    host = paired_host()
    command = api(
        "POST", "/api/projects", json={"host_id": host["host"]["id"], "cwd": "/project"}
    ).get_json()["command"]
    complete(
        app,
        host,
        command,
        {"project": {"cwd": "/canonical", "name": "Project"}, "desktop_registration": registration},
    )
    result = api("GET", f"/api/commands/{command['id']}").get_json()["command"]
    assert result["status"] == "completed"
    assert set(result["result"]) == {"project"}


def test_registration_invalid_optional_fields_are_omitted(app, api, paired_host):
    host = announce(app, paired_host())
    project = saved_project(app, host)
    command = api("POST", endpoint(project), json={}).get_json()["command"]
    complete(
        app,
        host,
        command,
        {
            "cwd": project["cwd"],
            "desktop_registration": {
                "status": "registered",
                "reason": {"private": "details"},
                "refresh_required": "true",
            },
        },
    )
    result = api("GET", f"/api/commands/{command['id']}").get_json()["command"]["result"]
    assert result["desktop_registration"] == {"status": "registered"}
