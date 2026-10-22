"""Journal explicit writer operations, scoped to authenticated browser sessions."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time

from flask import abort, jsonify, session

from .models import Command

REQUEST_SECONDS = 90


class WriterSessions:
    def __init__(self, app, issue_command):
        self.app = app
        self.issue_command = issue_command
        self.lock = threading.RLock()
        self.clients: dict[str, dict[tuple[str, str], dict]] = {}
        self.revoked: dict[str, float] = {}

    def client_payload(self, thread, data, *, release=False):
        client_id = data.get("client_id")
        issued_at = data.get("issued_at")
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id):
            raise ValueError("请提供有效的页面 client_id")
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(issued_at)
            or not 0 <= issued_at <= 10**15
        ):
            raise ValueError("issued_at 必须是有效的请求顺序时间")
        nonce = session.get("auth_nonce", "")
        # Another login/session must not reuse this page's writer operation ID.
        scoped = hashlib.sha256(f"{nonce}:{client_id}".encode()).hexdigest()
        now = time.time()
        payload = {
            "native_thread_id": thread.native_id,
            "client_id": scoped,
            "issued_at": issued_at,
            "expires_at": now + REQUEST_SECONDS,
        }
        with self.lock:
            self.revoked = {key: until for key, until in self.revoked.items() if until > now}
            if nonce in self.revoked:
                abort(401, "登录会话已退出")
            pages = self.clients.setdefault(nonce, {})
            # Keep short-lived order markers so stale takeover requests cannot
            # undo a newer explicit release. Browser presence never enters here.
            for key in [key for key, value in pages.items() if value["seen_at"] < now - 600]:
                del pages[key]
            key = (thread.id, scoped)
            previous = pages.get(key)
            if previous and (
                issued_at < previous["issued_at"]
                or (issued_at == previous["issued_at"] and not release)
            ):
                return {**payload, "_ignored": True}
            if len(pages) >= 1000 and key not in pages:
                abort(429, "打开的会话页面过多")
            pages[key] = {**payload, "seen_at": now}
        return payload

    def release_session(self, nonce):
        """Revoke browser operations without changing the native writer lifetime."""
        if not nonce:
            return
        now = time.time()
        with self.lock:
            self.clients.pop(nonce, None)
            self.revoked[nonce] = now + 600

    def release_all(self):
        with self.lock:
            nonces = list(self.clients)
        for nonce in nonces:
            self.release_session(nonce)

    def forget(self, thread_ids):
        """Discard deleted conversations without issuing commands to their node."""
        removed = set(thread_ids)
        with self.lock:
            for nonce, pages in list(self.clients.items()):
                for key in [key for key in pages if key[0] in removed]:
                    del pages[key]
                if not pages:
                    del self.clients[nonce]

    def register_routes(self, json_body, get_thread, online_host):
        app = self.app

        @app.post("/api/threads/<thread_id>/ownership")
        def writer_ownership(thread_id):
            json_body()
            with app.extensions["broker"].command_lock:
                thread, host = ready(thread_id)
                pending = Command.query.filter(
                    Command.thread_id == thread.id,
                    Command.kind == "thread.ownership",
                    Command.runtime_id == host.runtime_id,
                    Command.status.in_(["queued", "sent", "running"]),
                ).first()
                if pending:
                    return jsonify(command=pending.as_dict()), 202
                return jsonify(
                    command=self.issue_command(
                        host, thread, "thread.ownership", {"native_thread_id": thread.native_id}
                    )
                ), 202

        def ready(thread_id):
            thread = get_thread(thread_id)
            host = online_host(thread.host_id)
            if "threadOwnership" not in (host.capabilities or []):
                abort(409, "此节点尚不支持会话托管管理，请升级节点")
            if not thread.native_id:
                abort(409, "会话尚未创建完成")
            return thread, host

        def change(thread_id, kind, data=None):
            data = json_body() if data is None else data
            with app.extensions["broker"].command_lock:
                thread, host = ready(thread_id)
                if kind == "thread.takeover":
                    confirmed = data.get("confirmed", False)
                    token = data.get("owner_token")
                    if type(confirmed) is not bool:
                        raise ValueError("confirmed 必须是布尔值")
                    if token is not None and (
                        not isinstance(token, str) or not 1 <= len(token) <= 200
                    ):
                        raise ValueError("占用进程确认已失效，请重新检查")
                    if token and not confirmed:
                        abort(409, "请先确认结束占用进程")
                payload = self.client_payload(thread, data, release=kind == "thread.release")
                ignored = payload.pop("_ignored", False)
                key = f"{thread.id}:{kind}:{payload['client_id']}:{float(data['issued_at']):.6f}"
                existing = Command.query.filter_by(idempotency_key=key).first()
                if existing:
                    if kind == "thread.takeover" and (
                        existing.payload.get("owner_token") != token
                        or existing.payload.get("confirmed", False) != confirmed
                    ):
                        abort(409, "同一个接管请求对应了不同的进程确认")
                    return jsonify(command=existing.as_dict()), 202
                if ignored:
                    if kind == "thread.takeover":
                        abort(409, "接管请求已被较新的页面操作替代，请重新检查占用状态")
                    return jsonify(command=None, ignored=True)
                if kind == "thread.takeover":
                    if thread.status in {"active", "waiting", "starting", "importing"}:
                        abort(409, "会话正在执行或同步，请等待完成")
                    payload.update(confirmed=confirmed)
                    if token:
                        payload["owner_token"] = token
                elif kind == "thread.release":
                    payload["manual"] = True
                return jsonify(command=self.issue_command(host, thread, kind, payload, key)), 202

        @app.post("/api/threads/<thread_id>/presence")
        def writer_presence(thread_id):
            json_body()
            get_thread(thread_id)
            # Older pages may keep heartbeating, but only node-observed turn
            # completion and subsequent messages govern automatic release.
            return jsonify(command=None, ignored=True)

        @app.post("/api/threads/<thread_id>/release")
        def writer_release(thread_id):
            data = json_body()
            manual = data.get("manual", False)
            if type(manual) is not bool:
                raise ValueError("manual 必须是布尔值")
            if not manual:
                get_thread(thread_id)
                return jsonify(command=None, ignored=True)
            return change(thread_id, "thread.release", data)

        @app.post("/api/threads/<thread_id>/takeover")
        def writer_takeover(thread_id):
            return change(thread_id, "thread.takeover")
