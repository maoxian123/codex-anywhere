import asyncio
import logging
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from codex_anywhere.node import service as service_module
from codex_anywhere.node.service import NodeService


@pytest.mark.parametrize(
    "durations,expected",
    [([0.1, 0.1, 0.1, 0.1], [2, 4, 8, 16]), ([0.1, 0.1, 31.0, 0.1], [2, 4, 2, 4])],
)
def test_clean_connection_flapping_backs_off_until_a_stable_session(
    tmp_path, monkeypatch, durations, expected
):
    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"}, tmp_path
        )
        clock = [0.0]
        waits = []
        connections = iter(durations)

        async def connect():
            clock[0] += next(connections)

        async def sleep(delay):
            waits.append(delay)
            if len(waits) == len(expected):
                raise asyncio.CancelledError()

        monkeypatch.setattr(service, "_connect_once", connect)
        monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(service_module.random, "random", lambda: 0)
        monkeypatch.setattr(service_module.asyncio, "sleep", sleep)
        try:
            with pytest.raises(asyncio.CancelledError):
                await service._network()
            assert waits == expected
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_clean_remote_close_has_a_diagnostic_without_exposing_credentials(tmp_path, caplog):
    import json

    async def scenario():
        service = NodeService(
            {"server": "http://127.0.0.1:9876", "token": "PRIVATE-NODE-TOKEN", "host_id": "host"},
            tmp_path,
        )
        service.runtime = SimpleNamespace(runtime_id="runtime")

        async def hub(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "welcome", "host_id": "host", "protocol": 1}))
            await ws.close(code=1000)

        try:
            async with serve(hub, "127.0.0.1", 0) as server:
                service.server = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                with caplog.at_level(logging.WARNING, logger=service_module.__name__):
                    await service._connect_once()
            assert "code=1000" in caplog.text
            assert "PRIVATE-NODE-TOKEN" not in caplog.text
        finally:
            service.store.close()

    asyncio.run(scenario())
