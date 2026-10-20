import json
import threading
import time
from types import SimpleNamespace

import pytest

from codex_anywhere.hub.broker import Broker, FullWriteSocket, install_full_writes
from codex_anywhere.hub.models import Command, Thread, db
from codex_anywhere.hub.transport import handle_node_message


class PartialSocket:
    def __init__(self):
        self.output = bytearray()
        self.sendall_calls = 0

    def send(self, data):
        part = data[:3]
        self.output.extend(part)
        return len(part)

    def sendall(self, data):
        self.sendall_calls += 1
        remaining = memoryview(data)
        while remaining:
            count = self.send(remaining)
            remaining = remaining[count:]

    def fileno(self):
        return 123


class RecordingSocket:
    def __init__(self):
        self.messages = []
        self.threads = []

    def send(self, message):
        self.messages.append(json.loads(message))
        self.threads.append(threading.get_ident())


def test_full_write_proxy_handles_partial_socket_and_install_is_local():
    raw = PartialSocket()
    ws = SimpleNamespace(sock=raw)
    install_full_writes(ws)
    installed = ws.sock
    install_full_writes(ws)
    assert ws.sock is installed
    payload = b"A WebSocket frame longer than one send"
    assert ws.sock.send(payload) == len(payload)
    assert bytes(raw.output) == payload
    assert raw.sendall_calls == 1
    assert ws.sock.fileno() == 123
    assert isinstance(ws.sock, FullWriteSocket)
    assert not isinstance(raw, FullWriteSocket)


def test_full_write_proxy_serializes_application_and_library_control_writes():
    class SlowSocket(PartialSocket):
        def send(self, data):
            result = super().send(data)
            time.sleep(0.0001)
            return result

    raw = SlowSocket()
    wrapped = FullWriteSocket(raw)
    barrier = threading.Barrier(3)

    def writer(data):
        barrier.wait()
        wrapped.send(data)

    messages = [b"A" * 300, b"B" * 300]
    threads = [threading.Thread(target=writer, args=(data,)) for data in messages]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert bytes(raw.output) in (messages[0] + messages[1], messages[1] + messages[0])


def test_other_thread_only_enqueues_and_owner_pump_delivers_in_order():
    broker, socket = Broker(), RecordingSocket()
    assert broker.attach("host", socket)

    def producer():
        for index in range(4):
            assert broker.send("host", {"index": index})

    thread = threading.Thread(target=producer)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert socket.messages == []
    assert broker.pump("host", socket)
    assert socket.messages == [{"index": index} for index in range(4)]
    assert socket.threads == [threading.get_ident()] * 4


def test_non_owner_cannot_pump_frames():
    broker, socket = Broker(), RecordingSocket()
    broker.attach("host", socket)
    broker.send("host", {"hello": "world"})
    errors = []

    def wrong_writer():
        try:
            broker.pump("host", socket)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wrong_writer)
    thread.start()
    thread.join(timeout=2)
    assert errors and socket.messages == []
    assert broker.pump("host", socket)
    assert socket.messages == [{"hello": "world"}]


@pytest.mark.parametrize("options", [{"max_pending_messages": 1}, {"max_pending_bytes": 18}])
def test_queue_limits_detach_without_writing_on_the_producer(options):
    broker, socket = Broker(**options), RecordingSocket()
    broker.attach("host", socket)
    assert broker.send("host", {"message": "x"})
    assert not broker.send("host", {"message": "y"})
    assert not broker.is_online("host")
    assert socket.messages == []
    assert not broker.pump("host", socket)


def test_stale_owner_cannot_detach_or_pump_replacement():
    broker = Broker()
    old, replacement = RecordingSocket(), RecordingSocket()
    broker.attach("host", old)
    broker.detach("host", old)
    assert broker.attach("host", replacement)
    broker.detach("host", old)
    assert broker.is_attached("host", replacement)
    assert not broker.pump("host", old)
    assert broker.send("host", {"replacement": True})
    assert broker.pump("host", replacement)
    assert replacement.messages == [{"replacement": True}]
    assert old.messages == []


def test_queue_overflow_preserves_and_replays_durable_commands(app, api, paired_host):
    host = paired_host()
    host_id = host["host"]["id"]
    broker = app.extensions["broker"]
    broker.max_pending_messages = 1
    commands = []
    for index in range(2):
        result = api(
            "POST",
            "/api/threads",
            flush=False,
            json={"host_id": host_id, "cwd": host["root"], "title": f"Queued {index}"},
        )
        assert result.status_code == 202
        commands.append(result.get_json()["command"]["id"])
    assert not broker.is_online(host_id)
    with app.app_context():
        assert Command.query.count() == 2
        assert {row.status for row in Command.query.all()} == {"sent", "queued"}
    replacement = RecordingSocket()
    broker.max_pending_messages = 128
    assert broker.attach(host_id, replacement)
    handle_node_message(
        app,
        host_id,
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "test"},
        flush_outgoing=lambda: broker.pump(host_id, replacement),
        outgoing_socket=replacement,
    )
    replayed = [
        frame["command"]["id"] for frame in replacement.messages if frame["type"] == "command"
    ]
    assert sorted(replayed) == sorted(commands)
    with app.app_context():
        assert Command.query.count() == 2


def test_write_failure_detaches_but_keeps_the_accepted_command(app, api, paired_host):
    host = paired_host()
    host_id, socket = host["host"]["id"], host["socket"]
    broker = app.extensions["broker"]
    result = api(
        "POST",
        "/api/threads",
        flush=False,
        json={"host_id": host_id, "cwd": host["root"], "title": "Connection fails"},
    )
    assert result.status_code == 202

    def fail(_):
        raise OSError("peer disconnected")

    socket.send = fail
    assert not broker.pump(host_id, socket)
    assert not broker.is_online(host_id)
    with app.app_context():
        saved = db.session.get(Command, result.get_json()["command"]["id"])
        assert saved.status == "sent"
        assert saved.payload["title"] == "Connection fails"


def test_hello_can_replay_more_than_queue_capacity_without_disconnect(app, paired_host):
    host = paired_host()
    host_id = host["host"]["id"]
    broker = app.extensions["broker"]
    broker.detach(host_id, host["socket"])
    ids = []
    with app.app_context():
        for index in range(140):
            thread = Thread(host_id=host_id, title=f"Backlog {index}", cwd=host["root"])
            db.session.add(thread)
            db.session.flush()
            command = Command(
                host_id=host_id,
                thread_id=thread.id,
                kind="thread.start",
                runtime_id=host["runtime_id"],
                status="queued",
                payload={"cwd": host["root"], "title": thread.title},
            )
            db.session.add(command)
            db.session.flush()
            ids.append(command.id)
        db.session.commit()
    replacement = RecordingSocket()
    broker.max_pending_messages = 2
    assert broker.attach(host_id, replacement)
    handle_node_message(
        app,
        host_id,
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "test"},
        flush_outgoing=lambda: broker.pump(host_id, replacement),
        outgoing_socket=replacement,
    )
    assert broker.is_attached(host_id, replacement)
    replayed = [
        frame["command"]["id"] for frame in replacement.messages if frame["type"] == "command"
    ]
    assert len(replayed) == 140 and set(replayed) == set(ids)


def test_connection_is_not_online_until_owner_finishes_hello_replay():
    broker, socket = Broker(), RecordingSocket()
    assert broker.attach("host", socket)
    assert not broker.is_online("host")
    assert broker.mark_ready("host", socket)
    assert broker.is_online("host")
    assert not broker.mark_ready("host", RecordingSocket())


def test_slow_hello_replay_does_not_hold_http_command_lock(app, api, paired_host):
    host = paired_host()
    host_id, broker = host["host"]["id"], app.extensions["broker"]
    queued = api(
        "POST",
        "/api/threads",
        flush=False,
        json={"host_id": host_id, "cwd": host["root"], "title": "Replay outside lock"},
    )
    assert queued.status_code == 202
    broker.detach(host_id, host["socket"])

    class SlowSocket(RecordingSocket):
        def send(self, message):
            # Real HTTP producers must be able to acquire this lock while a
            # socket writer is slow, but cannot overtake replay readiness.
            assert not broker.is_online(host_id)
            acquired = threading.Event()

            def http_producer():
                with broker.command_lock:
                    acquired.set()

            thread = threading.Thread(target=http_producer, daemon=True)
            thread.start()
            assert acquired.wait(timeout=1), "socket write held the HTTP command lock"
            thread.join(timeout=1)
            time.sleep(0.02)
            super().send(message)

    replacement = SlowSocket()
    assert broker.attach(host_id, replacement)
    handle_node_message(
        app,
        host_id,
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "test"},
        flush_outgoing=lambda: broker.pump(host_id, replacement),
        outgoing_socket=replacement,
    )
    assert broker.is_online(host_id)
    assert [frame["type"] for frame in replacement.messages] == ["welcome", "command"]


def test_replay_is_bound_to_original_owner_if_connection_is_replaced(app, api, paired_host):
    host = paired_host()
    host_id, broker = host["host"]["id"], app.extensions["broker"]
    accepted = api(
        "POST",
        "/api/threads",
        flush=False,
        json={"host_id": host_id, "cwd": host["root"], "title": "Owner replacement"},
    )
    assert accepted.status_code == 202
    broker.detach(host_id, host["socket"])
    old, replacement = RecordingSocket(), RecordingSocket()
    assert broker.attach(host_id, old)

    def flush_then_replace():
        assert broker.pump(host_id, old)
        broker.detach(host_id, old)
        assert broker.attach(host_id, replacement, ready=True)
        return True

    handle_node_message(
        app,
        host_id,
        {"type": "hello", "runtime_id": host["runtime_id"], "version": "test"},
        flush_outgoing=flush_then_replace,
        outgoing_socket=old,
    )
    assert [frame["type"] for frame in old.messages] == ["welcome"]
    assert broker.pump(host_id, replacement)
    assert replacement.messages == []
    with app.app_context():
        assert db.session.get(Command, accepted.get_json()["command"]["id"]).status == "sent"
