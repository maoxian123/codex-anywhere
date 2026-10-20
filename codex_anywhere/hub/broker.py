"""Single-process routing with bounded queues and one application writer per node."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field


class FullWriteSocket:
    """Complete and serialize every underlying WebSocket frame write.

    simple-websocket uses socket.send(), including from its ping/pong thread,
    without handling a partial write. This per-connection proxy preserves
    recv/fileno/selector behavior, while sendall and a shared lock prevent
    truncated or interleaved frame bytes. No library globals are modified.
    """

    def __init__(self, socket):
        self._socket = socket
        self._send_lock = threading.Lock()

    def send(self, data, *args, **kwargs):
        with self._send_lock:
            self._socket.sendall(data, *args, **kwargs)
        return len(data)

    def sendall(self, data, *args, **kwargs):
        self.send(data, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._socket, name)


def install_full_writes(ws):
    """Cover application frames and the library's ping/pong/close writer alike."""
    if not isinstance(ws.sock, FullWriteSocket):
        ws.sock = FullWriteSocket(ws.sock)


@dataclass
class Connection:
    socket: object
    ready: bool = False
    writer_thread_id: int = field(default_factory=threading.get_ident)
    outgoing: deque = field(default_factory=deque)
    pending_bytes: int = 0
    last_seen: float = field(default_factory=time.monotonic)


class Broker:
    def __init__(self, max_pending_messages=128, max_pending_bytes=64 * 1024 * 1024):
        self._nodes: dict[str, Connection] = {}
        self._lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.max_pending_messages = max_pending_messages
        self.max_pending_bytes = max_pending_bytes

    def attach(self, host_id, socket, *, ready=False):
        with self._lock:
            if host_id in self._nodes:
                return False
            self._nodes[host_id] = Connection(socket, ready=ready)
            return True

    def mark_ready(self, host_id, socket):
        with self._lock:
            current = self._nodes.get(host_id)
            if not current or current.socket is not socket:
                return False
            current.ready = True
            return True

    def detach(self, host_id, socket):
        with self._lock:
            current = self._nodes.get(host_id)
            if current and current.socket is socket:
                del self._nodes[host_id]

    def is_attached(self, host_id, socket):
        with self._lock:
            current = self._nodes.get(host_id)
            return bool(current and current.socket is socket)

    def touch(self, host_id):
        with self._lock:
            if host_id in self._nodes:
                self._nodes[host_id].last_seen = time.monotonic()

    def is_online(self, host_id):
        with self._lock:
            item = self._nodes.get(host_id)
            return bool(item and item.ready and time.monotonic() - item.last_seen < 60)

    def send(self, host_id, data, *, expected_socket=None):
        """Enqueue without socket I/O; durable commands remain the source of truth."""
        encoded = json.dumps(data, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        with self._lock:
            current = self._nodes.get(host_id)
            if not current or (
                expected_socket is not None and current.socket is not expected_socket
            ):
                return False
            if (
                len(current.outgoing) >= self.max_pending_messages
                or current.pending_bytes + size > self.max_pending_bytes
            ):
                # The owner loop observes detachment and closes its own socket.
                # Unsent commands and unacknowledged events replay from storage.
                del self._nodes[host_id]
                return False
            current.outgoing.append((encoded, size))
            current.pending_bytes += size
            return True

    def pump(self, host_id, socket, limit=16):
        """Drain bounded work on the thread that accepted this node connection."""
        with self._lock:
            current = self._nodes.get(host_id)
            if not current or current.socket is not socket:
                return False
            if current.writer_thread_id != threading.get_ident():
                raise RuntimeError("Only the node connection owner may write outgoing frames")
        for _ in range(limit):
            with self._lock:
                if self._nodes.get(host_id) is not current:
                    return False
                if not current.outgoing:
                    return True
                encoded, size = current.outgoing.popleft()
                current.pending_bytes -= size
            try:
                socket.send(encoded)
            except Exception:
                self.detach(host_id, socket)
                return False
        return True

    def send_command(self, command):
        return self.send(command["host_id"], {"type": "command", "command": command["wire"]})
