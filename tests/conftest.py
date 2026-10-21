from uuid import uuid4

import pytest

from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import db
from codex_anywhere.hub.transport import handle_node_message


class FakeNodeSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, message):
        self.messages.append(message)

    def close(self):
        self.closed = True


@pytest.fixture
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-session-secret",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "test-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "ASSET_DIR": str(tmp_path / "assets"),
        }
    )
    with application.app_context():
        db.create_all()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in(client):
    token = client.get("/api/auth/me").get_json()["csrf_token"]
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-password"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def pump_nodes(app):
    def pump():
        for host_id, socket in app.extensions.get("_fake_nodes", {}).items():
            app.extensions["broker"].pump(host_id, socket, limit=128)

    return pump


@pytest.fixture
def api(logged_in, pump_nodes):
    def request(method, path, **kwargs):
        flush = kwargs.pop("flush", True)
        token = logged_in.get("/api/auth/me").get_json()["csrf_token"]
        headers = kwargs.pop("headers", {})
        headers["X-CSRF-Token"] = token
        response = logged_in.open(path, method=method, headers=headers, **kwargs)
        if flush:
            # A test client has no node owner loop; explicitly run its queue pump.
            pump_nodes()
        return response

    return request


@pytest.fixture
def paired_host(app, client, api, tmp_path, pump_nodes):
    def pair(name="workstation"):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        pairing = api("POST", "/api/pairings", json={}).get_json()
        result = client.post(
            "/api/hosts/pair",
            json={
                "code": pairing["code"],
                "name": name,
                "platform": "test",
                "version": "test",
            },
        )
        assert result.status_code in (200, 201)
        data = result.get_json()
        host_id = data["host"]["id"]
        socket = FakeNodeSocket()
        app.extensions["broker"].attach(host_id, socket, ready=True)
        app.extensions.setdefault("_fake_nodes", {})[host_id] = socket
        runtime_id = str(uuid4())
        with app.app_context():
            handle_node_message(
                app,
                host_id,
                {
                    "type": "hello",
                    "runtime_id": runtime_id,
                    "version": "test",
                    "platform": "test",
                },
            )
        pump_nodes()
        return {
            **data,
            "root": str(root.resolve()),
            "runtime_id": runtime_id,
            "socket": socket,
        }

    return pair


@pytest.fixture
def create_thread(app, api, pump_nodes):
    def create(host, title="Test task"):
        response = api(
            "POST",
            "/api/threads",
            json={"host_id": host["host"]["id"], "cwd": host["root"], "title": title},
        )
        assert response.status_code == 202, response.get_json()
        data = response.get_json()
        native_id = "native-" + uuid4().hex
        with app.app_context():
            handle_node_message(
                app,
                host["host"]["id"],
                {
                    "type": "command_result",
                    "id": data["command"]["id"],
                    "result": {"thread": {"id": native_id, "cwd": host["root"]}},
                },
            )
        pump_nodes()
        return {**data["thread"], "native_id": native_id}

    return create


@pytest.fixture
def completed_item(app, pump_nodes):
    def add(host, thread, text="Context from another task"):
        native_item = "item-" + uuid4().hex
        event = {
            "type": "event",
            "event_id": str(uuid4()),
            "runtime_id": host["runtime_id"],
            "seq": 1,
            "method": "item/completed",
            "params": {
                "threadId": thread["native_id"],
                "turnId": "turn-" + uuid4().hex,
                "item": {"id": native_item, "type": "agentMessage", "text": text},
            },
        }
        with app.app_context():
            handle_node_message(app, host["host"]["id"], event)
        pump_nodes()
        return native_item

    return add
