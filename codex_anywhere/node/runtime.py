"""Codex App Server stdio JSON-RPC, without a terminal or shell intermediary."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class RpcError(Exception):
    """The peer returned a definitive JSON-RPC error."""


class RuntimeGone(Exception):
    """An RPC may have reached Codex, but no definitive response was received."""


class CodexRuntime:
    def __init__(
        self,
        command: list[str],
        on_event: Callable[[str, dict, object], Awaitable[None]],
        on_exit: Callable[[str, int | None], Awaitable[None]] | None = None,
        timeout: float = 180,
    ):
        self.command = command
        self.on_event = on_event
        self.on_exit = on_exit
        self.timeout = timeout
        self.runtime_id = str(uuid.uuid4())
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.server_requests: dict[str, dict] = {}
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._counter = 0
        self.loaded_threads: set[str] = set()
        self.version: dict = {}
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        command = list(self.command)
        # Windows npm exposes codex.cmd, which CreateProcess cannot execute directly.
        # Invoke its installed JS entry point via node.exe without shell interpolation.
        executable = shutil.which(command[0]) or command[0]
        if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
            from pathlib import Path

            script = (
                Path(executable).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            )
            node = shutil.which("node")
            if not node or not script.is_file():
                raise RuntimeError(
                    "Use a native Codex executable or install Codex using npm with Node.js"
                )
            command = [node, str(script), *command[1:]]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_stdout(), name="codex-stdout")
        self._stderr = asyncio.create_task(self._drain_stderr(), name="codex-stderr")
        self.version = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_anywhere",
                    "title": "Codex Anywhere",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})

    async def _write(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        async with self._write_lock:
            if not self.process or self.process.returncode is not None or not self.process.stdin:
                raise RuntimeGone("Codex process is not running")
            try:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise RuntimeGone("Codex process closed its input") from exc

    async def request(self, method: str, params: dict) -> dict:
        self._counter += 1
        rpc_id = f"anywhere-{self._counter}"
        future = asyncio.get_running_loop().create_future()
        self.pending[rpc_id] = future
        try:
            await self._write({"id": rpc_id, "method": method, "params": params})
            try:
                return await asyncio.wait_for(future, timeout=self.timeout)
            except TimeoutError as exc:
                raise RuntimeGone(
                    "Codex response timed out; execution outcome is uncertain"
                ) from exc
        finally:
            self.pending.pop(rpc_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: dict) -> None:
        await self._write({"method": method, "params": params})

    async def answer(self, rpc_id, response: dict) -> dict:
        key = json.dumps(rpc_id)
        if key not in self.server_requests:
            raise ValueError("The interaction has expired or has already been answered")
        await self._write({"id": rpc_id, "result": response})
        self.server_requests.pop(key)
        return {"answered": True}

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        failure = "Codex process exited"
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A corrupted stream cannot safely correlate in-flight commands.
                    failure = "Codex produced malformed JSON on stdout"
                    break
                if not isinstance(message, dict):
                    failure = "Codex produced a non-object JSON-RPC message"
                    break
                if "method" in message:
                    method, params = message["method"], message.get("params", {})
                    rpc_id = message.get("id")
                    if rpc_id is not None:
                        self.server_requests[json.dumps(rpc_id)] = message
                    await self.on_event(method, params, rpc_id)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            future.set_exception(RpcError(str(error.get("message", error))))
                        else:
                            future.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = "Codex event stream failed"
            log.exception(failure)
        finally:
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeGone(failure))
            self.server_requests.clear()
            self.loaded_threads.clear()
            if self.on_exit:
                await self.on_exit(self.runtime_id, self.process.returncode)
            self.stopped.set()

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        # Discard diagnostics by default: stderr can contain workspace details or credentials.
        while await self.process.stderr.read(8192):
            pass

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
        if self._reader:
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), 8)
            except TimeoutError:
                if self.process and self.process.returncode is None:
                    self.process.kill()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reader
            except asyncio.CancelledError:
                if self.process and self.process.returncode is None:
                    self.process.kill()
                raise
        if self._stderr:
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr
