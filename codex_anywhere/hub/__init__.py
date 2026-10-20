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
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, current_app, jsonify, request, send_file, send_from_directory, session
from flask_sock import Sock
from PIL import Image, UnidentifiedImageError
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .broker import Broker
from .models import Asset, Command, Event, Host, Interaction, Item, Pairing, Thread, db, iso, new_id
from .references import quote_references, resolve_references
from .transport import emit, handle_node_message


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def valid_cwd(cwd, roots):
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return False
    # Validate in the host's path syntax, not the hub operating system's syntax.
    windows = bool(ntpath.splitdrive(cwd)[0])
    pathmod = ntpath if windows else posixpath
    if not pathmod.isabs(cwd):
        return False
    target = pathmod.normcase(pathmod.normpath(cwd))
    for root in roots:
        if not isinstance(root, str):
            continue
        root = pathmod.normcase(pathmod.normpath(root))
        try:
            if pathmod.commonpath([target, root]) == root:
                return True
        except ValueError:
            pass
    return False


def node_host():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return Host.query.filter_by(token_hash=token_hash(auth[7:])).first()


def _json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("请求必须是 JSON 对象")
    return data


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


def _save_image(host=None):
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
        db.session.add(asset)
        db.session.commit()
    except Exception:
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

    @app.before_request
    def protect_api():
        if request.path in {"/healthz", "/api/auth/me", "/api/hosts/pair"}:
            return
        if request.path.startswith("/api/node/") or request.path == "/ws/node":
            if not node_host():
                return jsonify(error="节点认证失败"), 401
            return
        if request.path.startswith("/api/") or request.path == "/ws/client":
            if request.path != "/api/auth/login" and not session.get("authenticated"):
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
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return dict(
            authenticated=bool(session.get("authenticated")),
            username=session.get("username"),
            csrf_token=session["csrf_token"],
        )

    @app.get("/api/auth/me")
    def me():
        return jsonify(auth_state())

    @app.post("/api/auth/login")
    def login():
        data = _json()
        now = time.monotonic()
        address = request.remote_addr
        with app.extensions["login_lock"]:
            attempts = app.extensions["login_attempts"]
            recent = [t for t in attempts.get(address, []) if now - t < 60]
            if len(recent) >= 10:
                return jsonify(error="登录尝试过于频繁，请稍后重试"), 429
            attempts[address] = recent + [now]
        user_ok = hmac.compare_digest(
            str(data.get("username", "")).encode(), app.config["ADMIN_USERNAME"].encode()
        )
        password_ok = check_password_hash(
            app.config["ADMIN_PASSWORD_HASH"], str(data.get("password", ""))
        )
        if not (user_ok and password_ok):
            return jsonify(error="用户名或密码错误"), 401
        session.clear()
        session.permanent = True
        session.update(authenticated=True, username=app.config["ADMIN_USERNAME"])
        return jsonify(auth_state())

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(auth_state())

    @app.get("/api/hosts")
    def hosts():
        return jsonify(
            hosts=[
                h.as_dict(app.extensions["broker"].is_online(h.id))
                for h in Host.query.order_by(Host.name).all()
            ]
        )

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
        roots = data.get("roots", [])
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 120 or not isinstance(roots, list) or not 1 <= len(roots) <= 32:
            raise ValueError("请提供节点名称和 1–32 个工作目录")
        if not all(isinstance(r, str) and valid_cwd(r, [r]) for r in roots):
            raise ValueError("工作目录必须是绝对路径")
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
                roots=roots,
                version=str(data.get("version", ""))[:120],
                token_hash=token_hash(token),
            )
            db.session.add(host)
            db.session.commit()
            return jsonify(host=host.as_dict(), token=token)

    @app.get("/api/threads")
    def threads():
        query = Thread.query
        search = request.args.get("q", "").strip()[:200]
        if search:
            query = query.filter(Thread.title.ilike(f"%{search}%"))
        return jsonify(
            threads=[t.as_dict() for t in query.order_by(Thread.updated_at.desc()).limit(200)]
        )

    @app.post("/api/threads")
    def create_thread():
        data = _json()
        host = db.session.get(Host, data.get("host_id"))
        if not host:
            return jsonify(error="节点不存在"), 404
        if not app.extensions["broker"].is_online(host.id):
            return jsonify(error="节点当前离线"), 409
        cwd = data.get("cwd", "")
        if not valid_cwd(cwd, host.roots):
            raise ValueError("工作目录必须在节点已授权的目录内")
        title = str(data.get("title", "新任务")).strip()[:240] or "新任务"
        thread = Thread(host_id=host.id, title=title, cwd=cwd)
        db.session.add(thread)
        db.session.flush()
        payload = dict(cwd=cwd, title=title)
        if data.get("model"):
            payload["model"] = str(data["model"])[:200]
        command = _issue_command(host, thread, "thread.start", payload)
        return jsonify(thread=thread.as_dict(), command=command), 202

    @app.get("/api/threads/<thread_id>")
    def thread_detail(thread_id):
        thread = _get_thread(thread_id)
        items = Item.query.filter_by(thread_id=thread.id).order_by(Item.created_at, Item.id).all()
        interactions = (
            Interaction.query.filter_by(thread_id=thread.id).order_by(Interaction.created_at).all()
        )
        return jsonify(
            thread=thread.as_dict(),
            items=[i.as_dict() for i in items],
            interactions=[i.as_dict() for i in interactions],
        )

    def send_message(thread_id, kind):
        data = _json()
        text = data.get("text", "")
        assets_ids = data.get("asset_ids", [])
        refs = data.get("references", [])
        if not isinstance(text, str) or len(text) > 100_000:
            raise ValueError("消息文本无效或超过 10 万字符")
        if not isinstance(assets_ids, list) or len(assets_ids) > 4:
            raise ValueError("每条消息最多附加 4 张图片")
        if not text.strip() and not assets_ids and not refs:
            raise ValueError("请输入消息或添加图片、引用")
        client_id = data.get("client_request_id")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError("请提供 client_request_id 以防止重复执行")
        signature = token_hash(
            json.dumps(
                dict(text=text, asset_ids=assets_ids, references=refs),
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
            host = db.session.get(Host, thread.host_id)
            if not app.extensions["broker"].is_online(host.id):
                return jsonify(error="节点当前离线"), 409
            if not thread.native_id:
                return jsonify(error="任务尚未创建完成"), 409
            if kind == "turn.start" and thread.status in {"active", "waiting"}:
                return jsonify(error="任务正在运行，请补充要求或等待完成"), 409
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
                raw = Path(asset.path).read_bytes()
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
            if kind == "turn.steer":
                payload["expected_turn_id"] = thread.active_turn_id
            thread.status = "active"
            command = _issue_command(host, thread, kind, payload, key)
            return jsonify(command=command), 202

    @app.post("/api/threads/<thread_id>/messages")
    def message(thread_id):
        return send_message(thread_id, "turn.start")

    @app.post("/api/threads/<thread_id>/steer")
    def steer(thread_id):
        return send_message(thread_id, "turn.steer")

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

    @app.post("/api/interactions/<interaction_id>/answer")
    def answer(interaction_id):
        data = _json()
        response = data.get("response")
        if not isinstance(response, dict):
            raise ValueError("response 必须是 JSON 对象")
        with app.extensions["broker"].command_lock:
            interaction = db.session.get(Interaction, interaction_id)
            if not interaction:
                return jsonify(error="交互请求不存在"), 404
            host = db.session.get(Host, interaction.host_id)
            if interaction.status != "pending" or interaction.runtime_id != host.runtime_id:
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
        return jsonify(asset=_save_image(node_host()).as_dict())

    @app.get("/api/assets/<asset_id>")
    def download(asset_id):
        asset = db.session.get(Asset, asset_id)
        if not asset or not Path(asset.path).is_file():
            return jsonify(error="附件不存在"), 404
        return send_file(
            asset.path, mimetype=asset.mime, download_name=asset.name, conditional=True, max_age=0
        )

    @app.post("/api/references/resolve")
    def resolve():
        references = resolve_references(_json().get("references", []))
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
        if not origin_valid():
            ws.close()
            return
        ws.send(json.dumps({"type": "ready"}))
        cursor = None
        thread_id = None
        while ws.connected:
            raw = ws.receive(timeout=0.15)
            if raw:
                try:
                    frame = json.loads(raw)
                    if frame.get("type") == "subscribe":
                        cursor = max(0, int(frame.get("after", 0)))
                        thread_id = frame.get("thread_id")
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
            for event in query.order_by(Event.id).limit(100).all():
                ws.send(json.dumps(event.as_dict(), ensure_ascii=False))
                cursor = event.id

    @sock.route("/ws/node")
    def node_socket(ws):
        host = node_host()
        host_id = host.id
        db.session.remove()
        broker = app.extensions["broker"]
        if not broker.attach(host_id, ws):
            ws.send(json.dumps({"type": "error", "error": "该节点已有活动连接"}))
            ws.close()
            return
        try:
            raw = ws.receive(timeout=15)
            frame = json.loads(raw) if raw else {}
            if frame.get("type") != "hello":
                raise ValueError("hello is required")
            handle_node_message(app, host_id, frame)
            while ws.connected:
                raw = ws.receive(timeout=40)
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
            with app.app_context():
                emit("anywhere/host_offline", {"host_id": host_id}, host_id=host_id)
                db.session.commit()
            ws.close()

    web_dist = Path(os.getenv("WEB_DIST", str(Path.cwd() / "web" / "dist"))).resolve()

    @app.get("/")
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
