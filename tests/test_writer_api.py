import time

import pytest

from codex_anywhere.hub.models import Command, Host, Thread, db
from codex_anywhere.hub.transport import handle_node_message


@pytest.fixture
def writer_thread(app, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    with app.app_context():
        db.session.get(Host, host["host"]["id"]).capabilities = ["threadOwnership"]
        db.session.commit()
    return host, thread


def post(api, thread, action, **data):
    return api("POST", f"/api/threads/{thread['id']}/{action}", json=data)


def test_opening_checks_writer_without_claiming_or_changing_thread(app, api, writer_thread):
    _, thread = writer_thread
    first = post(api, thread, "ownership").get_json()["command"]
    second = post(api, thread, "ownership").get_json()["command"]
    assert first["id"] == second["id"]
    assert first["kind"] == "thread.ownership"
    with app.app_context():
        assert db.session.get(Command, first["id"]).payload == {
            "native_thread_id": thread["native_id"]
        }
        assert db.session.get(Thread, thread["id"]).status == "idle"


def test_release_wins_over_late_heartbeat_and_requests_are_idempotent(app, api, writer_thread):
    _, thread = writer_thread
    data = {"client_id": "tab-a", "issued_at": time.time()}
    lease = post(api, thread, "presence", **data).get_json()["command"]
    with app.app_context():
        assert db.session.get(Command, lease["id"]).payload["expires_at"] > time.time()
    released = post(api, thread, "release", **data).get_json()["command"]
    with app.app_context():
        assert db.session.get(Command, released["id"]).payload["expires_at"] == 0
    again = post(api, thread, "release", **data).get_json()["command"]
    assert again["id"] == released["id"]
    stale = post(api, thread, "presence", **{**data, "issued_at": data["issued_at"] - 1})
    assert stale.status_code == 200
    assert stale.get_json() == {"command": None, "ignored": True}


def test_logout_releases_each_page_but_does_not_interrupt_running_turn(app, api, writer_thread):
    _, thread = writer_thread
    for client_id in ("tab-a", "tab-b"):
        post(api, thread, "presence", client_id=client_id, issued_at=time.time())
    with app.app_context():
        row = db.session.get(Thread, thread["id"])
        row.status, row.active_turn_id = "active", "running-turn"
        db.session.commit()
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    with app.app_context():
        releases = Command.query.filter_by(thread_id=thread["id"], kind="thread.release").all()
        assert len(releases) == 2
        assert len({command.payload["client_id"] for command in releases}) == 2
        assert all(command.payload["expires_at"] == 0 for command in releases)
        row = db.session.get(Thread, thread["id"])
        assert (row.status, row.active_turn_id) == ("active", "running-turn")
        assert not Command.query.filter_by(thread_id=thread["id"], kind="turn.interrupt").first()


def test_clients_with_same_browser_id_in_different_sessions_are_isolated(
    app, api, writer_thread
):
    _, thread = writer_thread
    data = {"client_id": "same-browser-id", "issued_at": time.time()}
    first = post(api, thread, "presence", **data).get_json()["command"]
    other = app.test_client()
    csrf = other.get("/api/auth/me").get_json()["csrf_token"]
    logged = other.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-password"},
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    second = other.post(
        f"/api/threads/{thread['id']}/presence",
        json=data,
        headers={"X-CSRF-Token": logged["csrf_token"]},
    ).get_json()["command"]
    with app.app_context():
        first_client = db.session.get(Command, first["id"]).payload["client_id"]
        second_client = db.session.get(Command, second["id"]).payload["client_id"]
    assert first_client != second_client
    api("POST", "/api/auth/logout", json={})
    with app.app_context():
        releases = Command.query.filter_by(thread_id=thread["id"], kind="thread.release").all()
        assert [c.payload["client_id"] for c in releases] == [first_client]


@pytest.mark.parametrize("issued", [True, -1, "123", None, 10**16])
def test_invalid_presence_does_not_reach_node(api, writer_thread, issued):
    _, thread = writer_thread
    response = post(api, thread, "presence", client_id="tab", issued_at=issued)
    assert response.status_code == 400


def test_external_takeover_needs_explicit_confirmation_and_unknown_is_not_retried(
    app, api, writer_thread
):
    host, thread = writer_thread
    data = {"client_id": "tab", "issued_at": time.time(), "owner_token": "opaque-owner"}
    assert post(api, thread, "takeover", **data).status_code == 409
    response = post(api, thread, "takeover", confirmed=True, **data)
    command = response.get_json()["command"]
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {"type": "command_result", "id": command["id"], "unknown": True, "error": "lost"},
        )
    retried = post(api, thread, "takeover", confirmed=True, **data).get_json()["command"]
    assert retried["id"] == command["id"]
    assert retried["status"] == "unknown"
    with app.app_context():
        assert Command.query.filter_by(thread_id=thread["id"], kind="thread.takeover").count() == 1


def test_takeover_refreshes_native_history_after_previous_owner(app, api, writer_thread):
    host, thread = writer_thread
    command = post(
        api, thread, "takeover", client_id="tab", issued_at=time.time()
    ).get_json()["command"]
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {"type": "command_result", "id": command["id"], "result": {"ownership": {"state": "managed"}}},
        )
        row = db.session.get(Thread, thread["id"])
        assert row.history_stale
        assert row.status == "idle"


def test_late_takeover_cannot_undo_a_newer_page_release(app, api, writer_thread):
    _, thread = writer_thread
    now = time.time()
    post(api, thread, "release", client_id="tab", issued_at=now)
    response = post(api, thread, "takeover", client_id="tab", issued_at=now - 1)
    assert response.status_code == 409
    with app.app_context():
        assert not Command.query.filter_by(thread_id=thread["id"], kind="thread.takeover").first()


def test_old_nodes_and_offline_nodes_cannot_claim_support(app, api, writer_thread):
    host, thread = writer_thread
    with app.app_context():
        db.session.get(Host, host["host"]["id"]).capabilities = []
        db.session.commit()
    assert post(api, thread, "ownership").status_code == 409
    app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    assert post(api, thread, "release", client_id="tab", issued_at=time.time()).status_code == 409
