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


def test_manual_release_is_expiring_and_idempotent_while_presence_is_ignored(
    app, api, writer_thread
):
    _, thread = writer_thread
    data = {"client_id": "tab-a", "issued_at": time.time()}
    lease = post(api, thread, "presence", **data)
    assert lease.status_code == 200
    assert lease.get_json() == {"command": None, "ignored": True}
    before = time.time()
    released = post(api, thread, "release", manual=True, **data).get_json()["command"]
    with app.app_context():
        payload = db.session.get(Command, released["id"]).payload
        assert payload["manual"] is True
        assert before + 89 <= payload["expires_at"] <= time.time() + 90
    again = post(api, thread, "release", manual=True, **data).get_json()["command"]
    assert again["id"] == released["id"]
    stale = post(api, thread, "presence", **{**data, "issued_at": data["issued_at"] - 1})
    assert stale.status_code == 200
    assert stale.get_json() == {"command": None, "ignored": True}


def test_logout_does_not_release_or_interrupt_running_turn(app, api, writer_thread):
    _, thread = writer_thread
    for client_id in ("tab-a", "tab-b"):
        post(api, thread, "presence", client_id=client_id, issued_at=time.time())
    with app.app_context():
        row = db.session.get(Thread, thread["id"])
        row.status, row.active_turn_id = "active", "running-turn"
        db.session.commit()
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    with app.app_context():
        assert not Command.query.filter_by(thread_id=thread["id"], kind="thread.release").first()
        assert not Command.query.filter_by(thread_id=thread["id"], kind="thread.presence").first()
        row = db.session.get(Thread, thread["id"])
        assert (row.status, row.active_turn_id) == ("active", "running-turn")
        assert not Command.query.filter_by(thread_id=thread["id"], kind="turn.interrupt").first()


def test_clients_with_same_browser_id_in_different_sessions_are_isolated(app, api, writer_thread):
    _, thread = writer_thread
    data = {"client_id": "same-browser-id", "issued_at": time.time()}
    first = post(api, thread, "release", manual=True, **data).get_json()["command"]
    other = app.test_client()
    csrf = other.get("/api/auth/me").get_json()["csrf_token"]
    logged = other.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-password"},
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    second = other.post(
        f"/api/threads/{thread['id']}/release",
        json={**data, "manual": True},
        headers={"X-CSRF-Token": logged["csrf_token"]},
    ).get_json()["command"]
    with app.app_context():
        first_client = db.session.get(Command, first["id"]).payload["client_id"]
        second_client = db.session.get(Command, second["id"]).payload["client_id"]
    assert first_client != second_client
    api("POST", "/api/auth/logout", json={})
    with app.app_context():
        releases = Command.query.filter_by(thread_id=thread["id"], kind="thread.release").all()
        assert {c.payload["client_id"] for c in releases} == {first_client, second_client}
        assert len(releases) == 2


@pytest.mark.parametrize("issued", [True, -1, "123", None, 10**16])
def test_invalid_manual_request_order_does_not_reach_node(api, writer_thread, issued):
    _, thread = writer_thread
    response = post(api, thread, "release", manual=True, client_id="tab", issued_at=issued)
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
    command = post(api, thread, "takeover", client_id="tab", issued_at=time.time()).get_json()[
        "command"
    ]
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "command_result",
                "id": command["id"],
                "result": {"ownership": {"state": "managed"}},
            },
        )
        row = db.session.get(Thread, thread["id"])
        assert row.history_stale
        assert row.status == "idle"


def test_late_takeover_cannot_undo_a_newer_page_release(app, api, writer_thread):
    _, thread = writer_thread
    now = time.time()
    post(api, thread, "release", manual=True, client_id="tab", issued_at=now)
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
    assert (
        post(
            api, thread, "release", manual=True, client_id="tab", issued_at=time.time()
        ).status_code
        == 409
    )


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize(
    "features", [[], ["threadOwnership"], ["threadOwnership", "completionIdleRelease"]]
)
def test_old_browser_presence_and_automatic_release_are_noops_without_runtime_mutation(
    app, api, writer_thread, features, offline
):
    host, thread = writer_thread
    with app.app_context():
        db.session.get(Host, host["host"]["id"]).capabilities = features
        db.session.commit()
        initial_commands = Command.query.count()
        initial_state = db.session.get(Thread, thread["id"]).as_dict()
    if offline:
        app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    sent = len(host["socket"].messages)
    writers = app.extensions["writer_sessions"]
    assert writers.clients == {}
    for page in ("old-tab-a", "old-tab-b", "mobile-browser"):
        for issued in (time.time(), time.time() + 300):
            presence = post(api, thread, "presence", client_id=page, issued_at=issued)
            assert presence.status_code == 200
            assert presence.get_json() == {"command": None, "ignored": True}
            for extra in ({}, {"manual": False}):
                release = post(api, thread, "release", client_id=page, issued_at=issued, **extra)
                assert release.status_code == 200
                assert release.get_json() == {"command": None, "ignored": True}
    # No browser presence state or ordering markers can delay the node's own
    # 120-second completion timer, regardless of how many old pages are open.
    assert writers.clients == {}
    with app.app_context():
        assert Command.query.count() == initial_commands
        assert db.session.get(Thread, thread["id"]).as_dict() == initial_state
    assert len(host["socket"].messages) == sent


@pytest.mark.parametrize("manual", [None, 0, 1, "true", "false", [], {}])
def test_non_boolean_manual_flag_is_rejected(app, api, writer_thread, manual):
    _, thread = writer_thread
    response = post(api, thread, "release", manual=manual, client_id="tab", issued_at=time.time())
    assert response.status_code == 400
    with app.app_context():
        assert not Command.query.filter_by(kind="thread.release").first()


def test_presence_noop_needs_no_legacy_page_payload(api, writer_thread):
    _, thread = writer_thread
    for data in ({}, {"client_id": None, "issued_at": "old-browser-format"}):
        response = post(api, thread, "presence", **data)
        assert response.status_code == 200
        assert response.get_json() == {"command": None, "ignored": True}


@pytest.mark.parametrize("action", ["presence", "release", "takeover"])
def test_writer_routes_still_require_authentication_and_csrf(app, logged_in, writer_thread, action):
    _, thread = writer_thread
    path = f"/api/threads/{thread['id']}/{action}"
    payload = {"client_id": "tab", "issued_at": time.time(), "manual": True}
    assert app.test_client().post(path, json=payload).status_code == 401
    assert logged_in.post(path, json=payload).status_code == 403


@pytest.mark.parametrize("invalidate_all", [False, True])
def test_session_invalidation_cleans_legacy_pages_without_journaling_release(
    app, api, logged_in, writer_thread, invalidate_all
):
    _, thread = writer_thread
    writers = app.extensions["writer_sessions"]
    with logged_in.session_transaction() as current:
        nonce = current["auth_nonce"]
    writers.clients[nonce] = {
        (thread["id"], "legacy-scoped-id"): {
            "native_thread_id": thread["native_id"],
            "client_id": "legacy-scoped-id",
            "issued_at": time.time(),
            "expires_at": time.time() + 90,
            "seen_at": time.time(),
        }
    }
    if invalidate_all:
        response = api(
            "PATCH",
            "/api/settings/administrator",
            json={"username": "admin", "current_password": "test-password"},
        )
    else:
        response = api("POST", "/api/auth/logout", json={})
    assert response.status_code == 200
    assert nonce not in writers.clients and nonce in writers.revoked
    with app.app_context():
        assert not Command.query.filter_by(thread_id=thread["id"], kind="thread.release").first()
        assert not Command.query.filter_by(thread_id=thread["id"], kind="turn.interrupt").first()


def test_manual_release_unknown_outcome_is_returned_without_new_delivery(app, api, writer_thread):
    host, thread = writer_thread
    data = {"manual": True, "client_id": "tab", "issued_at": time.time()}
    original = post(api, thread, "release", **data).get_json()["command"]
    with app.app_context():
        expires = db.session.get(Command, original["id"]).payload["expires_at"]
        handle_node_message(
            app,
            host["host"]["id"],
            {"type": "command_result", "id": original["id"], "unknown": True, "error": "lost"},
        )
    sent = len(host["socket"].messages)
    retried = post(api, thread, "release", **data).get_json()["command"]
    assert retried["id"] == original["id"] and retried["status"] == "unknown"
    assert len(host["socket"].messages) == sent
    with app.app_context():
        assert Command.query.filter_by(thread_id=thread["id"], kind="thread.release").count() == 1
        assert db.session.get(Command, original["id"]).payload["expires_at"] == expires


def test_legacy_heartbeat_cannot_supersede_explicit_takeover_order(app, api, writer_thread):
    _, thread = writer_thread
    issued = time.time()
    response = post(api, thread, "presence", client_id="tab", issued_at=issued + 1000)
    assert response.status_code == 200
    takeover = post(api, thread, "takeover", client_id="tab", issued_at=issued)
    assert takeover.status_code == 202
    with app.app_context():
        payload = db.session.get(Command, takeover.get_json()["command"]["id"]).payload
        assert issued <= payload["expires_at"] <= time.time() + 90
        assert not Command.query.filter_by(kind="thread.presence").first()
