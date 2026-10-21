"""Administrator settings, durable credentials and browser-session revocation."""

import json
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from flask import request
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

import codex_anywhere.hub as hub
from codex_anywhere.cli import upgrade_database
from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import HubSetting, db


def login(client, username="admin", password="test-password"):
    csrf = client.get("/api/auth/me").get_json()["csrf_token"]
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": csrf},
    )


def test_settings_exposes_only_bootstrap_username_before_first_save(app, api):
    response = api("GET", "/api/settings")
    assert response.get_json() == {"administrator": {"username": "admin"}}
    assert response.headers["Cache-Control"] == "no-store"
    with app.app_context():
        assert HubSetting.query.count() == 0


def test_username_only_change_preserves_password_and_trims_username(app, api):
    response = api(
        "PATCH",
        "/api/settings/administrator",
        json={"username": "  管理员  ", "current_password": "test-password"},
    )
    assert response.status_code == 200
    assert response.get_json()["settings"] == {"administrator": {"username": "管理员"}}
    assert response.get_json()["session"]["username"] == "管理员"
    with app.app_context():
        record = db.session.get(HubSetting, "administrator")
        assert record.updated_at > 0
        assert set(record.value) == {"username", "password_hash"}
        assert check_password_hash(record.value["password_hash"], "test-password")
        assert "test-password" not in json.dumps(record.value)
    assert login(app.test_client(), "管理员").status_code == 200
    assert login(app.test_client()).status_code == 401


def test_password_only_change_keeps_username_and_preserves_password_whitespace(app, api):
    new_password = "  new password  "
    response = api(
        "PATCH",
        "/api/settings/administrator",
        json={
            "username": "admin",
            "current_password": "test-password",
            "new_password": new_password,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["settings"] == {"administrator": {"username": "admin"}}
    assert new_password not in response.get_data(as_text=True)
    with app.app_context():
        value = db.session.get(HubSetting, "administrator").value
        assert check_password_hash(value["password_hash"], new_password)
        assert not check_password_hash(value["password_hash"], "test-password")
        assert new_password not in json.dumps(value)
    assert login(app.test_client(), password=new_password).status_code == 200
    assert login(app.test_client(), password=new_password.strip()).status_code == 401
    assert login(app.test_client()).status_code == 401


def test_save_rotates_current_session_and_revokes_every_previous_cookie(app, logged_in, api):
    copied = app.test_client()
    copied.set_cookie("session", logged_in.get_cookie("session").value)
    second = app.test_client()
    assert login(second).status_code == 200
    old_csrf = logged_in.get("/api/auth/me").get_json()["csrf_token"]
    response = api(
        "PATCH",
        "/api/settings/administrator",
        json={"username": "new-admin", "current_password": "test-password"},
    )
    state = response.get_json()["session"]
    assert state["authenticated"] is True
    assert state["username"] == "new-admin"
    assert state["csrf_token"] != old_csrf
    assert logged_in.get("/api/auth/me").get_json() == state
    assert logged_in.get("/api/settings").status_code == 200
    assert copied.get("/api/settings").status_code == 401
    assert second.get("/api/settings").status_code == 401
    assert len(app.extensions["auth_sessions"]) == 1
    assert (
        logged_in.patch(
            "/api/settings/administrator",
            json={"username": "third-admin", "current_password": "test-password"},
            headers={"X-CSRF-Token": old_csrf},
        ).status_code
        == 403
    )


def test_wrong_current_password_does_not_change_credentials_or_session(app, logged_in, api):
    previous_cookie = logged_in.get_cookie("session").value
    previous_state = logged_in.get("/api/auth/me").get_json()
    response = api(
        "PATCH",
        "/api/settings/administrator",
        json={"username": "new-admin", "current_password": "incorrect"},
    )
    assert response.status_code == 403
    assert logged_in.get("/api/auth/me").get_json() == previous_state
    copied = app.test_client()
    copied.set_cookie("session", previous_cookie)
    assert copied.get("/api/settings").status_code == 200
    with app.app_context():
        assert HubSetting.query.count() == 0


def test_settings_requires_login_and_csrf(app, client, logged_in):
    anonymous = app.test_client()
    data = {"username": "new-admin", "current_password": "test-password"}
    assert anonymous.get("/api/settings").status_code == 401
    assert anonymous.patch("/api/settings/administrator", json=data).status_code == 401
    assert logged_in.patch("/api/settings/administrator", json=data).status_code == 403
    assert (
        logged_in.patch(
            "/api/settings/administrator", json=data, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    with app.app_context():
        assert HubSetting.query.count() == 0


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"username": "admin"},
        {"username": None, "current_password": "test-password"},
        {"username": ["admin"], "current_password": "test-password"},
        {"username": "  ", "current_password": "test-password"},
        {"username": "a" * 121, "current_password": "test-password"},
        {"username": "ad\nmin", "current_password": "test-password"},
        {"username": "\tadmin", "current_password": "test-password"},
        {"username": "ad\x00min", "current_password": "test-password"},
        {"username": "ad\u200bmin", "current_password": "test-password"},
        {"username": "ad\ud800min", "current_password": "test-password"},
        {"username": "admin", "current_password": None},
        {"username": "admin", "current_password": 123},
        {"username": "admin", "current_password": ""},
        {"username": "admin", "current_password": "a" * 1025},
        {"username": "admin", "current_password": "test-password", "new_password": None},
        {"username": "admin", "current_password": "test-password", "new_password": 123},
        {"username": "admin", "current_password": "test-password", "new_password": "short"},
        {"username": "admin", "current_password": "test-password", "new_password": ""},
        {"username": "admin", "current_password": "test-password", "new_password": "a" * 1025},
        {"username": "admin", "current_password": "test-password", "unexpected": True},
        [],
        None,
    ],
)
def test_invalid_settings_never_mutate_credentials_or_revoke_login(app, logged_in, api, data):
    before = logged_in.get("/api/auth/me").get_json()
    response = api("PATCH", "/api/settings/administrator", json=data)
    assert response.status_code == 400
    assert logged_in.get("/api/auth/me").get_json() == before
    with app.app_context():
        assert HubSetting.query.count() == 0


def test_current_password_attempts_share_login_rate_limit(app, api, logged_in):
    for _ in range(9):
        assert (
            api(
                "PATCH",
                "/api/settings/administrator",
                json={"username": "admin", "current_password": "wrong-password"},
            ).status_code
            == 403
        )
    assert (
        api(
            "PATCH",
            "/api/settings/administrator",
            json={"username": "admin", "current_password": "test-password"},
        ).status_code
        == 429
    )
    assert login(app.test_client()).status_code == 429
    assert logged_in.get("/api/settings").get_json() == {"administrator": {"username": "admin"}}


def test_failed_commit_keeps_old_credentials_and_existing_sessions(
    app, logged_in, api, monkeypatch
):
    before = logged_in.get("/api/auth/me").get_json()

    def failed_commit():
        raise IntegrityError("save failed", {}, RuntimeError("database failure"))

    monkeypatch.setattr(db.session, "commit", failed_commit)
    response = api(
        "PATCH",
        "/api/settings/administrator",
        json={"username": "changed", "current_password": "test-password"},
    )
    assert response.status_code == 409
    assert logged_in.get("/api/auth/me").get_json() == before
    assert logged_in.get("/api/settings").get_json() == {"administrator": {"username": "admin"}}
    with app.app_context():
        assert HubSetting.query.count() == 0


def test_old_credentials_cannot_login_while_settings_save_is_in_progress(
    app, logged_in, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    original_check = hub.check_password_hash
    results = {}

    def blocked_check(password_hash, password):
        if threading.current_thread().name == "settings-save":
            entered.set()
            assert release.wait(timeout=5)
        return original_check(password_hash, password)

    monkeypatch.setattr(hub, "check_password_hash", blocked_check)
    csrf = logged_in.get("/api/auth/me").get_json()["csrf_token"]

    def save():
        results["save"] = logged_in.patch(
            "/api/settings/administrator",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "new-admin",
                "current_password": "test-password",
                "new_password": "new-password",
            },
        ).status_code

    def old_login():
        results["login"] = login(app.test_client()).status_code

    saving = threading.Thread(target=save, name="settings-save")
    signing_in = threading.Thread(target=old_login)
    saving.start()
    try:
        assert entered.wait(timeout=5)
        signing_in.start()
    finally:
        release.set()
        saving.join(timeout=5)
        if signing_in.ident:
            signing_in.join(timeout=5)
    assert not saving.is_alive()
    assert not signing_in.is_alive()
    assert results == {"save": 200, "login": 401}
    assert login(app.test_client(), "new-admin", "new-password").status_code == 200


def test_inflight_settings_request_rechecks_nonce_after_another_save(app):
    passed_authentication = threading.Event()
    release = threading.Event()
    results = {}

    @app.before_request
    def hold_authenticated_request():
        if request.headers.get("X-Test-Hold"):
            passed_authentication.set()
            assert release.wait(timeout=5)

    current = app.test_client()
    assert login(current).status_code == 200
    csrf = current.get("/api/auth/me").get_json()["csrf_token"]
    stale = app.test_client()
    stale.set_cookie("session", current.get_cookie("session").value)

    def stale_save():
        results["stale"] = stale.patch(
            "/api/settings/administrator",
            headers={"X-CSRF-Token": csrf, "X-Test-Hold": "yes"},
            json={"username": "stale-update", "current_password": "test-password"},
        ).status_code

    pending = threading.Thread(target=stale_save)
    pending.start()
    try:
        assert passed_authentication.wait(timeout=5)
        response = current.patch(
            "/api/settings/administrator",
            headers={"X-CSRF-Token": csrf},
            json={"username": "saved-update", "current_password": "test-password"},
        )
        assert response.status_code == 200
    finally:
        release.set()
        pending.join(timeout=5)
    assert not pending.is_alive()
    assert results == {"stale": 401}
    assert current.get("/api/settings").get_json() == {
        "administrator": {"username": "saved-update"}
    }


def test_migrated_settings_survive_restart_and_override_environment(tmp_path):
    database = tmp_path / "settings.sqlite3"
    config = {
        "SECRET_KEY": "settings-migration-secret",
        "ADMIN_USERNAME": "bootstrap-admin",
        "ADMIN_PASSWORD": "bootstrap-password",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(database),
        "ASSET_DIR": str(tmp_path / "assets"),
    }
    app = create_app(config)
    restarted = None
    try:
        alembic = Config()
        alembic.set_main_option(
            "script_location", str(Path(hub.__file__).parent.parent / "migrations")
        )
        with app.app_context(), db.engine.begin() as connection:
            alembic.attributes["connection"] = connection
            command.upgrade(alembic, "0005_runtime_settings")
        upgrade_database(app)
        client = app.test_client()
        assert login(client, "bootstrap-admin", "bootstrap-password").status_code == 200
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        response = client.patch(
            "/api/settings/administrator",
            headers={"X-CSRF-Token": token},
            json={
                "username": "saved-admin",
                "current_password": "bootstrap-password",
                "new_password": "saved-password",
            },
        )
        assert response.status_code == 200
        with app.app_context():
            serialized = json.dumps(db.session.get(HubSetting, "administrator").value)
            assert "bootstrap-password" not in serialized
            assert "saved-password" not in serialized
            db.session.remove()
            db.engine.dispose()
        restarted = create_app(
            {**config, "ADMIN_USERNAME": "different-env-admin", "ADMIN_PASSWORD": "different-env"}
        )
        upgrade_database(restarted)
        second_client = restarted.test_client()
        second_client.set_cookie("session", client.get_cookie("session").value)
        assert second_client.get("/api/settings").status_code == 401
        assert login(second_client, "saved-admin", "saved-password").status_code == 200
        assert second_client.get("/api/settings").get_json() == {
            "administrator": {"username": "saved-admin"}
        }
        assert (
            login(restarted.test_client(), "bootstrap-admin", "bootstrap-password").status_code
            == 401
        )
        assert (
            login(restarted.test_client(), "different-env-admin", "different-env").status_code
            == 401
        )
    finally:
        for application in (app, restarted):
            if application is not None:
                with application.app_context():
                    db.session.remove()
                    db.engine.dispose()


@pytest.mark.parametrize("path", ["/settings", "/settings/"])
def test_settings_deep_link_serves_application_shell(tmp_path, monkeypatch, path):
    dist = tmp_path / "web"
    dist.mkdir()
    (dist / "index.html").write_text("<html>settings application</html>", encoding="utf-8")
    monkeypatch.setenv("WEB_DIST", str(dist))
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "deep-link-test",
            "ADMIN_PASSWORD": "test-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "ASSET_DIR": str(tmp_path / "assets"),
        }
    )
    try:
        response = app.test_client().get(path)
        assert response.status_code == 200
        assert "settings application" in response.get_data(as_text=True)
        assert response.headers["Cache-Control"] == "no-cache"
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
