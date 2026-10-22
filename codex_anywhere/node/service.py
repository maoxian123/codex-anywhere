"""Node pairing, reconnecting transport, and safe command dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import platform
import random
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .assets import AssetBridge, _base64_image
from .capabilities import (
    RuntimeCapabilities,
    effective_settings,
    requested_settings,
    thread_overrides,
    turn_overrides,
)
from .config import (
    load_config,
    node_lock,
    resolve_path,
    save_config,
    validate_server,
    websocket_url,
)
from .directories import list_directories, open_project
from .history import import_thread, list_threads, load_history_page
from .ownership import WriterOwnership
from .runtime import CodexRuntime, RpcError, RuntimeGone
from .store import NodeStore
from .sync import CatalogSynchronizer, negotiated_settings

log = logging.getLogger(__name__)
VERSION = "0.1.0"
OUTBOX_RETRY_SECONDS = 5.0
OUTBOX_WINDOW = 100
STABLE_CONNECTION_SECONDS = 30.0
PRESENCE_TTL_SECONDS = 90.0
LEGACY_IDLE_SECONDS = 300.0
THREAD_RELEASE_TIMEOUT = 8.0


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
        self.capabilities = RuntimeCapabilities()
        self.execution_epoch: str | None = None
        self.thread_execution: dict[str, dict] = {}
        self.ownership = WriterOwnership()
        self.thread_leases: dict[str, dict[str, float]] = {}
        self.lease_versions: dict[tuple[str, str], tuple[float, bool]] = {}
        self.presence_seen: set[str] = set()
        self.last_activity: dict[str, float] = {}
        self.active_turns: dict[str, str] = {}
        self.completed_turns: dict[str, set[str]] = {}
        self.pending_thread_commands: dict[str, int] = {}
        self.release_requested: set[str] = set()
        self.release_errors: dict[str, str] = {}
        self.thread_closed: dict[str, asyncio.Event] = {}
        self.unstarted_threads: set[str] = set()
        self.hub_clock: tuple[float, float] | None = None

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
        thread_id = params.get("threadId")
        thread = params.get("thread")
        if not thread_id and isinstance(thread, dict):
            thread_id = thread.get("id")
        if isinstance(thread_id, str):
            if method == "thread/settings/updated":
                native = params.get("threadSettings")
                if isinstance(native, dict) and "serviceTier" in native:
                    tier = native["serviceTier"]
                    if tier is None or isinstance(tier, str):
                        known = self.thread_execution.setdefault(thread_id, {})
                        known["settings"] = dict(known.get("settings", {}), service_tier=tier)
            turn = params.get("turn", {})
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if method == "turn/started" and isinstance(turn_id, str):
                self.active_turns[thread_id] = turn_id
                self.last_activity[thread_id] = time.time()
                self.unstarted_threads.discard(thread_id)
            elif method == "turn/completed" and isinstance(turn_id, str):
                completed = self.completed_turns.setdefault(thread_id, set())
                if len(completed) >= 100:
                    completed.clear()
                completed.add(turn_id)
                if self.active_turns.get(thread_id) == turn_id:
                    self.active_turns.pop(thread_id, None)
                self.last_activity[thread_id] = time.time()
            status = params.get("status", {})
            not_loaded = status == "notLoaded" or (
                isinstance(status, dict) and status.get("type") == "notLoaded"
            )
            if method == "thread/closed" or (method == "thread/status/changed" and not_loaded):
                self.runtime.loaded_threads.discard(thread_id)
                self.thread_execution.pop(thread_id, None)
                self.active_turns.pop(thread_id, None)
                self.release_errors.pop(thread_id, None)
                self.release_requested.discard(thread_id)
                self.unstarted_threads.discard(thread_id)
                self.thread_closed.setdefault(thread_id, asyncio.Event()).set()
        self.store.record_event(
            self.runtime.runtime_id,
            method,
            params,
            rpc_id,
            asset_job=method in {"item/completed", "anywhere/import_item", "anywhere/history_item"},
        )
        self.event_wake.set()
        if method in {"item/completed", "anywhere/import_item", "anywhere/history_item"}:
            self.asset_wake.set()
        if method in {
            "config/updated",
            "config/changed",
            "account/updated",
            "account/login/completed",
        }:
            self.capabilities.invalidate()

    def _leases(self, thread_id: str, now: float | None = None) -> dict[str, float]:
        now = time.time() if now is None else now
        leases = self.thread_leases.setdefault(thread_id, {})
        for client_id, expires_at in list(leases.items()):
            if expires_at <= now:
                leases.pop(client_id, None)
        return leases

    def _sync_hub_clock(self, welcome: dict) -> None:
        server_time = welcome.get("server_time")
        self.hub_clock = (
            (float(server_time), time.monotonic())
            if type(server_time) in {int, float} and math.isfinite(server_time)
            else None
        )

    def _hub_now(self) -> float:
        if self.hub_clock is None:
            return time.time()
        server_time, received_at = self.hub_clock
        return server_time + time.monotonic() - received_at

    def _presence(
        self, thread_id: str, payload: dict, *, release: bool = False, takeover: bool = False
    ) -> bool:
        client_id, issued_at = payload.get("client_id"), payload.get("issued_at")
        if not isinstance(client_id, str) or not client_id or len(client_id) > 200:
            raise ValueError("A bounded client_id is required")
        if type(issued_at) not in {int, float} or not math.isfinite(issued_at):
            raise ValueError("A finite issued_at is required")
        expires_at = payload.get("expires_at", 0)
        if type(expires_at) not in {int, float} or not math.isfinite(expires_at):
            raise ValueError("A finite expires_at is required")
        release = release or expires_at <= 0
        now = time.time()
        remaining = expires_at - self._hub_now()
        if takeover and (release or remaining <= 0):
            raise ValueError("Takeover presence has expired; reopen the conversation and try again")
        key = (thread_id, client_id)
        version = (issued_at, release)
        previous = self.lease_versions.get(key, (-math.inf, False))
        if version <= previous:
            # A presence and takeover may share the browser's millisecond clock
            # value. Reuse only that still-live lease; the same-time release has
            # a higher version and always wins. Never revive an expired lease.
            if takeover and version == previous and self._leases(thread_id, now).get(client_id, 0) > now:
                return True
            return False
        self.lease_versions[key] = version
        self.presence_seen.add(thread_id)
        leases = self._leases(thread_id)
        if release or remaining <= 0:
            leases.pop(client_id, None)
        else:
            leases[client_id] = now + min(remaining, PRESENCE_TTL_SECONDS)
            self.release_requested.discard(thread_id)
        return True

    def _require_takeover_presence(self, thread_id: str, payload: dict) -> None:
        # Legacy direct callers have no browser client. The hub always supplies
        # its bounded lease, which must remain valid before each acquisition or
        # external-process action, rather than being applied after side effects.
        if "client_id" in payload and not self._presence(thread_id, payload, takeover=True):
            raise ValueError("Takeover presence is stale; reopen the conversation and try again")

    def _thread_busy(self, thread_id: str) -> bool:
        if thread_id in self.active_turns or self.pending_thread_commands.get(thread_id, 0):
            return True
        runtime = self.runtime
        return bool(
            runtime
            and any(
                message.get("params", {}).get("threadId") == thread_id
                for message in runtime.server_requests.values()
            )
        )

    def _release_due(self, thread_id: str, now: float) -> bool:
        if self._leases(thread_id, now):
            return False
        return (
            thread_id in self.release_requested
            or thread_id in self.presence_seen
            or now - self.last_activity.setdefault(thread_id, now) >= LEGACY_IDLE_SECONDS
        )

    async def _publish_ownership(self, thread_id: str, **extra) -> dict:
        runtime = self.runtime
        managed = bool(runtime and thread_id in runtime.loaded_threads)
        ownership = await asyncio.to_thread(self.ownership.inspect, thread_id, managed=managed)
        ownership.update(extra)
        if runtime:
            self.store.record_event(
                runtime.runtime_id,
                "anywhere/ownership",
                {"threadId": thread_id, "ownership": ownership},
            )
            self.event_wake.set()
        return ownership

    async def _release_thread(self, thread_id: str, *, explicit: bool = False) -> dict:
        runtime = self.runtime
        if self._leases(thread_id):
            return {
                "released": False,
                "retained": True,
                "deferred": True,
                "reason": "Another browser tab still holds this conversation",
            }
        if not runtime or thread_id not in runtime.loaded_threads:
            self.release_requested.discard(thread_id)
            return {"released": True}
        self.release_requested.add(thread_id)
        if thread_id in self.unstarted_threads:
            return {
                "released": False,
                "deferred": True,
                "reason": "Codex has not persisted this empty conversation; release follows its first turn",
            }
        if self._thread_busy(thread_id):
            return {"released": False, "deferred": True}
        if thread_id in self.release_errors and not explicit:
            return {"released": False, "error": self.release_errors[thread_id]}
        closed = self.thread_closed.setdefault(thread_id, asyncio.Event())
        closed.clear()
        try:
            async with asyncio.timeout(THREAD_RELEASE_TIMEOUT):
                await runtime.request("thread/unsubscribe", {"threadId": thread_id})
                await closed.wait()
        except (RpcError, RuntimeGone, TimeoutError) as exc:
            error = (
                "Codex did not confirm thread release; upgrade Codex if thread/unsubscribe "
                "or thread/closed is unsupported"
                if isinstance(exc, (TimeoutError, RuntimeGone))
                else str(exc)
            )
            self.release_errors[thread_id] = error
            await self._publish_ownership(thread_id, release_error=error)
            return {"released": False, "error": error}
        self.release_errors.pop(thread_id, None)
        self.release_requested.discard(thread_id)
        await self._publish_ownership(thread_id)
        return {"released": True}

    async def _sweep_threads_once(self) -> None:
        runtime = self.runtime
        if not runtime:
            return
        for thread_id in list(runtime.loaded_threads):
            lock = self.command_locks.setdefault(thread_id, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                if self.runtime is runtime and self._release_due(thread_id, time.time()):
                    await self._release_thread(thread_id)

    async def _sweep_threads(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._sweep_threads_once()
            except Exception:
                log.exception("Unable to release an idle Codex thread")

    async def _runtime_exit(self, epoch: str, returncode: int | None) -> None:
        self.store.record_event(epoch, "anywhere/runtime_stopped", {"returncode": returncode})
        self.store.recover_uncertain()
        self.event_wake.set()
        self.active_turns.clear()
        self.thread_execution.clear()
        self.release_errors.clear()
        self.unstarted_threads.clear()

    async def _start_runtime(self) -> None:
        runtime = CodexRuntime(
            [
                self.codex_bin,
                "-c",
                "thread_unload_delay_secs=0",
                "app-server",
                "--listen",
                "stdio://",
            ],
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
                        "capabilities": [
                            "catalogSync",
                            "lazyHistory",
                            "runtimeSettings",
                            "projectInventory",
                            "threadOwnership",
                        ],
                    }
                )
            )
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if welcome.get("type") != "welcome" or welcome.get("protocol") != 1:
                raise ValueError("Hub did not accept the node protocol")
            if welcome.get("host_id") != self.config["host_id"]:
                raise ValueError("Hub returned a different host identity")
            self._sync_hub_clock(welcome)
            self.ws = ws
            self.last_pong = asyncio.get_running_loop().time()
            log.info("Connected to hub as %s", self.config.get("name", "node"))
            background = [
                asyncio.create_task(self._outbox()),
                asyncio.create_task(self._heartbeat()),
            ]
            settings = negotiated_settings(welcome)
            if settings is not None:
                runtime = self.runtime
                synchronizer = CatalogSynchronizer(
                    runtime,
                    self.store,
                    self.event_wake.set,
                    settings,
                    is_current=lambda: self.ws is ws and self.runtime is runtime,
                    foreground_busy=lambda: any(not task.done() for task in self.command_tasks),
                )
                background.append(
                    asyncio.create_task(synchronizer.run(), name="codex-catalog-sync")
                )
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
                log.warning("Hub closed the node connection (code=%s); reconnecting", ws.close_code)
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
            started = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, TimeoutError, ValueError) as exc:
                # Exception strings from HTTP clients may contain URLs, never include tokens.
                log.warning("Hub connection unavailable (%s); reconnecting", type(exc).__name__)
            if time.monotonic() - started >= STABLE_CONNECTION_SECONDS:
                attempt = 0
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
        thread_id = payload.get("native_thread_id")
        kind = command.get("kind")
        if kind == "interaction.answer" and self.runtime:
            request = self.runtime.server_requests.get(json.dumps(payload.get("rpc_id")), {})
            thread_id = request.get("params", {}).get("threadId")
        guarded = bool(
            isinstance(thread_id, str)
            and kind in {"turn.start", "turn.steer", "turn.interrupt", "interaction.answer"}
        )
        if guarded:
            self.pending_thread_commands[thread_id] = (
                self.pending_thread_commands.get(thread_id, 0) + 1
            )
            self.last_activity[thread_id] = time.time()
        try:
            return await self._handle_command(command)
        finally:
            if guarded:
                count = self.pending_thread_commands.get(thread_id, 1) - 1
                if count:
                    self.pending_thread_commands[thread_id] = count
                else:
                    self.pending_thread_commands.pop(thread_id, None)

    async def _handle_command(self, command: dict) -> dict | None:
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
        if self.execution_epoch != runtime.runtime_id:
            self.execution_epoch = runtime.runtime_id
            self.thread_execution.clear()
            self.capabilities.invalidate()
        if kind == "runtime.capabilities":
            cwd = payload.get("cwd")
            if cwd is not None:
                cwd = str(resolve_path(cwd, directory=True))
            refresh = payload.get("refresh", False)
            if type(refresh) is not bool:
                raise ValueError("refresh must be a boolean")
            return await self.capabilities.read(runtime, cwd, refresh=refresh)
        if kind in {"thread.ownership", "thread.takeover", "thread.presence", "thread.release"}:
            thread_id = payload.get("native_thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("native_thread_id is required")
            if kind == "thread.ownership":
                return await asyncio.to_thread(
                    self.ownership.inspect, thread_id, managed=thread_id in runtime.loaded_threads
                )
            if kind in {"thread.presence", "thread.release"}:
                changed = self._presence(thread_id, payload, release=kind == "thread.release")
                if kind == "thread.presence":
                    return {"accepted": changed, "clients": len(self._leases(thread_id))}
                if not changed:
                    return {"released": False, "stale": True}
                return await self._release_thread(thread_id, explicit=True)
            self._require_takeover_presence(thread_id, payload)
            ownership = await asyncio.to_thread(
                self.ownership.inspect, thread_id, managed=thread_id in runtime.loaded_threads
            )
            state = ownership.get("state")
            if state == "external":
                if payload.get("confirmed") is not True or not payload.get("owner_token"):
                    raise ValueError("Confirm the external writer and supply its current owner_token")
                self._require_takeover_presence(thread_id, payload)
                try:
                    await asyncio.to_thread(
                        self.ownership.terminate, thread_id, payload["owner_token"]
                    )
                except (RuntimeError, TimeoutError) as exc:
                    raise ValueError(str(exc)) from exc
                ownership = await asyncio.to_thread(self.ownership.inspect, thread_id, managed=False)
                state = ownership.get("state")
            if state not in {"managed", "available"}:
                raise ValueError(ownership.get("reason") or "Cannot safely acquire the thread writer")
            if thread_id not in runtime.loaded_threads:
                self._require_takeover_presence(thread_id, payload)
                await self._resume_thread(runtime, thread_id)
            self.last_activity[thread_id] = time.time()
            return {"ownership": await self._publish_ownership(thread_id)}
        if kind == "thread.start":
            cwd = str(resolve_path(payload["cwd"], directory=True))
            settings, revision = requested_settings(payload)
            await self.capabilities.validate(runtime, cwd, settings)
            params = {"cwd": cwd, **thread_overrides(settings, new=True)}
            result = await runtime.request("thread/start", params)
            native_id = result["thread"]["id"]
            self.store.save_thread(native_id, cwd)
            runtime.loaded_threads.add(native_id)
            self.last_activity[native_id] = time.time()
            self.unstarted_threads.add(native_id)
            # Codex creates an empty thread's rollout lazily. Setting the user's
            # requested name persists it without inference, so unloading it does
            # not destroy the only resumable copy of the new conversation.
            title = payload.get("title")
            if isinstance(title, str) and title.strip():
                try:
                    await runtime.request(
                        "thread/name/set", {"threadId": native_id, "name": title.strip()}
                    )
                    path = result.get("thread", {}).get("path")
                    if isinstance(path, str) and Path(path).is_file():
                        self.unstarted_threads.discard(native_id)
                except (RpcError, RuntimeGone, OSError):
                    # Preserve the successful creation response and native ID.
                    # Older peers can persist the thread on its first real turn.
                    log.warning("Native conversation naming was unavailable; keeping empty thread loaded")
            fallback = {
                "approval_policy": params["approvalPolicy"],
                "sandbox_mode": params["sandbox"],
                **settings,
            }
            return self._execution_result(runtime, command, native_id, result, revision, fallback)
        if kind == "thread.list":
            return await list_threads(runtime, payload)
        if kind in {"thread.import", "thread.history"}:

            def wake_import() -> None:
                self.event_wake.set()
                self.asset_wake.set()

            handler = import_thread if kind == "thread.import" else load_history_page
            return await handler(runtime, self.store, command, wake_import)
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
        if kind == "turn.interrupt":
            return await runtime.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": payload["turn_id"]}
            )
        params = {"threadId": thread_id, "input": self._input(payload["input"])}
        if kind == "turn.steer":
            if not payload.get("expected_turn_id"):
                raise ValueError("Steering requires the current turn ID")
            params["expectedTurnId"] = payload["expected_turn_id"]
            return await runtime.request("turn/steer", params)
        settings, revision = requested_settings(payload)
        known = self.thread_execution.get(thread_id, {})
        if kind == "turn.start" and (
            thread_id not in runtime.loaded_threads or thread_id in self.release_errors
        ):
            # Resume original native defaults first. Validation then uses the actual
            # imported model/sandbox rather than the global new-thread defaults.
            await self._resume_thread(runtime, thread_id)
            known = self.thread_execution[thread_id]
        baseline = known.get("settings", {})
        await self.capabilities.validate(runtime, cwd, settings, baseline)
        overrides = turn_overrides(settings, baseline, known.get("sandbox"))
        marker = "pending:" + command["id"]
        self.active_turns.setdefault(thread_id, marker)
        try:
            result = await runtime.request("turn/start", {**params, **overrides})
        except RpcError:
            if self.active_turns.get(thread_id) == marker:
                self.active_turns.pop(thread_id, None)
            raise
        turn = result.get("turn", {})
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str):
            self.unstarted_threads.discard(thread_id)
            if turn_id in self.completed_turns.get(thread_id, set()) or turn.get("status") in {
                "completed", "failed", "interrupted"
            }:
                if self.active_turns.get(thread_id) in {marker, turn_id}:
                    self.active_turns.pop(thread_id, None)
            elif self.active_turns.get(thread_id) in {marker, turn_id}:
                self.active_turns[thread_id] = turn_id
        # A successful turn RPC accepts overrides for this and subsequent turns.
        applied = {**baseline, **settings}
        if "sandboxPolicy" in overrides:
            result_for_settings = {"sandbox": overrides["sandboxPolicy"], **result}
        else:
            result_for_settings = result
        return self._execution_result(
            runtime,
            command,
            thread_id,
            result_for_settings,
            revision,
            applied,
            raw_result=result,
        )

    async def _resume_thread(self, runtime: CodexRuntime, thread_id: str) -> dict:
        cwd = self.store.thread_cwd(thread_id)
        if cwd is None:
            metadata = await runtime.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            cwd = metadata.get("thread", {}).get("cwd")
        cwd = str(resolve_path(cwd, directory=True))
        resumed = await runtime.request(
            "thread/resume", {"threadId": thread_id, "cwd": cwd, "excludeTurns": True}
        )
        self.store.save_thread(thread_id, cwd)
        runtime.loaded_threads.add(thread_id)
        self.thread_closed.setdefault(thread_id, asyncio.Event()).clear()
        known = self.thread_execution.get(thread_id, {}).get("settings", {})
        mode = {"service_tier": known["service_tier"]} if "service_tier" in known else {}
        settings = effective_settings(resumed, mode)
        self.thread_execution[thread_id] = {
            "settings": settings,
            "sandbox": resumed.get("sandbox"),
        }
        if "service_tier" in settings:
            self.store.record_event(
                runtime.runtime_id,
                "anywhere/service_tier",
                {"threadId": thread_id, "serviceTier": settings["service_tier"]},
            )
            self.event_wake.set()
        self.last_activity[thread_id] = time.time()
        self.release_errors.pop(thread_id, None)
        return resumed

    def _execution_result(
        self,
        runtime,
        command: dict,
        thread_id: str,
        result: dict,
        revision: int,
        baseline: dict | None = None,
        *,
        raw_result: dict | None = None,
    ) -> dict:
        old = self.thread_execution.get(thread_id, {})
        baseline = dict(baseline or {})
        if "service_tier" in old.get("settings", {}):
            baseline["service_tier"] = old["settings"]["service_tier"]
        settings = effective_settings(result, baseline)
        self.thread_execution[thread_id] = {
            "settings": settings,
            "sandbox": result.get("sandbox", old.get("sandbox")),
        }
        self.store.record_event(
            runtime.runtime_id,
            "anywhere/execution_settings",
            {
                "threadId": thread_id,
                "commandId": command["id"],
                "settings": settings,
                "settingsRevision": revision,
            },
        )
        self.event_wake.set()
        return {
            **(raw_result if raw_result is not None else result),
            "effective_settings": settings,
            "settings_revision": revision,
        }

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
                    asyncio.create_task(self._sweep_threads()),
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
