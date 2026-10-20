import io
import time
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from codex_anywhere.hub.models import Asset, Command, db


def upload_image(api):
    stream = io.BytesIO()
    Image.new("RGB", (3, 3), color="green").save(stream, "PNG")
    stream.seek(0)
    result = api(
        "POST",
        "/api/assets",
        data={"file": (stream, "image.png", "image/png")},
        content_type="multipart/form-data",
    )
    assert result.status_code == 200
    return result.get_json()["asset"]


@pytest.mark.parametrize("host_id", [{"bad": "id"}, {"id": "not-a-host"}, ["bad"], 123])
def test_invalid_host_identifier_returns_json_400(api, host_id):
    response = api("POST", "/api/threads", json={"host_id": host_id, "cwd": "/project"})
    assert response.status_code == 400
    assert isinstance(response.get_json()["error"], str)


@pytest.mark.parametrize("asset_id", [{"bad": "id"}, ["bad"], 123])
def test_invalid_asset_identifier_does_not_create_a_command(
    app, api, paired_host, create_thread, asset_id
):
    thread = create_thread(paired_host())
    response = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Inspect", "asset_ids": [asset_id], "client_request_id": uuid4().hex},
    )
    assert response.status_code == 400
    with app.app_context():
        assert Command.query.filter_by(kind="turn.start").count() == 0


@pytest.mark.parametrize(
    "reference",
    [
        {"kind": "task", "thread_id": {"bad": "id"}},
        {"kind": "task", "thread_id": "missing", "item_id": ["bad"]},
    ],
)
def test_invalid_reference_identifiers_return_json_400(api, reference):
    response = api("POST", "/api/references/resolve", json={"references": [reference]})
    assert response.status_code == 400
    assert response.is_json


def test_logout_revokes_a_copied_cookie(app, logged_in, api):
    copied = logged_in.get_cookie("session").value
    thief = app.test_client()
    thief.set_cookie("session", copied)
    assert thief.get("/api/hosts").status_code == 200
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    assert thief.get("/api/hosts").status_code == 401
    assert thief.get("/api/auth/me").get_json()["authenticated"] is False


def test_expired_session_is_rejected_even_with_a_valid_signed_cookie(app, logged_in):
    with logged_in.session_transaction() as current:
        nonce = current["auth_nonce"]
    app.extensions["auth_sessions"][nonce] = time.time() - 1
    assert logged_in.get("/api/hosts").status_code == 401
    assert logged_in.get("/api/auth/me").get_json()["authenticated"] is False


def test_logout_does_not_revoke_another_logged_in_browser(app, logged_in, api):
    second = app.test_client()
    csrf = second.get("/api/auth/me").get_json()["csrf_token"]
    login = second.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "admin", "password": "test-password"},
    )
    assert login.status_code == 200
    assert api("POST", "/api/auth/logout", json={}).status_code == 200
    assert logged_in.get("/api/hosts").status_code == 401
    assert second.get("/api/hosts").status_code == 200


def test_invalid_unicode_is_a_json_error_instead_of_an_encoding_exception(api):
    response = api("POST", "/api/threads", json={"host_id": "\ud800", "cwd": "/project"})
    assert response.status_code == 400
    assert response.is_json


def test_bad_node_name_does_not_consume_a_valid_pairing_code(client, api, tmp_path):
    code = api("POST", "/api/pairings", json={}).get_json()["code"]
    payload = {"code": code, "name": ["invalid"], "roots": [str(tmp_path)], "platform": "test"}
    assert client.post("/api/hosts/pair", json=payload).status_code == 400
    payload["name"] = "Valid workstation"
    assert client.post("/api/hosts/pair", json=payload).status_code == 200


def test_missing_asset_file_is_a_controlled_error(app, api, paired_host, create_thread):
    thread = create_thread(paired_host())
    asset = upload_image(api)
    with app.app_context():
        Path(db.session.get(Asset, asset["id"]).path).unlink()
    assert api("GET", asset["url"]).status_code == 404
    sent = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={
            "text": "Use missing file",
            "asset_ids": [asset["id"]],
            "client_request_id": uuid4().hex,
        },
    )
    assert sent.status_code == 400
    assert sent.is_json
    with app.app_context():
        assert Command.query.filter_by(kind="turn.start").count() == 0


def test_asset_record_cannot_serve_a_path_outside_asset_root(
    app, api, tmp_path, paired_host, create_thread
):
    thread = create_thread(paired_host())
    asset = upload_image(api)
    outside = tmp_path / "private-file.txt"
    outside.write_text("private file", encoding="utf-8")
    with app.app_context():
        stored = db.session.get(Asset, asset["id"])
        stored.path = str(outside)
        db.session.commit()
    response = api("GET", asset["url"])
    assert response.status_code == 404
    assert b"private file" not in response.data
    attempted = api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": "Use file", "asset_ids": [asset["id"]], "client_request_id": uuid4().hex},
    )
    assert attempted.status_code == 400
