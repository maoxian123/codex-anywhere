"""Speed changes use fake native RPC acknowledgements, never live inference."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from codex_anywhere.node.runtime import RpcError
from codex_anywhere.node.service import NodeService

MISSING = object()


class SpeedPeer:
    def __init__(self, service):
        self.service = service
        self.runtime_id = "speed-regression-runtime"
        self.process = SimpleNamespace(returncode=None)
        self.loaded_threads = {"native"}
        self.calls = []
        self.event_tier = MISSING
        self.event_thread_id = "native"
        self.response_tier = MISSING
        self.rejected = False
        self.turn_count = 0

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "config/read":
            return {"config": {"model": "test-model", "service_tier": "default"}}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "test-model",
                        "model": "test-model",
                        "isDefault": True,
                        "serviceTiers": [{"id": "priority", "name": "Fast"}],
                    }
                ],
                "nextCursor": None,
            }
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "native"}, "model": "test-model"}
        if method == "turn/start":
            self.turn_count += 1
            if self.event_tier is not MISSING:
                await self.service._native_event(
                    "thread/settings/updated",
                    {
                        "threadId": self.event_thread_id,
                        "threadSettings": {"serviceTier": self.event_tier},
                    },
                    None,
                )
            if self.rejected:
                raise RpcError("Rejected speed change", code=-32000)
            return {
                "turn": {"id": f"turn-{self.turn_count}", "status": "completed"},
                **({} if self.response_tier is MISSING else {"serviceTier": self.response_tier}),
            }
        raise AssertionError(f"Unexpected RPC: {method}")


def make_node(tmp_path):
    service = NodeService(
        {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"},
        tmp_path,
    )
    peer = SpeedPeer(service)
    service.runtime = peer
    service.execution_epoch = peer.runtime_id
    service.store.save_thread("native", str(tmp_path))
    return service, peer


def command(identifier, tier=MISSING):
    return {
        "id": identifier,
        "kind": "turn.start",
        "payload": {
            "native_thread_id": "native",
            "input": [{"type": "text", "text": "Fake input"}],
            "execution_settings": {} if tier is MISSING else {"service_tier": tier},
            "settings_revision": 4,
        },
    }


def test_speed_changes_reach_native_rpc_and_inherit_does_not_reset_the_last_choice(tmp_path):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "default"}
        }
        try:
            fast = await service.handle_command(command("fast", "fast"))
            assert "error" not in fast
            assert fast["result"]["effective_settings"]["service_tier"] == "priority"
            normal = await service.handle_command(command("normal", "default"))
            assert "error" not in normal
            assert normal["result"]["effective_settings"]["service_tier"] == "default"
            inherited = await service.handle_command(command("inherit", None))
            assert "error" not in inherited
            assert inherited["result"]["effective_settings"]["service_tier"] == "default"
            calls = [params for method, params in peer.calls if method == "turn/start"]
            assert [params.get("serviceTier", MISSING) for params in calls] == [
                "priority", "default", MISSING
            ]
            assert all("service_tier" not in params for params in calls)
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_another_threads_mode_event_does_not_override_the_explicit_current_thread_choice(tmp_path):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "default"}
        }
        peer.event_tier = "default"
        peer.event_thread_id = "another-thread"
        try:
            result = await service.handle_command(command("current-fast", "fast"))
            assert "error" not in result
            assert result["result"]["effective_settings"]["service_tier"] == "priority"
            assert service.thread_execution["another-thread"]["settings"]["service_tier"] == "default"
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_runtime_replacement_drops_a_previous_epochs_confirmed_speed(tmp_path):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "priority"}
        }
        peer.runtime_id = "replacement-speed-runtime"
        try:
            result = await service.handle_command(command("replacement-unknown"))
            assert "error" not in result
            assert "service_tier" not in result["result"]["effective_settings"]
            assert "service_tier" not in service.thread_execution["native"]["settings"]
            assert service.execution_epoch == peer.runtime_id
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["default", "priority", None, "future-native-tier"])
def test_native_event_during_explicit_change_wins_even_when_equal_to_previous_value(tmp_path, tier):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "default"}
        }
        peer.event_tier = tier
        try:
            result = await service.handle_command(command("fast", "fast"))
            assert "error" not in result
            assert result["result"]["effective_settings"]["service_tier"] == tier
            assert service.thread_execution["native"]["settings"]["service_tier"] == tier
            assert service.store.pending_events()[-1]["params"]["settings"]["service_tier"] == tier
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_explicit_rpc_mode_readback_wins_over_an_event_received_before_the_response(tmp_path):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "default"}
        }
        peer.event_tier = "default"
        peer.response_tier = "priority"
        try:
            result = await service.handle_command(command("fast", "fast"))
            assert result["result"]["effective_settings"]["service_tier"] == "priority"
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("event_tier", [MISSING, "default"])
def test_rejected_change_never_publishes_requested_mode_or_retries_the_command(tmp_path, event_tier):
    async def scenario():
        service, peer = make_node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "test-model", "service_tier": "priority"}
        }
        peer.event_tier = event_tier
        peer.rejected = True
        request = command("rejected-normal", "default")
        try:
            result = await service.handle_command(request)
            assert "Rejected speed change" in result["error"]
            assert not result.get("unknown")
            expected = "priority" if event_tier is MISSING else event_tier
            assert service.thread_execution["native"]["settings"]["service_tier"] == expected
            events = service.store.pending_events()
            assert not any(row["method"] == "anywhere/execution_settings" for row in events)
            calls = len(peer.calls)
            assert await service.handle_command(request) == result
            assert len(peer.calls) == calls
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", [MISSING, None])
def test_new_thread_without_mode_readback_keeps_unknown_despite_node_default(tmp_path, tier):
    async def scenario():
        service, peer = make_node(tmp_path)
        request = {
            "id": "new-thread",
            "kind": "thread.start",
            "payload": {
                "cwd": str(tmp_path),
                "execution_settings": {} if tier is MISSING else {"service_tier": tier},
            },
        }
        try:
            result = await service.handle_command(request)
            assert "error" not in result
            assert "service_tier" not in result["result"]["effective_settings"]
            params = next(params for method, params in peer.calls if method == "thread/start")
            assert "serviceTier" not in params
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", [MISSING, None])
def test_resumed_thread_without_mode_readback_never_inherits_config_as_confirmed_mode(tmp_path, tier):
    async def scenario():
        service, peer = make_node(tmp_path)
        peer.loaded_threads.clear()
        try:
            result = await service.handle_command(command("resume-unknown", tier))
            assert "error" not in result
            assert "service_tier" not in result["result"]["effective_settings"]
            assert "service_tier" not in service.thread_execution["native"]["settings"]
            native = [params for method, params in peer.calls if method in {"thread/resume", "turn/start"}]
            assert len(native) == 2 and all("serviceTier" not in params for params in native)
            assert not any(row["method"] == "anywhere/service_tier" for row in service.store.pending_events())
        finally:
            service.store.close()

    asyncio.run(scenario())
