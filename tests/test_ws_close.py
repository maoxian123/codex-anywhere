"""Closing a browser connection must finish TCP shutdown, not only send a frame."""

import json
import socket
import threading
from types import SimpleNamespace

import pytest
from test_e2e import live_system as live_system
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from codex_anywhere.hub.broker import install_full_writes


def test_close_sends_eof_without_waiting_for_library_reader_to_finish():
    server, peer = socket.socketpair()
    peer.settimeout(0.5)
    ws = SimpleNamespace(sock=server, connected=True, event=threading.Event())

    def original_close(reason=None, message=None):
        ws.sock.send(f"close:{reason}:{message}".encode())
        ws.connected = False

    ws.close = original_close
    install_full_writes(ws)
    installed = ws.close
    install_full_writes(ws)
    assert ws.close is installed
    try:
        ws.close(reason=1008, message="Session expired")
        # The dependency reader can restore this stale value after close().
        # Transport EOF must remain independent of that cross-thread flag.
        ws.connected = True
        assert peer.recv(128) == b"close:1008:Session expired"
        assert peer.recv(1) == b""
        assert ws.event.is_set()
    finally:
        server.close()
        peer.close()


def test_repeated_invalid_subscriptions_complete_the_close_handshake(live_system):
    system = live_system
    cookie = "; ".join(f"{name}={value}" for name, value in system["client"].cookies.items())
    url = system["url"].replace("http://", "ws://") + "/ws/client"
    for index in range(24):
        with connect(
            url, origin=system["url"], additional_headers={"Cookie": cookie}, proxy=None
        ) as ws:
            assert json.loads(ws.recv(timeout=2))["type"] == "ready"
            frame = (
                {"type": "subscribe", "after": True}
                if index % 2
                else {
                    "type": "subscribe",
                    "after": 0,
                    "thread_id": {"invalid": "id"},
                }
            )
            ws.send(json.dumps(frame))
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv(timeout=2)
            assert closed.value.rcvd.code == 1000
