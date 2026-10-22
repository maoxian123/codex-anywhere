"""Fake-native tests for dynamic read-only service-tier events and races."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from codex_anywhere.node.runtime import RpcError
from codex_anywhere.node.service import NodeService

OMITTED = object()


class TierPeer:
    def __init__(self):
        self.runtime_id = "tier-runtime"
        self.process = SimpleNamespace(returncode=None)
        self.loaded_threads = {"native"}
        self.calls = []
        self.service = None
        self.event_during_turn = OMITTED
        self.event_during_resume = OMITTED
        self.result_tier = OMITTED
        self.resume_tier = OMITTED
        self.turn_error = False

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        assert "serviceTier" not in params and "service_tier" not in params
        assert "service_tier" not in params.get("config", {})
        if method == "thread/resume":
            if self.event_during_resume is not OMITTED:
                await self.service._native_event(
                    "thread/settings/updated",
                    {
                        "threadId": "native",
                        "threadSettings": {"serviceTier": self.event_during_resume},
                    },
                    None,
                )
            return {
                "thread": {"id": "native"},
                "model": "native-model",
                **({} if self.resume_tier is OMITTED else {"serviceTier": self.resume_tier}),
            }
        if method == "turn/start":
            if self.turn_error:
                raise RpcError("Native rejected turn", code=-32000)
            if self.event_during_turn is not OMITTED:
                await self.service._native_event(
                    "thread/settings/updated",
                    {
                        "threadId": "native",
                        "threadSettings": {"serviceTier": self.event_during_turn},
                    },
                    None,
                )
            return {
                "turn": {"id": "native-turn"},
                **({} if self.result_tier is OMITTED else {"serviceTier": self.result_tier}),
            }
        raise AssertionError(f"Unexpected native request: {method}")


def node(tmp_path):
    service = NodeService(
        {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"}, tmp_path
    )
    peer = TierPeer()
    peer.service = service
    service.runtime = peer
    service.execution_epoch = peer.runtime_id
    service.store.save_thread("native", str(tmp_path))
    return service, peer


def turn_command(settings=None):
    return {
        "id": "start-turn",
        "kind": "turn.start",
        "payload": {
            "native_thread_id": "native",
            "input": [{"type": "text", "text": "hello"}],
            "execution_settings": settings or {},
            "settings_revision": 4,
        },
    }


@pytest.mark.parametrize("tier", ["fast", "priority", "default", None, "custom-tier", ""])
def test_native_tier_event_updates_baseline_and_is_journaled_without_runtime_calls(tmp_path, tier):
    async def scenario():
        service, peer = node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "native-model"},
            "sandbox": {"type": "readOnly"},
        }
        params = {"threadId": "native", "threadSettings": {"serviceTier": tier, "opaque": True}}
        try:
            await service._native_event("thread/settings/updated", params, None)
            assert service.thread_execution["native"] == {
                "settings": {"model": "native-model", "service_tier": tier},
                "sandbox": {"type": "readOnly"},
            }
            recorded = service.store.pending_events()[-1]
            assert recorded["runtime_id"] == peer.runtime_id
            assert recorded["params"] == params
            assert recorded["method"] == "thread/settings/updated"
            assert service.event_wake.is_set()
            assert peer.calls == []
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", [None, "default", "priority", "custom-tier"])
def test_native_event_during_turn_response_keeps_latest_tier_in_cache_and_readback(tmp_path, tier):
    async def scenario():
        service, peer = node(tmp_path)
        service.thread_execution["native"] = {
            "settings": {"model": "native-model", "service_tier": "fast"}
        }
        peer.event_during_turn = tier
        try:
            result = await service.handle_command(turn_command())
            assert "error" not in result
            assert result["result"]["effective_settings"]["service_tier"] == tier
            assert service.thread_execution["native"]["settings"]["service_tier"] == tier
            assert [method for method, _ in peer.calls] == ["turn/start"]
            readback = service.store.pending_events()[-1]
            assert readback["method"] == "anywhere/execution_settings"
            assert readback["params"]["settings"]["service_tier"] == tier
            assert readback["params"]["settingsRevision"] == 4
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_explicit_native_response_tier_wins_while_missing_fields_never_invent_a_mode(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        peer.event_during_turn = None
        peer.result_tier = "priority"
        try:
            result = await service.handle_command(turn_command())
            assert result["result"]["effective_settings"]["service_tier"] == "priority"
            service.thread_execution.clear()
            peer.event_during_turn = OMITTED
            peer.result_tier = OMITTED
            another = turn_command()
            another["id"] = "unknown-mode-turn"
            result = await service.handle_command(another)
            assert "service_tier" not in result["result"]["effective_settings"]
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [None, {}, {"serviceTier": True}, {"serviceTier": []}])
def test_missing_or_invalid_mode_event_does_not_clear_previous_node_readback(tmp_path, native):
    async def scenario():
        service, peer = node(tmp_path)
        service.thread_execution["native"] = {"settings": {"service_tier": "fast"}}
        try:
            await service._native_event(
                "thread/settings/updated", {"threadId": "native", "threadSettings": native}, None
            )
            assert service.thread_execution["native"]["settings"]["service_tier"] == "fast"
            assert peer.calls == []
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_native_runtime_change_discards_previous_epoch_mode_before_new_command(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        service.thread_execution["native"] = {"settings": {"service_tier": "fast"}}
        peer.runtime_id = "replacement-runtime"
        try:
            result = await service.handle_command(turn_command())
            assert "service_tier" not in result["result"]["effective_settings"]
            assert service.execution_epoch == peer.runtime_id
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["fast", None])
def test_requested_service_tier_is_rejected_before_any_native_request(tmp_path, tier):
    async def scenario():
        service, peer = node(tmp_path)
        try:
            result = await service.handle_command(turn_command({"service_tier": tier}))
            assert "Unsupported execution_settings" in result["error"]
            assert not result.get("unknown")
            assert peer.calls == []
            assert service.store.pending_events() == []
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_native_mode_event_during_resume_survives_legacy_response_without_tier(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        peer.loaded_threads.clear()
        peer.event_during_resume = None
        service.thread_execution["native"] = {
            "settings": {"service_tier": "fast", "approval_policy": "never"}
        }
        try:
            result = await service.handle_command(turn_command())
            assert "error" not in result
            assert result["result"]["effective_settings"]["service_tier"] is None
            assert result["result"]["effective_settings"]["approval_policy"] is None
            assert service.thread_execution["native"]["settings"]["service_tier"] is None
            assert [method for method, _ in peer.calls] == ["thread/resume", "turn/start"]
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["fast", None, "default", "custom-tier"])
def test_resume_publishes_only_native_mode_without_turn_or_capability_queries(tmp_path, tier):
    async def scenario():
        service, peer = node(tmp_path)
        peer.resume_tier = tier
        try:
            await service._resume_thread(peer, "native")
            recorded = service.store.pending_events()
            assert len(recorded) == 1
            assert recorded[0]["runtime_id"] == peer.runtime_id
            assert recorded[0]["method"] == "anywhere/service_tier"
            assert recorded[0]["params"] == {"threadId": "native", "serviceTier": tier}
            assert service.event_wake.is_set()
            assert [method for method, _ in peer.calls] == ["thread/resume"]
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_unknown_resume_does_not_publish_a_guessed_mode(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        try:
            await service._resume_thread(peer, "native")
            assert "service_tier" not in service.thread_execution["native"]["settings"]
            assert service.store.pending_events() == []
            assert [method for method, _ in peer.calls] == ["thread/resume"]
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_rejected_turn_does_not_hide_confirmed_mode_observed_on_resume(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        peer.loaded_threads.clear()
        peer.resume_tier = None
        peer.turn_error = True
        try:
            result = await service.handle_command(turn_command())
            assert "Native rejected turn" in result["error"]
            recorded = service.store.pending_events()
            assert [row["method"] for row in recorded] == ["anywhere/service_tier"]
            assert recorded[0]["params"] == {"threadId": "native", "serviceTier": None}
            assert "settingsRevision" not in recorded[0]["params"]
            assert service.thread_execution["native"]["settings"]["service_tier"] is None
        finally:
            service.store.close()

    asyncio.run(scenario())
