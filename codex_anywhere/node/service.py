"""Node pairing, reconnecting transport, and safe command dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import platform
import random
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .assets import AssetBridge, _base64_image
from .config import (
    load_config,
    node_lock,
    resolve_path,
    save_config,
    validate_server,
    websocket_url,
)
from .directories import list_directories, open_project
from .history import import_thread, list_threads
from .runtime import CodexRuntime, RpcError, RuntimeGone
from .store import NodeStore

log = logging.getLogger(__name__)
VERSION = "0.1.0"
OUTBOX_RETRY_SECONDS = 5.0
OUTBOX_WINDOW = 100


def pair_node(
    server: str,
    name: str,
    state_dir: str | Path,
    code: str,
    allow_insecure: bool = False,
) -> dict:
    server = validate_server(server, allow_insecure)
    directory = Path(state_dir).expanduser().resolve()
    if (directory / "node.json").exists():
        raise ValueError("This state directory is already paired; use a new state directory")
    if not code.strip() or not name.strip():
        raise ValueError("Node name and pairing code are required")
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            server + "/api/hosts/pair",
            json={
                "code": code.strip(),
                "name": name.strip(),
                "platform": platform.system().lower(),
                "version": VERSION,
            },
        )
        if response.is_error:
            try:
                error = response.json().get("error", "Pairing failed")
            except (ValueError, AttributeError):
                error = "Pairing failed"
            raise ValueError(f"{error} (HTTP {response.status_code})")
        result = response.json()
    if not isinstance(result.get("token"), str) or not result.get("host", {}).get("id"):
        raise ValueError("Server returned an invalid pairing response")
    save_config(
        directory,
        {
            "server": server,
            "token": result["token"],
            "host_id": result["host"]["id"],
            "name": name.strip(),
        },
    )
    return result["host"]


class NodeService:
    def __init__(
        self,
        config: dict,
        state_dir: Path,
        codex_bin: str = "codex",
        allow_insecure: bool = False,
    ):
        self.config = {key: value for key, value in config.items() if key != "roots"}
        self.server = validate_server(config["server"], allow_insecure)
        self.codex_bin = codex_bin
        self.store = NodeStore(state_dir / "node.sqlite3")
        self.store.recover_uncertain()
        self.runtime: CodexRuntime | None = None
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.event_wake = asyncio.Event()
        self.asset_wake = asyncio.Event()
        self.command_locks: dict[str, asyncio.Lock] = {}
        self.command_tasks: set[asyncio.Task] = set()
        self.stopping = False
        self.last_pong = 0.0

    async def _send(self, message: dict) -> bool:
        async with self.send_lock:
            if self.ws is None:
                return False
            try:
                await self.ws.send(json.dumps(message, ensure_ascii=False))
                return True
            except (ConnectionClosed, OSError):
                return False

    async def _native_event(self, method: str, params: dict, rpc_id) -> None:
        assert self.runtime
        self.store.record_event(
            self.runtime.runtime_id,
            method,
            params,
            rpc_id,
            asset_job=method in {"item/completed", "anywhere/import_item"},
        )
        self.event_wake.set()
        if method in {"item/completed", "anywhere/import_item"}:
            self.asset_wake.set()

    async def _runtime_exit(self, epoch: str, returncode: int | None) -> None:
        self.store.record_event(epoch, "anywhere/runtime_stopped", {"returncode": returncode})
        self.store.recover_uncertain()
        self.event_wake.set()

    async def _start_runtime(self) -> None:
        runtime = CodexRuntime(
            [self.codex_bin, "app-server", "--listen", "stdio://"],
            self._native_event,
            self._runtime_exit,
        )
        self.runtime = runtime
        try:
            await runtime.start()
        except BaseException:
            await runtime.close()
            runtime.stopped.set()
            raise
        self.store.record_event(
            runtime.runtime_id,
            "anywhere/runtime_info",
            {
                "clientVersion": VERSION,
                "userAgent": runtime.version.get("userAgent"),
            },
        )
        self.event_wake.set()

    async def _supervise(self) -> None:
        while not self.stopping:
            assert self.runtime
            await self.runtime.stopped.wait()
            if self.stopping:
                return
            await asyncio.sleep(2)
            try:
                await self._start_runtime()
            except Exception:
                log.exception("Unable to restart Codex; retrying")
                await asyncio.sleep(8)
                continue
            # A new connection advertises the new process epoch before new commands.
            if self.ws is not None:
                await self.ws.close(code=1012, reason="Codex runtime restarted")

    async def _outbox(self) -> None:
        # This state belongs to one connection. A reconnect starts with an
        # empty window and replays every unacknowledged durable event.
        in_flight: dict[str, float] = {}
        loop = asyncio.get_running_loop()
        while True:
            self.event_wake.clear()
            pending = self.store.pending_events(limit=OUTBOX_WINDOW)
            pending_ids = {event["event_id"] for event in pending}
            in_flight = {key: sent_at for key, sent_at in in_flight.items() if key in pending_ids}
            sent = 0
            for event in pending:
                event_id = event["event_id"]
                last_sent = in_flight.get(event_id)
                if last_sent is not None and loop.time() - last_sent < OUTBOX_RETRY_SECONDS:
                    continue
                if not await self._send(event):
                    return
                in_flight[event_id] = loop.time()
                sent += 1
                if sent % 16 == 0:
                    # ws.send may complete without yielding. Let the reader
                    # persist ACKs and accept control commands between batches.
                    await asyncio.sleep(0)
            timeout = (
                max(0.001, min(in_flight.values()) + OUTBOX_RETRY_SECONDS - loop.time())
                if in_flight
                else OUTBOX_RETRY_SECONDS
            )
            try:
                await asyncio.wait_for(self.event_wake.wait(), timeout)
            except TimeoutError:
                pass

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = asyncio.get_running_loop().time()
            if now - self.last_pong > 50:
                if self.ws:
                    await self.ws.close(code=1011, reason="Hub heartbeat timed out")
                return
            if not await self._send({"type": "ping"}):
                return

    async def _connect_once(self) -> None:
        headers = {"Authorization": "Bearer " + self.config["token"]}
        options = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}
        async with connect(
            websocket_url(self.server),
            additional_headers=headers,
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            max_size=32 * 1024 * 1024,
            **options,
        ) as ws:
            assert self.runtime
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "runtime_id": self.runtime.runtime_id,
                        "version": VERSION,
                        "platform": platform.system().lower(),
                    }
                )
            )
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if welcome.get("type") != "welcome" or welcome.get("protocol") != 1:
                raise ValueError("Hub did not accept the node protocol")
            if welcome.get("host_id") != self.config["host_id"]:
                raise ValueError("Hub returned a different host identity")
            self.ws = ws
            self.last_pong = asyncio.get_running_loop().time()
            log.info("Connected to hub as %s", self.config.get("name", "node"))
            background = [
                asyncio.create_task(self._outbox()),
                asyncio.create_task(self._heartbeat()),
            ]
            try:
                # Resend journaled responses after a disconnect. Hub handles these idempotently.
                for response in self.store.completed():
                    await self._send(response)
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("type") == "command":
                        command = message.get("command")
                        if not isinstance(command, dict) or not isinstance(command.get("id"), str):
                            continue
                        task = asyncio.create_task(self.handle_command(command))
                        self.command_tasks.add(task)
                        task.add_done_callback(self._command_done)
                    elif message.get("type") == "event_ack":
                        if self.store.acknowledge(message.get("event_id", "")):
                            self.event_wake.set()
                    elif message.get("type") == "pong":
                        self.last_pong = asyncio.get_running_loop().time()
            finally:
                if self.ws is ws:
                    self.ws = None
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)

    def _command_done(self, task: asyncio.Task) -> None:
        self.command_tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Command handler failed: %s", task.exception())

    async def _network(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, TimeoutError, ValueError) as exc:
                # Exception strings from HTTP clients may contain URLs, never include tokens.
                log.warning("Hub connection unavailable (%s); reconnecting", type(exc).__name__)
            attempt = min(attempt + 1, 6)
            await asyncio.sleep(min(30, 2**attempt) + random.random())

    async def _assets(self, client: httpx.AsyncClient) -> None:
        bridge = AssetBridge(self.store, client, self.server)
        while True:
            self.asset_wake.clear()
            for event in self.store.asset_jobs():
                try:
                    assets = await bridge.process(event)
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and 400 <= exc.response.status_code < 500
                    ):
                        self.store.record_event(
                            event["runtime_id"],
                            "anywhere/asset_error",
                            {
                                "threadId": event["params"].get("threadId"),
                                "itemId": event["params"].get("item", {}).get("id"),
                                "message": "Hub rejected an output image",
                            },
                        )
                        self.store.finish_asset_job(event["event_id"])
                        self.event_wake.set()
                        continue
                    break
                except Exception:
                    log.exception("Image bridge could not process an output item")
                    self.store.finish_asset_job(event["event_id"])
                    continue
                if assets:
                    params = event["params"]
                    self.store.record_event(
                        event["runtime_id"],
                        "anywhere/assets",
                        {
                            "threadId": params["threadId"],
                            "itemId": params["item"]["id"],
                            "assets": assets,
                            **({"commandId": params["commandId"]} if "commandId" in params else {}),
                        },
                    )
                    self.event_wake.set()
                self.store.finish_asset_job(event["event_id"])
            try:
                await asyncio.wait_for(self.asset_wake.wait(), 5)
            except TimeoutError:
                pass

    async def handle_command(self, command: dict) -> dict | None:
        payload = command.get("payload", {})
        key = payload.get("native_thread_id") or command.get("thread_id") or command["id"]
        if command.get("kind") == "interaction.answer":
            key = "interaction:" + str(payload.get("rpc_id"))
        elif command.get("kind") in {"turn.interrupt", "turn.steer"}:
            # Control an active turn even while its start/resume RPC is pending.
            # Identical deliveries still share a lock and the durable journal.
            key = "control:" + command["id"]
        lock = self.command_locks.setdefault(str(key), asyncio.Lock())
        async with lock:
            try:
                claimed, response = self.store.claim(command)
            except ValueError as exc:
                response = {"type": "command_result", "id": command["id"], "error": str(exc)}
                await self._send(response)
                return response
            if not claimed:
                if response:
                    await self._send(response)
                else:
                    await self._send({"type": "command_started", "id": command["id"]})
                return response
            await self._send({"type": "command_started", "id": command["id"]})
            try:
                result = await self._execute(command)
                response = self.store.finish(command["id"], result=result)
            except RuntimeGone as exc:
                response = self.store.finish(command["id"], error=str(exc), unknown=True)
            except asyncio.CancelledError:
                self.store.finish(
                    command["id"], error="Node stopped before outcome was recorded", unknown=True
                )
                raise
            except (RpcError, ValueError, TypeError, KeyError, OSError) as exc:
                response = self.store.finish(command["id"], error=str(exc))
            except Exception:  # noqa: BLE001 - do not log payloads or exception secrets
                log.error("Unexpected command failure; execution outcome is uncertain")
                response = self.store.finish(
                    command["id"], error="Unexpected node error; inspect node logs", unknown=True
                )
            await self._send(response)
            return response

    def _input(self, values: list) -> list[dict]:
        if not isinstance(values, list) or not values:
            raise ValueError("A non-empty input array is required")
        result = []
        for value in values:
            if not isinstance(value, dict):
                raise TypeError("Invalid Codex input")
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                result.append({"type": "text", "text": value["text"]})
            elif value.get("type") == "image" and isinstance(value.get("url"), str):
                if not value["url"].startswith("data:image/"):
                    raise ValueError("Image inputs must use uploaded base64 image data")
                _base64_image(value["url"], "input.png")
                result.append({"type": "image", "url": value["url"]})
            else:
                raise ValueError("Only text and uploaded images are accepted as input")
        return result

    async def _execute(self, command: dict) -> dict:
        kind, payload = command["kind"], command.get("payload", {})
        if kind == "directory.list":
            return await asyncio.to_thread(list_directories, payload)
        if kind == "project.open":
            return await asyncio.to_thread(open_project, payload)
        runtime = self.runtime
        if not runtime or not runtime.process or runtime.process.returncode is not None:
            raise ValueError("Codex is restarting; submit a new command after it reconnects")
        if kind == "thread.start":
            cwd = str(resolve_path(payload["cwd"], directory=True))
            params = {"cwd": cwd, "approvalPolicy": "on-request", "sandbox": "workspace-write"}
            if payload.get("model"):
                params["model"] = payload["model"]
            result = await runtime.request("thread/start", params)
            native_id = result["thread"]["id"]
            self.store.save_thread(native_id, cwd)
            runtime.loaded_threads.add(native_id)
            return result
        if kind == "thread.list":
            return await list_threads(runtime, payload)
        if kind == "thread.import":

            def wake_import() -> None:
                self.event_wake.set()
                self.asset_wake.set()

            return await import_thread(runtime, self.store, command, wake_import)
        if kind == "interaction.answer":
            if payload.get("runtime_id") != runtime.runtime_id:
                raise ValueError("Interaction belongs to an expired Codex process")
            if not isinstance(payload.get("response"), dict):
                raise ValueError("Interaction response must be an object")
            return await runtime.answer(payload["rpc_id"], payload["response"])
        if kind not in {"turn.start", "turn.steer", "turn.interrupt"}:
            raise ValueError(f"Unsupported node command: {kind}")
        thread_id = payload["native_thread_id"]
        cwd = self.store.thread_cwd(thread_id)
        if cwd is None:
            raise ValueError("Thread was not created by this node")
        resolve_path(cwd, directory=True)
        if kind == "turn.start" and thread_id not in runtime.loaded_threads:
            await runtime.request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "cwd": cwd,
                    "approvalPolicy": "on-request",
                    "sandbox": "workspace-write",
                    "excludeTurns": True,
                },
            )
            runtime.loaded_threads.add(thread_id)
        if kind == "turn.interrupt":
            return await runtime.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": payload["turn_id"]}
            )
        params = {"threadId": thread_id, "input": self._input(payload["input"])}
        if kind == "turn.steer":
            if not payload.get("expected_turn_id"):
                raise ValueError("Steering requires the current turn ID")
            params["expectedTurnId"] = payload["expected_turn_id"]
        return await runtime.request("turn/start" if kind == "turn.start" else "turn/steer", params)

    async def run(self) -> None:
        background = []
        try:
            await self._start_runtime()
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + self.config["token"]},
                timeout=60,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                background = [
                    asyncio.create_task(self._supervise()),
                    asyncio.create_task(self._assets(client)),
                ]
                await self._network()
        finally:
            self.stopping = True
            if self.ws:
                await self.ws.close()
            for task in [*background, *self.command_tasks]:
                task.cancel()
            await asyncio.gather(*background, *self.command_tasks, return_exceptions=True)
            if self.runtime:
                await self.runtime.close()
            self.store.close()


def run_node(
    state_dir: str | Path,
    codex_bin: str = "codex",
    allow_insecure: bool = False,
) -> None:
    directory = Path(state_dir).expanduser().resolve()
    config = load_config(directory)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with node_lock(directory):
        service = NodeService(config, directory, codex_bin, allow_insecure)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(service.run())
