"""Single-process socket routing. Durable events are read from SQL, not this object."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Connection:
    socket: object
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seen: float = field(default_factory=time.monotonic)


class Broker:
    def __init__(self):
        self._nodes: dict[str, Connection] = {}
        self._lock = threading.RLock()
        self.command_lock = threading.RLock()

    def attach(self, host_id, socket):
        with self._lock:
            if host_id in self._nodes:
                return False
            self._nodes[host_id] = Connection(socket)
            return True

    def detach(self, host_id, socket):
        with self._lock:
            current = self._nodes.get(host_id)
            if current and current.socket is socket:
                del self._nodes[host_id]

    def touch(self, host_id):
        with self._lock:
            if host_id in self._nodes:
                self._nodes[host_id].last_seen = time.monotonic()

    def is_online(self, host_id):
        with self._lock:
            item = self._nodes.get(host_id)
            return bool(item and time.monotonic() - item.last_seen < 60)

    def send(self, host_id, data):
        with self._lock:
            current = self._nodes.get(host_id)
        if not current:
            return False
        try:
            with current.lock:
                current.socket.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            self.detach(host_id, current.socket)
            return False

    def send_command(self, command):
        return self.send(command["host_id"], {"type": "command", "command": command["wire"]})
