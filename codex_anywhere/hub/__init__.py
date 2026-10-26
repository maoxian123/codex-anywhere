"""Flask application factory and authenticated control-plane endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import ntpath
import os
import posixpath
import secrets
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, current_app, jsonify, request, send_file, send_from_directory, session
from flask_sock import Sock
from PIL import Image, UnidentifiedImageError
from sqlalchemy import event as sa_event
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import with_expression
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .broker import Broker, install_full_writes
from .execution import execution_settings, require_runtime_settings
from .history import history_command_applied
from .hosts import register_host_routes
from .message_queue import (
    cancel_queued,
    dispatch_queue,
    enqueue_prepared,
    queue_state,
    resume_queue,
    should_enqueue,
    update_queued,
)
from .models import (
    Asset,
    Command,
    Event,
    Host,
    HubSetting,
    Interaction,
    Item,
    Pairing,
    Project,
    Thread,
    db,
    iso,
    latest_thread_inputs,
    new_id,
    thread_activity_at,
)
from .ownership import WriterSessions
from .preferences import thread_preferences
from .presentation import browser_event, browser_item
from .projects import ensure_project
from .references import quote_references, resolve_references
from .settings import administrator_changes, administrator_credentials, public_settings
from .transport import emit, handle_node_message
from .user_input import pending_interactions, validate_user_input


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def is_absolute_directory(cwd):
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return False
    # Validate in the host's path syntax, not the hub operating system's syntax.
    windows = bool(ntpath.splitdrive(cwd)[0])
    pathmod = ntpath if windows else posixpath
    return pathmod.isabs(cwd)


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} 必须是整数")
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}–{maximum} 之间")
    return result


def _online_host(host_id):
    from flask import abort

    host = db.session.get(Host, _identifier(host_id, "host_id"))
    if not host:
        abort(404, "节点不存在")
    if not current_app.extensions["broker"].is_online(host.id):
        abort(409, "节点当前离线")
    return host


def node_host():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return Host.query.filter_by(token_hash=token_hash(auth[7:])).first()


def _json():
    try:
        data = request.get_json(silent=True)
    except RecursionError as exc:
        raise ValueError("JSON 嵌套层级过深") from exc
    if not isinstance(data, dict):
        raise ValueError("请求必须是 JSON 对象")
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("文本包含无效的 Unicode 字符") from exc
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return data


def _identifier(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 250 or "\x00" in value:
        raise ValueError(f"{name} 必须是有效的字符串 ID")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} 包含无效字符") from exc
    return value


def _command_locked(handler):
    @wraps(handler)
    def locked(*args, **kwargs):
        # Finish reading/validating JSON before taking the node control lock.
        _json()
        with current_app.extensions["broker"].command_lock:
            db.session.remove()
            return handler(*args, **kwargs)

    return locked


def _reference_shapes(references):
    if not isinstance(references, list) or len(references) > 8:
        raise ValueError("最多引用 8 个任务或网页")
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("引用格式错误")
        if reference.get("kind") == "task":
            _identifier(reference.get("thread_id"), "thread_id")
            if reference.get("item_id") is not None:
                _identifier(reference["item_id"], "item_id")
    return references


def _asset_file(asset):
    root = Path(current_app.config["ASSET_DIR"]).resolve()
    try:
        path = Path(asset.path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("附件文件已丢失或不可访问，请重新上传")
    except OSError as exc:
        raise ValueError("附件文件已丢失或不可访问，请重新上传") from exc
    return path


def _get_thread(thread_id):
    thread = db.session.get(Thread, thread_id)
    if not thread:
        from flask import abort

        abort(404, "任务不存在")
    return thread


def _issue_command(host, thread, kind, payload, key=None):
    command = Command(
        host_id=host.id,
        thread_id=thread.id if thread else None,
        kind=kind,
        payload=payload,
        idempotency_key=key,
        runtime_id=host.runtime_id,
    )
    db.session.add(command)
    db.session.flush()
    wire = command.wire()
    result = command.as_dict()
    db.session.commit()  # Accepted commands must be durable before delivery.
    delivered = current_app.extensions["broker"].send_command({"host_id": host.id, "wire": wire})
    if delivered:
        # A fast node may already have completed the command; never overwrite its outcome.
        Command.query.filter_by(id=command.id, status="queued").update({"status": "sent"})
        db.session.commit()
        db.session.refresh(command)
        result = command.as_dict()
    return result


def _latest_history_import(thread):
    return (
        Command.query.filter_by(thread_id=thread.id, kind="thread.import")
        .order_by(Command.created_at.desc(), Command.id.desc())
        .first()
    )


def _history_import_pending(thread, command):
    return command is not None and (
        command.status in {"queued", "sent", "running"}
        # The RPC summary can arrive before durable import_complete. Only that
        # event makes the projected history available to the browser.
        or (command.status == "completed" and thread.status == "importing")
    )


def _start_history_import(host, thread):
    thread.imported = True
    thread.history_loaded = False
    thread.status = "importing"
    thread.active_turn_id = None
    thread.last_error = None
    if thread.cwd:
        thread.project_id = ensure_project(host, thread.cwd).id
    db.session.flush()
    return _issue_command(host, thread, "thread.import", dict(native_thread_id=thread.native_id))


def _save_image(host=None):
    owner_id = host.id if host else None
    file = request.files.get("file")
    if not file:
        raise ValueError("请选择图片")
    raw = file.read(20 * 1024 * 1024 + 1)
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ValueError("图片大小必须在 1 字节到 20 MiB 之间")
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 25_000_000:
            raise ValueError("图片像素总数超过 2500 万")
        fmt = image.format
        image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("文件不是有效的图片") from exc
    mime_ext = {
        "PNG": ("image/png", ".png"),
        "JPEG": ("image/jpeg", ".jpg"),
        "WEBP": ("image/webp", ".webp"),
        "GIF": ("image/gif", ".gif"),
    }
    if fmt not in mime_ext:
        raise ValueError("支持 PNG、JPEG、WebP 和 GIF 图片")
    mime, ext = mime_ext[fmt]
    asset_id = new_id()
    target = Path(current_app.config["ASSET_DIR"]) / (asset_id + ext)
    thread_id = None
    item_id = None
    if host:
        native = request.form.get("thread_id", "")
        thread = Thread.query.filter_by(host_id=host.id, native_id=native).first()
        if not thread:
            raise ValueError("图片所属的任务不存在或不属于这个节点")
        thread_id = thread.id
        item_id = request.form.get("item_id", "")[:250]
    asset = Asset(
        id=asset_id,
        host_id=host.id if host else None,
        thread_id=thread_id,
        native_item_id=item_id,
        name=secure_filename(file.filename or "")[:240] or f"image{ext}",
        mime=mime,
        size=len(raw),
        path=str(target),
    )
    target.write_bytes(raw)
    try:
        with current_app.extensions["broker"].command_lock:
            if owner_id:
                # Image validation and disk writes happen outside the control
                # lock. Recheck authentication after that work so deletion
                # cannot be followed by an orphaned late node upload.
                db.session.remove()
                owner = node_host()
                if owner is None or owner.id != owner_id:
                    from flask import abort

                    abort(401, "节点认证已失效")
                attached_thread = db.session.get(Thread, thread_id)
                if attached_thread is None or attached_thread.host_id != owner_id:
                    raise ValueError("图片所属的任务已不存在")
            db.session.add(asset)
            db.session.commit()
    except Exception:
        db.session.rollback()
        target.unlink(missing_ok=True)
        raise
    return asset


def create_app(test_config=None):
    load_dotenv()
    instance = Path.cwd() / "instance"
    instance.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, instance_path=str(instance), static_folder=None)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", ""),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", ""),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", f"sqlite:///{instance / 'anywhere.db'}"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        ASSET_DIR=os.getenv("ASSET_DIR", str(instance / "assets")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        PUBLIC_URL=os.getenv("PUBLIC_URL", ""),
        PERMANENT_SESSION_LIFETIME=43200,
        MAX_CONTENT_LENGTH=22 * 1024 * 1024,
        SOCK_SERVER_OPTIONS={"ping_interval": 25, "max_message_size": 32 * 1024 * 1024},
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] or not app.config["ADMIN_PASSWORD"]:
        raise RuntimeError(
            "请先运行 codex-anywhere init 生成本地配置，或设置 SECRET_KEY 和 ADMIN_PASSWORD"
        )
    app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(app.config["ADMIN_PASSWORD"])
    app.config["ASSET_DIR"] = str(Path(app.config["ASSET_DIR"]).resolve())
    Path(app.config["ASSET_DIR"]).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    app.extensions["broker"] = Broker()
    app.extensions["login_attempts"] = {}
    app.extensions["login_lock"] = threading.Lock()
    # This single-process hub can revoke a signed cookie and an already-open
    # WebSocket together. Restarting the hub intentionally requires a new login.
    app.extensions["auth_sessions"] = {}
    app.extensions["auth_lock"] = threading.RLock()
    writers = WriterSessions(app, _issue_command)
    app.extensions["writer_sessions"] = writers
    writers.register_routes(_json, _get_thread, _online_host)
    register_host_routes(app, _json, _identifier)
    with app.app_context():
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):

            @sa_event.listens_for(db.engine, "connect")
            def sqlite_settings(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        if app.testing:
            db.create_all()
    sock = Sock(app)

    def authenticated():
        nonce = session.get("auth_nonce")
        if not session.get("authenticated") or not isinstance(nonce, str):
            return False
        with app.extensions["auth_lock"]:
            expires = app.extensions["auth_sessions"].get(nonce)
            if expires is None or expires <= time.time():
                app.extensions["auth_sessions"].pop(nonce, None)
                return False
            return True

    def revoke_current_session():
        nonce = session.get("auth_nonce")
        if isinstance(nonce, str):
            with app.extensions["auth_lock"]:
                app.extensions["auth_sessions"].pop(nonce, None)
            writers.release_session(nonce)

    def issue_session(username):
        with app.extensions["auth_lock"]:
            revoke_current_session()
            session.clear()
            session.permanent = True
            nonce = secrets.token_urlsafe(32)
            now = time.time()
            registry = app.extensions["auth_sessions"]
            for expired in [key for key, until in registry.items() if until <= now]:
                registry.pop(expired, None)
            registry[nonce] = now + app.permanent_session_lifetime.total_seconds()
            session.update(authenticated=True, username=username, auth_nonce=nonce)

    def password_attempt_allowed():
        now = time.monotonic()
        address = request.remote_addr
        with app.extensions["login_lock"]:
            attempts = app.extensions["login_attempts"]
            recent = [t for t in attempts.get(address, []) if now - t < 60]
            if len(recent) >= 10:
                return False
            attempts[address] = recent + [now]
            return True

    @app.before_request
    def protect_api():
        if request.path in {"/healthz", "/api/auth/me", "/api/hosts/pair"}:
            return
        if request.path.startswith("/api/node/") or request.path == "/ws/node":
            if not node_host():
                return jsonify(error="节点认证失败"), 401
            return
        if request.path.startswith("/api/") or request.path == "/ws/client":
            if request.path != "/api/auth/login" and not authenticated():
                return jsonify(error="请先登录"), 401
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                csrf = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf_token", "")
                if not expected or not hmac.compare_digest(csrf.encode(), expected.encode()):
                    return jsonify(error="CSRF 校验失败，请刷新页面"), 403

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        db.session.rollback()
        return jsonify(error=str(exc)), 400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.errorhandler(IntegrityError)
    def conflict(_):
        db.session.rollback()
        return jsonify(error="数据已被其他请求更新，请刷新后重试"), 409

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", version="0.1.0")

    def auth_state():
        if session.get("authenticated") and not authenticated():
            session.clear()
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return dict(
            authenticated=authenticated(),
            username=session.get("username"),
            csrf_token=session["csrf_token"],
        )

    @app.get("/api/auth/me")
    def me():
        return jsonify(auth_state())

    @app.post("/api/auth/login")
    def login():
        data = _json()
        if not password_attempt_allowed():
            return jsonify(error="登录尝试过于频繁，请稍后重试"), 429
        with app.extensions["auth_lock"]:
            credentials = administrator_credentials()
            user_ok = hmac.compare_digest(
                str(data.get("username", "")).encode(), credentials["username"].encode()
            )
            password_ok = check_password_hash(
                credentials["password_hash"], str(data.get("password", ""))
            )
            if not (user_ok and password_ok):
                return jsonify(error="用户名或密码错误"), 401
            issue_session(credentials["username"])
            return jsonify(auth_state())

    @app.post("/api/auth/logout")
    def logout():
        revoke_current_session()
        session.clear()
        return jsonify(auth_state())

    @app.get("/api/settings")
    def settings():
        return jsonify(public_settings(administrator_credentials()))

    @app.patch("/api/settings/administrator")
    def update_administrator():
        username, current_password, new_password = administrator_changes(_json())
        if not password_attempt_allowed():
            return jsonify(error="登录尝试过于频繁，请稍后重试"), 429
        with app.extensions["auth_lock"]:
            # A second settings request may have passed before_request before
            # another save revoked its nonce while it waited for this lock.
            if not authenticated():
                return jsonify(error="请先登录"), 401
            credentials = administrator_credentials()
            if not check_password_hash(credentials["password_hash"], current_password):
                return jsonify(error="当前密码错误"), 403
            credentials["username"] = username
            if new_password is not None:
                credentials["password_hash"] = generate_password_hash(new_password)
            record = db.session.get(HubSetting, "administrator")
            if record is None:
                record = HubSetting(key="administrator")
                db.session.add(record)
            record.value = credentials
            db.session.commit()
            # Only a durable save revokes other browsers and their open sockets.
            writers.release_all()
            app.extensions["auth_sessions"].clear()
            issue_session(username)
            return jsonify(settings=public_settings(credentials), session=auth_state())

    @app.get("/api/hosts")
    def hosts():
        return jsonify(
            hosts=[
                h.as_dict(app.extensions["broker"].is_online(h.id))
                for h in Host.query.order_by(Host.name).all()
            ]
        )

    @app.get("/api/hosts/<host_id>/capabilities")
    def runtime_capabilities(host_id):
        host = db.session.get(Host, _identifier(host_id, "host_id"))
        if not host:
            return jsonify(error="节点不存在"), 404
        cwd = request.args.get("cwd")
        if cwd is not None and not is_absolute_directory(cwd):
            raise ValueError("工作目录必须是绝对路径")
        catalog = host.runtime_capabilities
        if catalog and (
            catalog.get("runtime_id") != host.runtime_id
            or not host.capabilities_updated_at
            or time.time() - host.capabilities_updated_at >= 60
            or (cwd is not None and catalog.get("cwd") != cwd)
        ):
            catalog = None
        return jsonify(
            capabilities=catalog,
            updated_at=iso(host.capabilities_updated_at) if catalog else None,
        )

    @app.post("/api/hosts/<host_id>/capabilities")
    def refresh_runtime_capabilities(host_id):
        data = _json()
        with app.extensions["broker"].command_lock:
            host = _online_host(host_id)
            require_runtime_settings(host, {"model": "capability-query"})
            payload = {}
            if data.get("cwd") is not None:
                if not is_absolute_directory(data["cwd"]):
                    raise ValueError("工作目录必须是绝对路径")
                payload["cwd"] = data["cwd"]
            refresh = data.get("refresh", False)
            if not isinstance(refresh, bool):
                raise ValueError("refresh 必须是布尔值")
            payload["refresh"] = refresh
            pending = Command.query.filter(
                Command.host_id == host.id,
                Command.kind == "runtime.capabilities",
                Command.runtime_id == host.runtime_id,
                Command.status.in_(["queued", "sent", "running"]),
            ).all()
            for command in pending:
                if command.payload == payload:
                    return jsonify(command=command.as_dict()), 202
            return jsonify(command=_issue_command(host, None, "runtime.capabilities", payload)), 202

    @app.post("/api/pairings")
    def pairing():
        code = secrets.token_hex(6).upper()
        record = Pairing(code_hash=token_hash(code), expires_at=time.time() + 600)
        db.session.add(record)
        db.session.commit()
        return jsonify(code=code, expires_at=iso(record.expires_at))

    @app.post("/api/hosts/pair")
    def pair_host():
        data = _json()
        code = str(data.get("code", "")).strip().upper()
        name = data.get("name", "")
        if not isinstance(name, str):
            raise ValueError("节点名称必须是字符串")
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("请提供有效的节点名称")
        with app.extensions["broker"].command_lock:
            record = Pairing.query.filter_by(code_hash=token_hash(code), consumed=False).first()
            if not record or record.expires_at < time.time():
                return jsonify(error="配对码无效或已过期"), 400
            # Atomic consumption also protects PostgreSQL deployments against a double claim.
            changed = Pairing.query.filter_by(id=record.id, consumed=False).update(
                {"consumed": True}
            )
            if not changed:
                db.session.rollback()
                return jsonify(error="配对码已使用"), 400
            token = secrets.token_urlsafe(48)
            host = Host(
                name=name,
                platform=str(data.get("platform", "unknown"))[:80],
                version=str(data.get("version", ""))[:120],
                token_hash=token_hash(token),
            )
            db.session.add(host)
            db.session.commit()
            return jsonify(host=host.as_dict(), token=token)

    @app.get("/api/threads")
    def threads():
        inputs = latest_thread_inputs()
        activity = thread_activity_at(inputs.c.created_at)
        query = Thread.query.outerjoin(inputs, inputs.c.thread_id == Thread.id).options(
            with_expression(Thread.last_activity_at, activity)
        )
        search = request.args.get("q", "").strip()[:200]
        if search:
            query = query.filter(
                Thread.title.ilike(f"%{search}%") | Thread.display_title.ilike(f"%{search}%")
            )
        archived = request.args.get("archived", "false")
        if archived not in {"false", "true", "all"}:
            raise ValueError("archived 必须是 false、true 或 all")
        if archived != "all":
            query = query.filter(Thread.archived.is_(archived == "true"))
        for field in ("host_id", "project_id"):
            if field in request.args:
                query = query.filter(
                    getattr(Thread, field) == _identifier(request.args[field], field)
                )
        limit = _integer(request.args.get("limit", 200), "limit", 1, 200)
        offset = _integer(request.args.get("offset", 0), "offset", 0, 2**31 - 1)
        rows = (
            query.order_by(Thread.pinned.desc(), activity.desc(), Thread.id.desc())
            .offset(offset)
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        return jsonify(
            threads=[t.as_dict() for t in rows[:limit]],
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )

    @app.get("/api/projects")
    def projects():
        inputs = latest_thread_inputs()
        last_activity_at = func.coalesce(
            func.max(thread_activity_at(inputs.c.created_at)), Project.created_at
        )
        query = db.session.query(Project, func.count(Thread.id), last_activity_at).outerjoin(
            Thread, (Thread.project_id == Project.id) & Thread.archived.is_(False)
        ).outerjoin(inputs, inputs.c.thread_id == Thread.id)
        if "host_id" in request.args:
            query = query.filter(Project.host_id == _identifier(request.args["host_id"], "host_id"))
        rows = (
            query.group_by(Project.id).order_by(last_activity_at.desc(), Project.id.desc()).all()
        )
        return jsonify(
            projects=[
                project.as_dict(thread_count=count, last_activity_at=activity)
                for project, count, activity in rows
            ]
        )

    @app.patch("/api/projects/<project_id>/settings")
    def update_project_settings(project_id):
        data = _json()
        settings = execution_settings(data.get("execution_settings"))
        with app.extensions["broker"].command_lock:
            project = db.session.get(Project, _identifier(project_id, "project_id"))
            if not project:
                return jsonify(error="项目不存在"), 404
            host = db.session.get(Host, project.host_id)
            require_runtime_settings(host, settings)
            project.execution_settings = settings
            emit("anywhere/project_settings", {"project_id": project.id}, host_id=host.id)
            db.session.commit()
            return jsonify(project=project.as_dict())

    @app.patch("/api/threads/<thread_id>/preferences")
    @_command_locked
    def update_thread_preferences(thread_id):
        changes = thread_preferences(_json())
        thread = _get_thread(thread_id)
        if any(getattr(thread, key) != value for key, value in changes.items()):
            for key, value in changes.items():
                setattr(thread, key, value)
            emit(
                "anywhere/thread_preferences",
                {
                    "threadId": thread.id,
                    "display_title": thread.display_title,
                    "pinned": thread.pinned,
                    "archived": thread.archived,
                },
                thread.id,
                thread.host_id,
            )
            db.session.commit()
        return jsonify(thread=thread.as_dict())

    @app.patch("/api/threads/<thread_id>/settings")
    def update_thread_settings(thread_id):
        data = _json()
        settings = execution_settings(data.get("execution_settings"))
        with app.extensions["broker"].command_lock:
            thread = _get_thread(thread_id)
            host = db.session.get(Host, thread.host_id)
            require_runtime_settings(host, settings)
            if "settings_revision" in data:
                expected = _integer(data["settings_revision"], "settings_revision", 0, 2**31 - 1)
                if expected != thread.settings_revision:
                    return jsonify(
                        error="运行设置已被更新，请刷新后重试", thread=thread.as_dict()
                    ), 409
            if settings != thread.execution_settings:
                thread.execution_settings = settings
                thread.settings_revision += 1
                emit("anywhere/thread_settings", {"thread_id": thread.id}, thread.id, host.id)
                db.session.commit()
            dispatch_queue(app, thread_id=thread.id)
            db.session.refresh(thread)
            return jsonify(thread=thread.as_dict())

    @app.post("/api/projects")
    @_command_locked
    def open_project():
        data = _json()
        host = _online_host(data.get("host_id"))
        cwd = data.get("cwd")
        if not is_absolute_directory(cwd):
            raise ValueError("项目目录必须是绝对路径")
        create = data.get("create_directory", False)
        if not isinstance(create, bool):
            raise ValueError("create_directory 必须是布尔值")
        payload = dict(cwd=cwd, create_directory=create)
        if "name" in data:
            if not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 240:
                raise ValueError("项目名称必须是 1–240 个字符")
            payload["name"] = data["name"].strip()
        return jsonify(command=_issue_command(host, None, "project.open", payload)), 202

    @app.post("/api/hosts/<host_id>/directories")
    @_command_locked
    def directories(host_id):
        data = _json()
        host = _online_host(host_id)
        payload = {}
        if data.get("path") is not None:
            if not is_absolute_directory(data["path"]):
                raise ValueError("浏览目录必须是绝对路径")
            payload["path"] = data["path"]
        return jsonify(command=_issue_command(host, None, "directory.list", payload)), 202

    @app.post("/api/hosts/<host_id>/catalog")
    @_command_locked
    def catalog(host_id):
        data = _json()
        host = _online_host(host_id)
        archived = data.get("archived", False)
        if not isinstance(archived, bool):
            raise ValueError("archived 必须是布尔值")
        payload = dict(limit=_integer(data.get("limit", 50), "limit", 1, 100), archived=archived)
        for key, maximum in (("cursor", 4000), ("search", 200)):
            if data.get(key) is not None:
                if not isinstance(data[key], str) or len(data[key]) > maximum:
                    raise ValueError(f"{key} 格式无效")
                payload[key] = data[key]
        if data.get("cwd") is not None:
            if not is_absolute_directory(data["cwd"]):
                raise ValueError("会话目录筛选必须是绝对路径")
            payload["cwd"] = data["cwd"]
        return jsonify(command=_issue_command(host, None, "thread.list", payload)), 202

    @app.post("/api/hosts/<host_id>/imports")
    @_command_locked
    def import_thread(host_id):
        data = _json()
        host = _online_host(host_id)
        native_id = _identifier(data.get("native_thread_id"), "native_thread_id")
        if len(native_id) > 200:
            raise ValueError("native_thread_id 最多 200 个字符")
        cwd = data.get("cwd", "")
        if cwd != "" and not is_absolute_directory(cwd):
            raise ValueError("会话目录必须是绝对路径")
        title = data.get("title", "")
        if not isinstance(title, str) or len(title) > 240:
            raise ValueError("会话标题格式无效")
        with app.extensions["broker"].command_lock:
            thread = Thread.query.filter_by(host_id=host.id, native_id=native_id).first()
            if thread and thread.status in {"active", "waiting", "starting"}:
                return jsonify(error="正在运行或等待交互的任务不能重新导入"), 409
            if thread:
                pending = _latest_history_import(thread)
                if _history_import_pending(thread, pending):
                    return jsonify(thread=thread.as_dict(), command=pending.as_dict()), 202
            if thread is None:
                thread = Thread(
                    host_id=host.id, native_id=native_id, cwd=cwd, title=title.strip() or "导入会话"
                )
                db.session.add(thread)
            else:
                if cwd:
                    thread.cwd = cwd
                if title.strip():
                    thread.title = title.strip()
            command = _start_history_import(host, thread)
            return jsonify(thread=thread.as_dict(), command=command), 202

    @app.post("/api/threads/<thread_id>/history")
    def load_history(thread_id):
        data = _json()
        cursor = data.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8192):
            raise ValueError("历史分页游标无效")
        limit = _integer(data.get("limit", 50), "limit", 1, 100)
        retry = data.get("retry", False)
        if not isinstance(retry, bool):
            raise ValueError("retry 必须是布尔值")
        with app.extensions["broker"].command_lock:
            thread = _get_thread(thread_id)
            host = _online_host(thread.host_id)
            if thread.status in {"active", "waiting", "starting"}:
                return jsonify(thread=thread.as_dict(), command=None)
            if "lazyHistory" in (host.capabilities or []):
                if not thread.native_id:
                    return jsonify(error="任务尚未创建完成"), 409
                latest = (
                    Command.query.filter(
                        Command.thread_id == thread.id,
                        Command.kind.in_(["thread.history", "thread.import"]),
                    )
                    .order_by(Command.created_at.desc(), Command.id.desc())
                    .first()
                )
                if latest and (
                    latest.status in {"queued", "sent", "running"}
                    or (
                        latest.status == "completed"
                        and (
                            (
                                latest.kind == "thread.history"
                                and not history_command_applied(latest)
                            )
                            or (latest.kind == "thread.import" and thread.status == "importing")
                        )
                    )
                ):
                    return jsonify(thread=thread.as_dict(), command=latest.as_dict()), 202
                if (
                    cursor is None
                    and thread.history_loaded
                    and not thread.history_stale
                    and not retry
                ):
                    return jsonify(thread=thread.as_dict(), command=None)
                if cursor is not None and (cursor != thread.history_cursor or thread.history_stale):
                    return jsonify(
                        error="历史已更新，请刷新最近消息后继续加载", thread=thread.as_dict()
                    ), 409
                if latest and latest.status in {"unknown", "failed"} and not retry:
                    return jsonify(
                        error=(
                            "上次历史读取失败或结果未知，请显式手动重试"
                            + (f"：{latest.error}" if latest.error else "")
                        ),
                        thread=thread.as_dict(),
                        command=latest.as_dict(),
                    ), 409
                payload = dict(
                    native_thread_id=thread.native_id,
                    limit=limit,
                    _native_fingerprint=thread.native_fingerprint,
                )
                if cursor:
                    payload["cursor"] = cursor
                if not thread.history_loaded:
                    thread.status = "importing"
                    thread.last_error = None
                command = _issue_command(host, thread, "thread.history", payload)
                return jsonify(thread=thread.as_dict(), command=command), 202
            if cursor:
                return jsonify(error="该节点不支持原生历史分页，请更新并重启节点"), 409
            if thread.history_loaded and not thread.history_stale:
                return jsonify(thread=thread.as_dict(), command=None)
            pending = _latest_history_import(thread)
            if _history_import_pending(thread, pending):
                return jsonify(thread=thread.as_dict(), command=pending.as_dict()), 202
            if pending and pending.status in {"unknown", "failed"}:
                error = (
                    "上次历史导入的执行结果未知，请显式手动重试"
                    if pending.status == "unknown"
                    else "上次历史导入失败，请显式手动重试"
                )
                return jsonify(error=error, thread=thread.as_dict(), command=pending.as_dict()), 409
            if not thread.native_id:
                return jsonify(error="任务尚未创建完成"), 409
            command = _start_history_import(host, thread)
            return jsonify(thread=thread.as_dict(), command=command), 202

    @app.post("/api/threads")
    @_command_locked
    def create_thread():
        data = _json()
        key = None
        signature = None
        if "client_request_id" in data:
            client_id = _identifier(data["client_request_id"], "client_request_id")
            if len(client_id) > 200:
                raise ValueError("client_request_id 最多 200 个字符")
            key = f"thread.start:{client_id}"
            signature = token_hash(
                json.dumps(
                    {name: value for name, value in data.items() if name != "client_request_id"},
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
            existing = Command.query.filter_by(idempotency_key=key).first()
            if existing:
                if existing.payload.get("_signature") != signature:
                    return jsonify(error="同一个请求 ID 对应了不同内容"), 409
                return jsonify(
                    thread=_get_thread(existing.thread_id).as_dict(), command=existing.as_dict()
                ), 202
        project = None
        if data.get("project_id") is not None:
            project = db.session.get(Project, _identifier(data["project_id"], "project_id"))
            if not project:
                return jsonify(error="项目不存在"), 404
            if data.get("host_id") is not None and data["host_id"] != project.host_id:
                raise ValueError("项目不属于所选节点")
            host = _online_host(project.host_id)
            cwd = project.cwd
        else:
            host = _online_host(data.get("host_id"))
            cwd = data.get("cwd", "")
            if not is_absolute_directory(cwd):
                raise ValueError("工作目录必须是绝对路径")
            project = ensure_project(host, cwd)
        supplied_title = data.get("title")
        title = supplied_title.strip()[:240] if isinstance(supplied_title, str) else ""
        settings = dict(project.execution_settings or {})
        if "execution_settings" in data:
            settings.update(execution_settings(data["execution_settings"]))
        require_runtime_settings(host, settings)
        thread = Thread(
            host_id=host.id,
            project_id=project.id,
            title=title or "新对话",
            cwd=cwd,
            execution_settings=settings,
            history_complete=True,
        )
        db.session.add(thread)
        db.session.flush()
        payload = dict(cwd=cwd)
        if title:
            payload["title"] = title
        if signature is not None:
            payload["_signature"] = signature
        if data.get("model"):
            payload["model"] = str(data["model"])[:200]
            if "model" not in settings:
                settings["model"] = payload["model"]
                thread.execution_settings = dict(settings)
        if "runtimeSettings" in (host.capabilities or []):
            payload.update(
                execution_settings=settings, settings_revision=thread.settings_revision or 0
            )
        command = _issue_command(host, thread, "thread.start", payload, key)
        return jsonify(thread=thread.as_dict(), command=command), 202

    @app.get("/api/threads/<thread_id>")
    def thread_detail(thread_id):
        thread = _get_thread(thread_id)
        query = Item.query.filter_by(thread_id=thread.id)
        paging = "item_limit" in request.args or "before_position" in request.args
        page_info = {}
        if paging:
            limit = _integer(request.args.get("item_limit", 100), "item_limit", 1, 200)
            if "before_position" in request.args:
                before = _integer(request.args["before_position"], "before_position", 0, 2**63 - 1)
                query = query.filter(Item.position < before)
            rows = (
                query.with_entities(Item.id, Item.position)
                .order_by(Item.position.desc(), Item.id.desc())
                .limit(limit + 1)
                .all()
            )
            has_more = len(rows) > limit
            items = list(reversed(rows[:limit]))
            page_info = dict(
                has_more_items=has_more,
                next_before_position=items[0].position if has_more else None,
            )
        else:
            items = query.order_by(Item.position, Item.id).all()
        if paging:
            displayed_items = []
            # Materialize at most one raw, potentially image-bearing item at a
            # time. Selecting a page must not deserialize 100 giant payloads
            # before their compact presentation can be constructed.
            for row in items:
                item = db.session.get(Item, row.id)
                displayed_items.append(browser_item(item))
                db.session.expunge(item)
        else:
            displayed_items = [item.as_dict() for item in items]
        interactions = (
            Interaction.query.filter_by(thread_id=thread.id).order_by(Interaction.created_at).all()
        )
        return jsonify(
            thread=thread.as_dict(),
            items=displayed_items,
            interactions=[i.as_dict() for i in interactions],
            message_queue=queue_state(thread),
            **page_info,
        )

    @app.get("/api/threads/<thread_id>/items/<item_id>")
    def full_item(thread_id, item_id):
        thread = _get_thread(thread_id)
        item = Item.query.filter_by(id=_identifier(item_id, "item_id"), thread_id=thread.id).first()
        if not item:
            return jsonify(error="消息不存在"), 404
        return jsonify(item=item.as_dict())

    def send_message(thread_id, kind):
        data = _json()
        overrides = (
            execution_settings(data["execution_settings"]) if "execution_settings" in data else None
        )
        if overrides is not None and kind == "turn.steer":
            raise ValueError("补充当前轮次时不能修改运行设置；请设置下一轮参数")
        text = data.get("text", "")
        assets_ids = data.get("asset_ids", [])
        refs = data.get("references", [])
        if not isinstance(text, str) or len(text) > 100_000:
            raise ValueError("消息文本无效或超过 10 万字符")
        if not isinstance(assets_ids, list) or len(assets_ids) > 4:
            raise ValueError("每条消息最多附加 4 张图片")
        for asset_id in assets_ids:
            _identifier(asset_id, "asset_id")
        _reference_shapes(refs)
        if not text.strip() and not assets_ids and not refs:
            raise ValueError("请输入消息或添加图片、引用")
        client_id = data.get("client_request_id")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError("请提供 client_request_id 以防止重复执行")
        signed_content = dict(text=text, asset_ids=assets_ids, references=refs)
        if overrides is not None:
            signed_content["execution_settings"] = overrides
        signature = token_hash(
            json.dumps(
                signed_content,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        key = f"{thread_id}:{kind}:{client_id}"
        with app.extensions["broker"].command_lock:
            existing = Command.query.filter_by(idempotency_key=key).first()
            if existing:
                if existing.payload.get("_signature") != signature:
                    return jsonify(error="同一个请求 ID 对应了不同内容"), 409
                return jsonify(command=existing.as_dict()), 202
            thread = _get_thread(thread_id)
            if not thread.history_loaded or thread.status == "importing":
                return jsonify(error="会话历史仍在导入，请稍后重试"), 409
            host = db.session.get(Host, thread.host_id)
            if not app.extensions["broker"].is_online(host.id):
                return jsonify(error="节点当前离线"), 409
            if not thread.native_id:
                return jsonify(error="任务尚未创建完成"), 409
            should_queue = kind == "turn.start" and should_enqueue(thread, host)
            if kind == "turn.steer" and not thread.active_turn_id:
                return jsonify(error="当前没有可补充要求的执行轮次"), 409
            assets = []
            size = 0
            inputs = []
            for asset_id in assets_ids:
                asset = db.session.get(Asset, asset_id)
                if not asset:
                    raise ValueError("附件不存在")
                size += asset.size
                if size > 20 * 1024 * 1024:
                    raise ValueError("单条消息的图片合计不能超过 20 MiB")
                try:
                    with _asset_file(asset).open("rb") as stream:
                        raw = stream.read(20 * 1024 * 1024 + 1)
                except OSError as exc:
                    raise ValueError("附件无法读取，请重新上传") from exc
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError("附件文件超过 20 MiB，请重新上传")
                inputs.append(
                    dict(
                        type="image",
                        url=f"data:{asset.mime};base64," + base64.b64encode(raw).decode(),
                    )
                )
                assets.append(asset.as_dict())
            references = resolve_references(refs)
            inputs.insert(0, dict(type="text", text=quote_references(text, references)))
            payload = dict(
                native_thread_id=thread.native_id,
                input=inputs,
                _signature=signature,
                _references=references,
                _display_text=text,
                _assets=assets,
            )
            if kind == "turn.start":
                settings = dict(thread.execution_settings or {})
                if overrides is not None:
                    settings.update(overrides)
                # Legacy model-only thread creation remains usable on old nodes.
                if "runtimeSettings" in (host.capabilities or []):
                    require_runtime_settings(host, settings)
                    payload.update(
                        execution_settings=settings, settings_revision=thread.settings_revision
                    )
                elif overrides or set(settings) - {"model"}:
                    # A downgraded node must not ignore saved sandbox restrictions.
                    require_runtime_settings(host, settings)
                if not should_queue:
                    for history_command in Command.query.filter_by(
                        thread_id=thread.id, kind="thread.history"
                    ).filter(Command.status.in_(["queued", "sent", "running", "completed"])):
                        if not history_command_applied(history_command):
                            history_command.status = "failed"
                            history_command.error = "会话已开始新轮次，此历史页未应用；请刷新历史"
            if kind == "turn.steer":
                payload["expected_turn_id"] = thread.active_turn_id
            if (
                kind == "turn.start"
                and thread.title == "新对话"
                and not Command.query.filter(
                    Command.thread_id == thread.id,
                    Command.kind.in_(["turn.start", "turn.queue"]),
                ).first()
            ):
                creation = Command.query.filter_by(
                    thread_id=thread.id, kind="thread.start"
                ).first()
                if creation and not creation.payload.get("title"):
                    # Use original user text, not quoted reference contents.
                    # Explicit and imported names can also equal the placeholder.
                    first_line = next((line for line in text.splitlines() if line.strip()), "")
                    thread.title = " ".join(first_line.split())[:80] or (
                        "图片对话" if assets else "引用对话"
                    )
            if should_queue:
                if overrides is not None:
                    payload["_queue_overrides"] = overrides
                command = enqueue_prepared(host, thread, payload, key)
                # The previous turn may have finished while this request was in
                # flight. Its place in the queue remains stable either way.
                dispatch_queue(app, host_id=host.id, thread_id=thread.id)
                db.session.refresh(command)
                return jsonify(command=command.as_dict()), 202
            thread.status = "active"
            thread.last_error = None
            command = _issue_command(host, thread, kind, payload, key)
            return jsonify(command=command), 202

    @app.post("/api/threads/<thread_id>/messages")
    def message(thread_id):
        return send_message(thread_id, "turn.start")

    @app.post("/api/threads/<thread_id>/steer")
    def steer(thread_id):
        return send_message(thread_id, "turn.steer")

    @app.get("/api/threads/<thread_id>/queue")
    def queued_messages(thread_id):
        with app.extensions["broker"].command_lock:
            return jsonify(queue_state(_get_thread(thread_id)))

    @app.patch("/api/threads/<thread_id>/queue/<command_id>")
    @_command_locked
    def edit_queued_message(thread_id, command_id):
        data = _json()
        if set(data) - {"text", "revision"}:
            raise ValueError("排队消息只能修改文本")
        thread = _get_thread(thread_id)
        command = update_queued(
            thread, _identifier(command_id, "command_id"), data.get("text"), data.get("revision")
        )
        message = next(item for item in queue_state(thread)["messages"] if item["id"] == command.id)
        return jsonify(message=message)

    @app.delete("/api/threads/<thread_id>/queue/<command_id>")
    @_command_locked
    def delete_queued_message(thread_id, command_id):
        data = _json()
        if set(data) - {"revision"}:
            raise ValueError("取消排队消息的字段无效")
        thread = _get_thread(thread_id)
        cancel_queued(thread, _identifier(command_id, "command_id"), data.get("revision"))
        return jsonify(cancelled=True)

    @app.post("/api/threads/<thread_id>/queue/resume")
    @_command_locked
    def continue_queued_messages(thread_id):
        if _json():
            raise ValueError("继续队列不接受额外参数")
        _get_thread(thread_id)
        command = resume_queue(app, thread_id)
        db.session.remove()
        return jsonify(
            command=command.as_dict() if command else None,
            queue=queue_state(_get_thread(thread_id)),
        ), 202

    @app.post("/api/threads/<thread_id>/interrupt")
    def interrupt(thread_id):
        with app.extensions["broker"].command_lock:
            thread = _get_thread(thread_id)
            if not thread.active_turn_id:
                return jsonify(error="当前没有运行中的轮次"), 409
            host = db.session.get(Host, thread.host_id)
            if not app.extensions["broker"].is_online(host.id):
                return jsonify(error="节点当前离线"), 409
            return jsonify(
                command=_issue_command(
                    host,
                    thread,
                    "turn.interrupt",
                    dict(native_thread_id=thread.native_id, turn_id=thread.active_turn_id),
                )
            ), 202

    @app.get("/api/commands/<command_id>")
    def command_detail(command_id):
        command = db.session.get(Command, command_id)
        if not command:
            return jsonify(error="指令不存在"), 404
        return jsonify(command=command.as_dict())

    @app.get("/api/events/<int:event_id>")
    def full_event(event_id):
        event = db.session.get(Event, event_id)
        if not event:
            return jsonify(error="事件不存在"), 404
        return jsonify(event=event.as_dict())

    @app.get("/api/interactions")
    def interaction_list():
        limit = _integer(request.args.get("limit", 50), "limit", 1, 200)
        offset = _integer(request.args.get("offset", 0), "offset", 0, 10**9)
        return jsonify(pending_interactions(limit, offset))

    @app.get("/api/interactions/<interaction_id>")
    def interaction_detail(interaction_id):
        interaction = db.session.get(Interaction, _identifier(interaction_id, "interaction_id"))
        if not interaction:
            return jsonify(error="交互请求不存在"), 404
        return jsonify(interaction=interaction.as_dict())

    @app.post("/api/interactions/<interaction_id>/answer")
    def answer(interaction_id):
        data = _json()
        response = data.get("response")
        if not isinstance(response, dict):
            raise ValueError("response 必须是 JSON 对象")
        with app.extensions["broker"].command_lock:
            db.session.remove()
            interaction = db.session.get(Interaction, interaction_id)
            if not interaction:
                return jsonify(error="交互请求不存在"), 404
            host = db.session.get(Host, interaction.host_id)
            if (
                not host
                or interaction.status != "pending"
                or interaction.runtime_id != host.runtime_id
            ):
                return jsonify(error="该请求已处理或已过期"), 409
            if not app.extensions["broker"].is_online(host.id):
                return jsonify(error="节点当前离线"), 409
            if interaction.method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                decision = response.get("decision")
                available = interaction.params.get("availableDecisions")
                if available is None:
                    available = ["accept", "acceptForSession", "decline", "cancel"]
                if decision not in available:
                    raise ValueError("请选择该请求允许的审批选项")
            validate_user_input(interaction, response)
            interaction.status = "answering"
            thread = (
                db.session.get(Thread, interaction.thread_id) if interaction.thread_id else None
            )
            command = _issue_command(
                host,
                thread,
                "interaction.answer",
                dict(
                    rpc_id=interaction.rpc_id,
                    runtime_id=interaction.runtime_id,
                    response=response,
                    _interaction_id=interaction.id,
                ),
            )
            return jsonify(command=command), 202

    @app.post("/api/assets")
    def upload():
        return jsonify(asset=_save_image().as_dict())

    @app.post("/api/node/assets")
    def node_upload():
        db.session.remove()
        host = node_host()
        if host is None:
            return jsonify(error="节点认证已失效"), 401
        return jsonify(asset=_save_image(host).as_dict())

    @app.get("/api/assets/<asset_id>")
    def download(asset_id):
        asset = db.session.get(Asset, asset_id)
        if not asset:
            return jsonify(error="附件不存在"), 404
        try:
            return send_file(
                _asset_file(asset),
                mimetype=asset.mime,
                download_name=asset.name,
                conditional=True,
                max_age=0,
            )
        except (ValueError, OSError):
            return jsonify(error="附件不存在"), 404

    @app.post("/api/references/resolve")
    @_command_locked
    def resolve():
        references = resolve_references(_reference_shapes(_json().get("references", [])))
        db.session.commit()
        return jsonify(references=references)

    def origin_valid():
        origin = request.headers.get("Origin")
        expected = urlsplit(app.config["PUBLIC_URL"] or request.host_url)
        return bool(
            origin
            and urlsplit(origin).netloc == expected.netloc
            and urlsplit(origin).scheme == expected.scheme
        )

    @sock.route("/ws/client")
    def client_socket(ws):
        install_full_writes(ws)
        if not origin_valid():
            ws.close()
            return
        ws.send(json.dumps({"type": "ready"}))
        cursor = None
        thread_id = None
        while ws.connected:
            if not authenticated():
                ws.close(reason=1008, message="Session expired")
                return
            raw = ws.receive(timeout=0.15)
            if raw:
                try:
                    frame = json.loads(raw)
                    if frame.get("type") == "subscribe":
                        after = frame.get("after", 0)
                        if type(after) is not int or not 0 <= after <= 2**63 - 1:
                            raise ValueError("Invalid event cursor")
                        cursor = after
                        thread_id = frame.get("thread_id")
                        if thread_id is not None:
                            _identifier(thread_id, "thread_id")
                except (ValueError, TypeError, AttributeError):
                    ws.close()
                    return
            if cursor is None:
                continue
            # End every read transaction; a long-lived snapshot would hide new events.
            db.session.remove()
            query = Event.query.filter(Event.id > cursor)
            if thread_id:
                query = query.filter((Event.thread_id == thread_id) | Event.thread_id.is_(None))
            event_ids = query.with_entities(Event.id).order_by(Event.id).limit(100).all()
            for (event_id,) in event_ids:
                if not authenticated():
                    ws.close(reason=1008, message="Session expired")
                    return
                event = db.session.get(Event, event_id)
                if event is None:
                    cursor = event_id
                    continue
                ws.send(json.dumps(browser_event(event), ensure_ascii=False))
                cursor = event_id
                db.session.expunge(event)

    @sock.route("/ws/node")
    def node_socket(ws):
        install_full_writes(ws)
        broker = app.extensions["broker"]
        with broker.command_lock:
            db.session.remove()
            host = node_host()
            host_id = host.id if host else None
            attached = bool(host_id and broker.attach(host_id, ws))
            db.session.remove()
        if host_id is None:
            ws.close(reason=1008, message="节点认证已失效")
            return
        if not attached:
            ws.send(json.dumps({"type": "error", "error": "该节点已有活动连接"}))
            ws.close()
            return
        try:
            raw = ws.receive(timeout=15)
            frame = json.loads(raw) if raw else {}
            if frame.get("type") != "hello":
                raise ValueError("hello is required")
            handle_node_message(
                app,
                host_id,
                frame,
                flush_outgoing=lambda: broker.pump(host_id, ws),
                outgoing_socket=ws,
            )
            while ws.connected and broker.is_attached(host_id, ws):
                # HTTP handlers only enqueue. This connection's owner thread
                # performs all application writes without waiting for inbound
                # traffic or a heartbeat to wake a long receive timeout.
                if not broker.pump(host_id, ws):
                    break
                raw = ws.receive(timeout=0.1)
                if raw is None:
                    if not broker.is_online(host_id):
                        break
                    continue
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError("Invalid node frame")
                handle_node_message(app, host_id, frame)
        except Exception as exc:
            app.logger.warning("Node connection ended (%s): %s", host_id, type(exc).__name__)
        finally:
            broker.detach(host_id, ws)
            with app.app_context(), broker.command_lock:
                db.session.remove()
                if db.session.get(Host, host_id) is not None and not broker.is_online(host_id):
                    emit("anywhere/host_offline", {"host_id": host_id}, host_id=host_id)
                    db.session.commit()
            try:
                ws.close()
            except Exception:
                pass

    web_dist = Path(os.getenv("WEB_DIST", str(Path.cwd() / "web" / "dist"))).resolve()

    @app.get("/")
    @app.get("/settings")
    @app.get("/settings/")
    @app.get("/tasks/<task_id>")
    def index(task_id=None):
        if not (web_dist / "index.html").exists():
            return "前端尚未构建，请在 web 目录运行 npm install 和 npm run build", 503
        response = send_from_directory(web_dist, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/<path:path>")
    def static_file(path):
        if path.startswith(("api/", "ws/")):
            return jsonify(error="接口不存在"), 404
        return send_from_directory(web_dist, path)

    return app
