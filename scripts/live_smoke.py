"""Opt-in live acceptance checks against the installed, signed-in Codex.

Runs its own loopback hub and an isolated workspace, never the user's project.
Unlike the automated tests, this script makes real model requests.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import platform
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image
from werkzeug.serving import make_server

from codex_anywhere.cli import upgrade_database
from codex_anywhere.hub import create_app
from codex_anywhere.hub.models import db
from codex_anywhere.node import pair_node
from codex_anywhere.node.config import load_config
from codex_anywhere.node.service import NodeService


def wait_for(check, description, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out: {description}")


class LiveSystem:
    def __init__(self, directory: Path, port: int, codex_bin: str, timeout: int):
        self.directory = directory
        self.workspace = directory / "workspace"
        self.workspace.mkdir(parents=True)
        self.timeout = timeout
        self.results = []
        self.command_timings = []
        self.holder = {}
        self.password = "live-verification-only"
        self.app = create_app(
            {
                "SECRET_KEY": secrets.token_urlsafe(48),
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": self.password,
                "SESSION_COOKIE_SECURE": False,
                "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(directory / "hub.db"),
                "ASSET_DIR": str(directory / "assets"),
                "PUBLIC_URL": "",
            }
        )
        upgrade_database(self.app)
        self.server = make_server("127.0.0.1", port, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.app.config["PUBLIC_URL"] = self.url
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.client = httpx.Client(base_url=self.url, timeout=30, trust_env=False)
        self.csrf = self.client.get("/api/auth/me").json()["csrf_token"]
        state = self.post("/api/auth/login", {"username": "admin", "password": self.password})
        self.csrf = state["csrf_token"]
        code = self.post("/api/pairings", {})["code"]
        self.host = pair_node(
            self.url,
            f"Live verification · {platform.system()}",
            directory / "node",
            code,
        )

        def run_node():
            async def run():
                service = NodeService(
                    load_config(directory / "node"), directory / "node", codex_bin
                )
                self.holder.update(service=service, loop=asyncio.get_running_loop())
                task = asyncio.create_task(service.run())
                self.holder["task"] = task
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.holder["error"] = str(exc)

            asyncio.run(run())

        self.node_thread = threading.Thread(target=run_node, daemon=True)
        self.node_thread.start()
        wait_for(lambda: self.online() or self.fail_node(), "real Codex node online", timeout)
        print(f"LIVE_HUB_URL={self.url}", flush=True)
        print(f"WORKSPACE={self.workspace}", flush=True)

    def fail_node(self):
        if self.holder.get("error"):
            raise RuntimeError(self.holder["error"])
        return False

    def online(self):
        return any(
            h["id"] == self.host["id"] and h["online"]
            for h in self.client.get("/api/hosts").json()["hosts"]
        )

    def post(self, path, body):
        response = self.client.post(path, json=body, headers={"X-CSRF-Token": self.csrf})
        if response.is_error:
            raise AssertionError(f"{path}: {response.status_code} {response.json().get('error')}")
        return response.json()

    def command(self, command):
        started = time.monotonic()

        def completed():
            result = self.client.get(f"/api/commands/{command['id']}").json()["command"]
            if result["status"] in {"failed", "unknown"}:
                raise AssertionError(f"{result['kind']}: {result['error']}")
            return result if result["status"] == "completed" else None

        result = wait_for(completed, command["kind"], self.timeout)
        self.command_timings.append(
            dict(kind=command["kind"], seconds=round(time.monotonic() - started, 3))
        )
        return result

    def thread(self, title):
        result = self.post(
            "/api/threads", dict(host_id=self.host["id"], title=title, cwd=str(self.workspace))
        )
        self.command(result["command"])
        return result["thread"]["id"]

    def history(self, thread_id):
        return self.client.get(f"/api/threads/{thread_id}").json()

    def send(self, thread_id, text, **extra):
        result = self.post(
            f"/api/threads/{thread_id}/messages",
            dict(text=text, client_request_id=uuid4().hex, **extra),
        )
        self.command(result["command"])
        return result

    def answer(self, thread_id):
        def done():
            history = self.history(thread_id)
            pending = [i for i in history["interactions"] if i["status"] == "pending"]
            if pending:
                raise AssertionError("A real approval is pending; inspect it in the live hub")
            state = history["thread"]["status"]
            if state == "error":
                raise AssertionError(history["thread"].get("last_error") or "Codex turn failed")
            if state not in {"idle", "interrupted"}:
                return None
            messages = [i for i in history["items"] if i["type"] == "agentMessage"]
            return (history, messages[-1]["data"].get("text", "")) if messages else None

        return wait_for(done, "real Codex answer", self.timeout)

    def record(self, name, action):
        started = time.monotonic()
        try:
            details = action() or {}
            result = dict(
                check=name, status="passed", seconds=round(time.monotonic() - started, 2), **details
            )
        except Exception as exc:
            result = dict(
                check=name,
                status="failed",
                seconds=round(time.monotonic() - started, 2),
                error=str(exc),
            )
            self.results.append(result)
            self.save()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            raise
        self.results.append(result)
        self.save()
        print(json.dumps(result, ensure_ascii=False), flush=True)

    def save(self):
        report = dict(
            url=self.url,
            workspace=str(self.workspace),
            checks=self.results,
            command_timings=self.command_timings,
            environment=dict(
                platform=platform.platform(),
                python=platform.python_version(),
                codex=self.holder["service"].runtime.version.get("userAgent"),
            ),
        )
        (self.directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def close(self):
        if self.holder.get("task"):
            self.holder["loop"].call_soon_threadsafe(self.holder["task"].cancel)
            self.node_thread.join(timeout=15)
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def run(self):
        source = self.thread("真实 Codex · 问答与图片")
        task_nonce = "TASK_NONCE_" + secrets.token_hex(4)

        def basic():
            self.send(source, f"这是一项软件验收测试。只回复 {task_nonce}，不要调用工具。")
            _, text = self.answer(source)
            assert task_nonce in text, text
            return dict(thread_id=source, answer=text)

        self.record("real_model_answer", basic)

        image = Image.new("RGB", (320, 160), "red")
        image.paste(Image.new("RGB", (160, 160), "blue"), (160, 0))
        image_path = self.workspace / "test-image.png"
        image.save(image_path)

        def image_input():
            response = self.client.post(
                "/api/assets",
                files={"file": ("test-image.png", image_path.read_bytes(), "image/png")},
                headers={"X-CSRF-Token": self.csrf},
            )
            response.raise_for_status()
            asset = response.json()["asset"]
            self.send(
                source,
                "判断所附图片左半部和右半部的颜色。仅回复 JSON，left/right 为英文颜色名称。",
                asset_ids=[asset["id"]],
            )
            _, text = self.answer(source)
            assert "red" in text.lower() and "blue" in text.lower(), text
            return dict(answer=text)

        self.record("real_image_input", image_input)

        def image_output():
            self.send(
                source,
                "使用图片查看工具打开当前项目目录里的 test-image.png，"
                "并在最终回答中用 Markdown 嵌入该本地图片。不要生成新图片，不要执行命令。",
            )
            _, text = self.answer(source)

            def uploaded():
                history = self.history(source)
                return next(
                    (
                        asset
                        for item in history["items"]
                        for asset in item["data"].get("_assets", [])
                        if asset.get("source_path")
                    ),
                    None,
                )

            asset = wait_for(uploaded, "real output image bridge", 30)
            assert self.client.get(asset["url"]).status_code == 200
            return dict(answer=text, asset_url=asset["url"])

        self.record("real_image_output", image_output)

        def large_image_input():
            large = Image.new("RGB", (1536, 768), "red")
            large.paste(Image.new("RGB", (768, 768), "blue"), (768, 0))
            large.paste(Image.frombytes("RGB", (1536, 256), os.urandom(1536 * 256 * 3)), (0, 512))
            large_path = self.workspace / "large-test-image.png"
            large.save(large_path)
            raw = large_path.read_bytes()
            assert len(raw) > 1024 * 1024, "Test needs an actual large WebSocket frame"
            response = self.client.post(
                "/api/assets",
                files={"file": (large_path.name, raw, "image/png")},
                headers={"X-CSRF-Token": self.csrf},
            )
            response.raise_for_status()
            self.send(
                source,
                "观察新附图的上半部分，判断左上和右上的大色块颜色。"
                "忽略底部噪点。只输出英文颜色名称。",
                asset_ids=[response.json()["asset"]["id"]],
            )
            _, text = self.answer(source)
            assert "red" in text.lower() and "blue" in text.lower(), text
            return dict(image_bytes=len(raw), answer=text)

        self.record("real_large_image_input", large_image_input)

        def references():
            target = self.thread("真实 Codex · 任务和网页引用")
            web_nonce = "WEB_NONCE_" + secrets.token_hex(4)
            self.send(
                target,
                "从引用任务和网页摘录中，提取 TASK_NONCE_ 和 WEB_NONCE_ 开头的"
                "两个完整校验串，仅输出这两个串。不要调用工具。",
                references=[
                    dict(kind="task", thread_id=source),
                    dict(
                        kind="web",
                        url="https://example.com/acceptance",
                        title="验收摘录",
                        excerpt=f"网页校验串是 {web_nonce}。",
                    ),
                ],
            )
            _, text = self.answer(target)
            assert task_nonce in text and web_nonce in text, text
            return dict(thread_id=target, answer=text)

        self.record("real_task_and_web_context", references)

        def reconnect():
            target = self.thread("真实 Codex · 断线恢复")
            self.send(target, "从1数到150，每行只写一个数字。不要调用工具。")
            service = self.holder["service"]
            original_pid = service.runtime.process.pid
            original_socket = service.ws
            future = asyncio.run_coroutine_threadsafe(original_socket.close(), self.holder["loop"])
            future.result(timeout=10)
            wait_for(
                lambda: (
                    service.ws is not None and service.ws is not original_socket and self.online()
                ),
                "node reconnect",
                45,
            )
            _, text = self.answer(target)
            assert "150" in text, text[-100:]
            assert service.runtime.process.pid == original_pid
            return dict(thread_id=target, same_codex_pid=True)

        self.record("real_reconnect_without_restart", reconnect)

        def steer():
            target = self.thread("真实 Codex · 补充要求与中断")
            self.control_thread = target
            self.send(target, "从1数到1000，每行一个数字，不要调用工具。")
            wait_for(lambda: self.history(target)["thread"]["active_turn_id"], "active turn", 30)
            marker = "STEER_OK_" + secrets.token_hex(4)
            command = self.post(
                f"/api/threads/{target}/steer",
                dict(
                    text=f"现在停止数数，最后单独一行输出 {marker}，并立即结束。",
                    client_request_id=uuid4().hex,
                ),
            )["command"]
            self.command(command)
            _, text = self.answer(target)
            assert marker in text, text[-200:]
            return dict(thread_id=target, marker=marker)

        self.record("real_turn_steer", steer)

        def interrupt():
            target = self.control_thread
            self.send(target, "重新从1数到2000，每行一个数字，不要调用工具。")
            wait_for(lambda: self.history(target)["thread"]["active_turn_id"], "active turn", 30)
            self.command(self.post(f"/api/threads/{target}/interrupt", {})["command"])
            result = wait_for(
                lambda: (
                    state
                    if (state := self.history(target)["thread"])["status"] == "interrupted"
                    else None
                ),
                "native interrupted turn",
                30,
            )
            assert result["active_turn_id"] is None
            return dict(thread_id=target, final_status=result["status"])

        self.record("real_turn_interrupt", interrupt)

        def web_search():
            target = self.thread("真实 Codex · 网页搜索")
            self.send(
                target,
                "请使用内置网页搜索工具搜索 Flask 官方 quickstart 文档。"
                "只访问官方文档，回答一句中文说明并给出可点击的 Markdown 来源链接。"
                "不要运行命令，不要修改文件。",
            )
            history, text = self.answer(target)
            searches = [item for item in history["items"] if item["type"] == "webSearch"]
            assert searches, "No native webSearch event received"
            assert "https://flask.palletsprojects.com" in text, text
            return dict(thread_id=target, search_items=len(searches), answer=text)

        self.record("real_web_search", web_search)

    def run_auto_sync(self, native_id):
        """Use real local history without inference or a manual import request."""

        def discovery():
            def discovered():
                threads = self.client.get("/api/threads").json()["threads"]
                return threads if any(t["native_id"] == native_id for t in threads) else None

            threads = wait_for(discovered, "automatic recent-50 catalog", self.timeout)
            assert len(threads) <= 50
            self.auto_thread = next(t for t in threads if t["native_id"] == native_id)
            assert not self.auto_thread["history_loaded"]
            assert self.auto_thread["project_id"]
            projects = self.client.get("/api/projects").json()["projects"]
            assert projects
            with self.app.app_context():
                from codex_anywhere.hub.models import Command, Item

                assert Item.query.count() == 0 and Command.query.count() == 0
            host = self.client.get("/api/hosts").json()["hosts"][0]
            assert host["last_sync_at"] and not host["sync_error"]
            self.first_auto_sync = host["last_sync_at"]
            return dict(
                conversations=len(threads),
                projects=len(projects),
                history_items_transferred=0,
                manual_import_requests=0,
            )

        self.record("real_automatic_catalog_and_projects", discovery)

        def history():
            thread_id = self.auto_thread["id"]
            result = self.post(f"/api/threads/{thread_id}/history", {})
            self.command(result["command"])
            detail = wait_for(
                lambda: (
                    value
                    if (value := self.history(thread_id))["thread"]["history_loaded"]
                    else None
                ),
                "lazy original history projection",
                self.timeout,
            )
            assert len(detail["items"]) >= 2 and not detail["thread"]["history_stale"]
            page = self.client.get(f"/api/threads/{thread_id}?item_limit=2").json()
            assert len(page["items"]) == 2 and page["has_more_items"]
            older = self.client.get(
                f"/api/threads/{thread_id}?item_limit=2&before_position={page['next_before_position']}"
            ).json()
            assert older["items"][-1]["position"] < page["items"][0]["position"]
            current = self.post(f"/api/threads/{thread_id}/history", {})
            assert current["command"] is None
            return dict(
                thread_id=thread_id,
                item_count=len(detail["items"]),
                bounded_page_items=len(page["items"]),
                no_duplicate_history_command=True,
            )

        self.record("real_automatic_on_open_history_and_pagination", history)

        def periodic():
            def refreshed():
                host = self.client.get("/api/hosts").json()["hosts"][0]
                return host if host["last_sync_at"] != self.first_auto_sync else None

            host = wait_for(refreshed, "next automatic 60-second scan", 90)
            assert not host["sync_error"]
            with self.app.app_context():
                from codex_anywhere.hub.models import Command, Item

                assert Command.query.count() == 1
                assert len({row[0] for row in db.session.query(Item.thread_id).distinct()}) == 1
            return dict(period_seconds=60, no_other_history_downloaded=True)

        self.record("real_periodic_sync_without_bulk_history", periodic)

    def run_projects(self, native_id, expected_context=""):
        """Import an explicitly selected existing session without creating a model turn."""

        def projects():
            outside = self.directory / "another-project"
            opened = self.command(
                self.post(
                    "/api/projects",
                    dict(
                        host_id=self.host["id"],
                        cwd=str(outside),
                        name="跨目录验收项目",
                        create_directory=True,
                    ),
                )["command"]
            )["result"]["project"]
            assert outside.is_dir() and Path(opened["cwd"]) == outside
            listing = self.command(
                self.post(
                    f"/api/hosts/{self.host['id']}/directories",
                    dict(
                        path=str(self.directory),
                    ),
                )["command"]
            )["result"]
            assert any(Path(entry["path"]) == outside for entry in listing["directories"])
            return dict(project_id=opened["id"], cwd=opened["cwd"])

        self.record("real_directory_browser_and_new_project", projects)

        def catalog():
            result = self.command(
                self.post(
                    f"/api/hosts/{self.host['id']}/catalog",
                    dict(
                        limit=50,
                    ),
                )["command"]
            )["result"]
            assert isinstance(result["data"], list) and result["data"]
            return dict(count=len(result["data"]), has_next_page=bool(result.get("nextCursor")))

        self.record("real_existing_codex_catalog", catalog)

        def imported():
            result = self.post(
                f"/api/hosts/{self.host['id']}/imports", dict(native_thread_id=native_id)
            )
            self.import_thread_id = result["thread"]["id"]
            self.command(result["command"])
            history = wait_for(
                lambda: (
                    value
                    if (value := self.history(self.import_thread_id))["thread"]["history_loaded"]
                    else None
                ),
                "all imported history projected",
                self.timeout,
            )
            assert history["thread"]["native_id"] == native_id
            assert len(history["items"]) >= 2
            positions = [item["position"] for item in history["items"]]
            assert positions == sorted(positions) and len(positions) == len(set(positions))
            assert history["thread"]["project_id"]
            return dict(
                thread_id=self.import_thread_id,
                item_count=len(positions),
                original_native_id=True,
                cwd=history["thread"]["cwd"],
            )

        self.record("real_history_import", imported)

        def images():
            def ready():
                history = self.history(self.import_thread_id)
                assets = [
                    asset for item in history["items"] for asset in item["data"].get("_assets", [])
                ]
                return assets or None

            assets = wait_for(ready, "historical image uploads", 60)
            for asset in assets:
                assert self.client.get(asset["url"]).status_code == 200
            return dict(image_count=len(assets))

        self.record("real_imported_images", images)

        def duplicate():
            before = self.history(self.import_thread_id)
            result = self.post(
                f"/api/hosts/{self.host['id']}/imports", dict(native_thread_id=native_id)
            )
            assert result["thread"]["id"] == self.import_thread_id
            self.command(result["command"])
            after = wait_for(
                lambda: (
                    h
                    if (h := self.history(self.import_thread_id))["thread"]["history_loaded"]
                    else None
                ),
                "reimport projected",
                self.timeout,
            )
            assert len(after["items"]) == len(before["items"])
            return dict(same_thread=True, no_duplicate_items=True)

        self.record("real_duplicate_import", duplicate)

        def continued():
            marker = "IMPORT_CONTINUE_OK_" + secrets.token_hex(4)
            prompt = f"继续这个已有会话进行验收。不要调用工具，只回复 {marker}。"
            if expected_context:
                prompt += "并附上本会话最开始的 TASK_NONCE_ 开头完整校验串，从历史中读取。"
            self.send(self.import_thread_id, prompt)
            history, text = self.answer(self.import_thread_id)
            assert marker in text, text
            if expected_context:
                assert expected_context in text, text
            assert history["thread"]["native_id"] == native_id
            return dict(
                thread_id=self.import_thread_id,
                original_native_id=True,
                recalled_history=bool(expected_context),
                answer=text,
            )

        self.record("real_continue_imported_original_thread", continued)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5010)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--projects-only", action="store_true")
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Test automatic catalogs/lazy history without inference",
    )
    parser.add_argument("--import-native-id", help="Explicit existing test session to import")
    parser.add_argument("--expected-context", default="")
    parser.add_argument(
        "--hold", action="store_true", help="Keep the isolated hub for manual checks"
    )
    args = parser.parse_args()
    if (args.projects_only or args.sync_only) and not args.import_native_id:
        parser.error("--projects-only and --sync-only require --import-native-id")
    if args.projects_only and args.sync_only:
        parser.error("Select one acceptance mode")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    directory = Path("instance/live-tests") / (
        time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    )
    directory = directory.resolve()
    directory.mkdir(parents=True)
    system = None
    failed = False
    interrupted = False
    try:
        system = LiveSystem(directory, args.port, args.codex_bin, args.timeout)
        if args.sync_only:
            system.run_auto_sync(args.import_native_id)
        elif args.projects_only:
            system.run_projects(args.import_native_id, args.expected_context)
        else:
            system.run()
    except KeyboardInterrupt:
        interrupted = True
        failed = True
        print("Live verification interrupted", flush=True)
    except Exception as exc:
        print(f"Live verification failed: {exc}", flush=True)
        failed = True
    finally:
        if args.hold and system and not interrupted:
            print(f"Manual UI checks: {system.url} / admin / {system.password}", flush=True)
            print(f"REPORT={directory / 'report.json'}", flush=True)
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time.sleep(1)
        if system:
            system.close()
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
