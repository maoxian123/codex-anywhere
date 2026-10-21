"""Loopback-only configuration UI for the independently running local node."""

from __future__ import annotations

import ipaddress
import logging
import secrets
import signal
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from .config import node_lock

log = logging.getLogger(__name__)
DEFAULT_LOCAL_PORT = 5780


def create_local_app(manager, port: int = DEFAULT_LOCAL_PORT) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=secrets.token_urlsafe(48),
        MAX_CONTENT_LENGTH=32 * 1024,
        LOCAL_PORT=port,
        SESSION_COOKIE_NAME=f"codex_anywhere_node_{port}",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
    )
    assets = Path(__file__).parent / "local_ui"

    def public_status(value):
        allowed = {
            "paired", "running", "connected", "status", "server", "name", "host_id",
            "codex_bin", "allow_insecure", "last_error", "platform",
        }
        return {key: item for key, item in value.items() if key in allowed}

    def allowed_origins():
        port = app.config["LOCAL_PORT"]
        suffix = "" if port == 80 else f":{port}"
        return {f"http://127.0.0.1{suffix}", f"http://localhost{suffix}"}

    @app.before_request
    def protect_local_control():
        try:
            loopback = ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback or f"http://{request.host}" not in allowed_origins():
            return jsonify(error="本地节点管理页仅允许通过本机回环地址访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin not in allowed_origins():
            return jsonify(error="不允许其他网页操作本地节点"), 403
        if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="不允许跨站访问本地节点接口"), 403
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
                return jsonify(error="页面校验已失效，请刷新后重试"), 403
            if not request.is_json:
                return jsonify(error="请求必须使用 JSON"), 415

    @app.after_request
    def local_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(RuntimeError)
    def busy(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(OSError)
    def unavailable(exc):
        # Filesystem/network exceptions can contain private paths or credentials.
        log.warning("Local node operation failed (%s)", type(exc).__name__)
        return jsonify(error="本地节点操作失败，请检查运行环境后重试"), 503

    def body(allowed):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - set(allowed):
            raise ValueError("请求字段无效")
        return data

    def text(data, key, *, default=None, limit=4096):
        value = data.get(key, default)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{key} 必须是有效的非空文本")
        return value.strip()

    def boolean(data, key, default=False):
        value = data.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"{key} 必须是布尔值")
        return value

    @app.get("/")
    def index():
        return send_from_directory(assets, "index.html")

    @app.get("/app.js")
    def javascript():
        return send_from_directory(assets, "app.js")

    @app.get("/style.css")
    def stylesheet():
        return send_from_directory(assets, "style.css")

    @app.get("/api/health")
    def health():
        return jsonify(service="codex-anywhere-node", version="0.1.0")

    @app.get("/api/status")
    def status():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return jsonify(status=public_status(manager.status()), csrf_token=session["csrf_token"])

    @app.post("/api/pair")
    def pair():
        data = body({"server", "name", "code", "codex_bin", "allow_insecure", "replace_pairing"})
        result = manager.pair(
            server=text(data, "server", limit=2048),
            name=text(data, "name", limit=120),
            code=text(data, "code", limit=200),
            codex_bin=text(data, "codex_bin", default="codex"),
            allow_insecure=boolean(data, "allow_insecure"),
            replace_pairing=boolean(data, "replace_pairing"),
        )
        return jsonify(status=public_status(result)), 202

    @app.post("/api/start")
    def start():
        data = body({"codex_bin", "allow_insecure"})
        options = {}
        if "codex_bin" in data:
            options["codex_bin"] = text(data, "codex_bin")
        if "allow_insecure" in data:
            options["allow_insecure"] = boolean(data, "allow_insecure")
        return jsonify(status=public_status(manager.start(**options))), 202

    @app.post("/api/stop")
    def stop():
        body(set())
        return jsonify(status=public_status(manager.stop())), 202

    return app


def serve_local_node(
    state_dir: Path,
    port: int = DEFAULT_LOCAL_PORT,
    codex_bin: str = "codex",
    open_browser: bool = True,
) -> None:
    from .manager import NodeManager

    state_dir = Path(state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    with node_lock(state_dir / ".launcher"):
        manager = NodeManager(state_dir, codex_bin=codex_bin)
        server = None
        browser_timer = None
        old_term = None
        try:
            app = create_local_app(manager, port)
            # Binding before auto-starting prevents a port conflict from starting
            # a node that has no corresponding local control server.
            server = make_server("127.0.0.1", port, app, threaded=True)
            actual_port = server.server_port
            app.config.update(
                LOCAL_PORT=actual_port,
                SESSION_COOKIE_NAME=f"codex_anywhere_node_{actual_port}",
            )
            url = f"http://127.0.0.1:{actual_port}"
            print(f"Local node settings: {url}", flush=True)
            print("Keep this process running. Ctrl+C stops the local service and node.", flush=True)
            if manager.status()["paired"]:
                try:
                    manager.start()
                except (ValueError, RuntimeError, OSError):
                    log.warning("Node could not auto-start; inspect the local settings page")
            if open_browser:
                def show_browser():
                    try:
                        webbrowser.open(url)
                    except Exception:
                        log.warning("Open the local settings URL in your browser")

                browser_timer = threading.Timer(0.25, show_browser)
                browser_timer.daemon = True
                browser_timer.start()
            if threading.current_thread() is threading.main_thread():
                old_term = signal.getsignal(signal.SIGTERM)

                def terminate(_signum, _frame):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, terminate)
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if old_term is not None:
                signal.signal(signal.SIGTERM, old_term)
            if browser_timer:
                browser_timer.cancel()
            if server:
                server.server_close()
            manager.close()
