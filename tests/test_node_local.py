import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codex_anywhere.cli import main
from codex_anywhere.node.local import create_local_app

ORIGIN = "http://127.0.0.1:5780"


class FakeManager:
    def __init__(self):
        self.calls = []
        self.error = None

    def status(self):
        return {"paired": False, "running": False, "status": "unpaired", "token": "private-token"}

    def call(self, method, **kwargs):
        if self.error:
            raise self.error
        self.calls.append((method, kwargs))
        return self.status()

    def pair(self, **kwargs):
        return self.call("pair", **kwargs)

    def start(self, **kwargs):
        return self.call("start", **kwargs)

    def stop(self):
        return self.call("stop")


@pytest.fixture
def local():
    manager = FakeManager()
    app = create_local_app(manager)
    app.config["TESTING"] = True
    client = app.test_client()
    state = client.get("/api/status", base_url=ORIGIN).get_json()
    return app, client, manager, state["csrf_token"]


def mutate(local, path, data, **kwargs):
    _, client, _, csrf = local
    headers = kwargs.pop("headers", {})
    headers.setdefault("X-CSRF-Token", csrf)
    return client.post(path, json=data, base_url=ORIGIN, headers=headers, **kwargs)


def test_public_status_and_command_results_never_expose_node_token(local):
    _, client, manager, _ = local
    response = client.get("/api/status", base_url=ORIGIN)
    assert response.status_code == 200
    assert "private-token" not in response.get_data(as_text=True)
    paired = mutate(local, "/api/pair", {"server": "https://hub.example", "name": "本机", "code": "ABC"})
    assert paired.status_code == 202
    assert "private-token" not in paired.get_data(as_text=True)
    assert manager.calls == [("pair", {
        "server": "https://hub.example", "name": "本机", "code": "ABC", "codex_bin": "codex",
        "allow_insecure": False, "replace_pairing": False,
    })]


@pytest.mark.parametrize("headers", [
    {"X-CSRF-Token": "wrong"},
    {"Origin": "https://attacker.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "attacker.example:5780"},
])
def test_external_pages_cannot_pair_or_stop_local_node(local, headers):
    response = mutate(local, "/api/stop", {}, headers=headers)
    assert response.status_code == 403
    assert not local[2].calls


def test_non_loopback_request_and_dns_rebinding_are_rejected(local):
    _, client, _, _ = local
    assert client.get("/api/status", base_url=ORIGIN, environ_base={"REMOTE_ADDR": "10.0.0.10"}).status_code == 403
    assert client.get("/api/status", base_url="http://rebind.example:5780").status_code == 403
    assert client.get("/api/status", base_url="http://127.0.0.1:5781").status_code == 403
    assert client.get("/api/status", base_url="http://localhost:5780").status_code == 200


@pytest.mark.parametrize("path,data", [
    ("/api/pair", {"server": "https://hub.example", "name": "PC", "code": ""}),
    ("/api/pair", {"server": "https://hub.example", "name": "PC", "code": "ABC", "allow_insecure": "false"}),
    ("/api/pair", {"server": "https://hub.example", "name": "PC", "code": "ABC", "replace_pairing": 1}),
    ("/api/start", {"codex_bin": "codex\n--option"}),
    ("/api/stop", {"pid": 123}),
])
def test_invalid_controls_never_reach_manager(local, path, data):
    assert mutate(local, path, data).status_code == 400
    assert not local[2].calls


def test_mutations_require_json_and_session_csrf(local):
    app, client, manager, csrf = local
    stranger = app.test_client()
    assert stranger.post("/api/stop", json={}, base_url=ORIGIN).status_code == 403
    response = client.post("/api/stop", data="{}", base_url=ORIGIN, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 415
    assert not manager.calls


def test_browser_controls_preserve_custom_codex_and_explicit_http_opt_in(local):
    response = mutate(local, "/api/start", {"codex_bin": "C:/程序/Codex/codex.exe", "allow_insecure": True})
    assert response.status_code == 202
    assert local[2].calls == [("start", {"codex_bin": "C:/程序/Codex/codex.exe", "allow_insecure": True})]
    assert mutate(local, "/api/stop", {}).status_code == 202


def test_local_node_has_no_cors_and_is_not_embeddable(local):
    app, _, _, _ = local
    client = app.test_client()
    response = client.get("/api/status", base_url=ORIGIN)
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers
    assert "HttpOnly" in response.headers.get("Set-Cookie", "")
    assert "SameSite=Strict" in response.headers.get("Set-Cookie", "")


def test_busy_and_filesystem_failures_are_visible_without_private_exception_details(local):
    local[2].error = RuntimeError("节点尚未停止，请等待")
    assert mutate(local, "/api/stop", {}).status_code == 409
    local[2].error = OSError("secret-pairing-code private-token")
    response = mutate(local, "/api/stop", {})
    assert response.status_code == 503
    assert "private-token" not in response.get_data(as_text=True)


def test_cli_node_web_forwards_configuration_without_starting_a_node(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("codex_anywhere.node.local.serve_local_node", lambda *args: calls.append(args))
    result = CliRunner().invoke(main, ["node", "web", "--state-dir", str(tmp_path), "--port", "5790", "--codex-bin", "custom-codex", "--no-open"])
    assert result.exit_code == 0, result.output
    assert calls == [(Path(tmp_path), 5790, "custom-codex", False)]
    assert CliRunner().invoke(main, ["node", "web", "--port", "65536"]).exit_code != 0


def test_oversized_configuration_request_is_rejected(local):
    response = mutate(local, "/api/pair", {"server": "x" * 40_000})
    assert response.status_code == 413
    assert json.loads(response.data)["error"]
