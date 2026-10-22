from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from codex_anywhere.node.capabilities import (
    RuntimeCapabilities,
    effective_settings,
    requested_settings,
    thread_overrides,
    turn_overrides,
)
from codex_anywhere.node.runtime import RpcError
from codex_anywhere.node.service import NodeService


class Peer:
    def __init__(self):
        self.runtime_id = "settings-runtime"
        self.process = SimpleNamespace(returncode=None)
        self.loaded_threads = set()
        self.calls = []
        self.config = {
            "model": "catalog-model",
            "model_reasoning_effort": "future-effort",
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
            "approvals_reviewer": "user",
            "sandbox_workspace_write": {"network_access": False},
            "developer_instructions": "SECRET-INSTRUCTIONS",
            "model_providers": {"example": {"http_headers": {"Authorization": "SECRET-KEY"}}},
        }
        self.requirements = None
        self.models = [
            {
                "id": "catalog-id",
                "model": "catalog-model",
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "future-effort", "opaque": 4}],
                "defaultReasoningEffort": "future-effort",
                "unknownModelField": {"ok": True},
            },
            {
                "id": "other-id",
                "model": "other-model",
                "isDefault": False,
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                "defaultReasoningEffort": "low",
            },
        ]
        self.resume_result = {
            "thread": {"id": "imported"},
            "model": "custom-imported-model",
            "reasoningEffort": "custom-effort",
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": {
                "type": "workspaceWrite",
                "networkAccess": False,
                "writableRoots": ["native-extra-root"],
                "readOnlyAccess": {"type": "restricted", "readableRoots": ["native-read-root"]},
            },
        }

    async def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "config/read":
            return {"config": copy.deepcopy(self.config), "origins": {"SECRET-ORIGIN": {}}}
        if method == "configRequirements/read":
            return {"requirements": copy.deepcopy(self.requirements)}
        if method == "model/list":
            offset = int(params.get("cursor", "0"))
            return {
                "data": copy.deepcopy(self.models[offset : offset + 1]),
                "nextCursor": str(offset + 1) if offset + 1 < len(self.models) else None,
            }
        if method == "thread/start":
            return {
                "thread": {"id": "new-native", "cwd": params["cwd"]},
                "model": "catalog-model",
                "reasoningEffort": "future-effort",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "networkAccess": False},
            }
        if method == "thread/resume":
            return copy.deepcopy(self.resume_result)
        if method in {"turn/start", "turn/steer"}:
            return {"turn": {"id": "turn-native"}}
        return {}


def node(tmp_path):
    service = NodeService(
        {"server": "http://127.0.0.1:9876", "token": "test", "host_id": "host"}, tmp_path
    )
    peer = Peer()
    service.runtime = peer
    return service, peer


def test_catalog_paginates_preserves_unknowns_and_never_exposes_config_secrets():
    async def scenario():
        peer = Peer()
        capabilities = await RuntimeCapabilities().read(peer, "project")
        assert capabilities["models"] == peer.models
        assert (
            capabilities["models"][0]["supportedReasoningEfforts"][0]["reasoningEffort"]
            == "future-effort"
        )
        assert capabilities["defaults"]["model"] == "catalog-model"
        assert capabilities["constraints"]["approvals_reviewers"] == ["user"]
        assert capabilities["supported"]["auto_review"] is False
        assert "SECRET" not in str(capabilities)
        assert peer.calls[0] == ("config/read", {"includeLayers": False, "cwd": "project"})
        assert [
            params.get("cursor") for method, params in peer.calls if method == "model/list"
        ] == [None, "1"]

    asyncio.run(scenario())


def test_capabilities_cache_refresh_config_cwd_and_epoch_awareness():
    async def scenario():
        peer, reader = Peer(), RuntimeCapabilities()

        def count():
            return sum(method == "model/list" for method, _ in peer.calls)

        await reader.read(peer, "first")
        await reader.read(peer, "first")
        assert count() == 2
        assert sum(method == "config/read" for method, _ in peer.calls) == 2
        await reader.read(peer, "first", refresh=True)
        assert count() == 4
        peer.config["sandbox_mode"] = "read-only"
        await reader.read(peer, "first")
        assert count() == 6
        await reader.read(peer, "second")
        assert count() == 8
        peer.runtime_id = "restarted"
        assert (await reader.read(peer, "second"))["runtime_id"] == "restarted"
        assert count() == 10
        peer.config["model_provider"] = "another-provider"
        await reader.read(peer, "second")
        assert count() == 12

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"model": "invented"}, "model"),
        ({"effort": "invented"}, "reasoning effort"),
        ({"approval_policy": "never"}, "requirements"),
        ({"sandbox_mode": "danger-full-access"}, "requirements"),
        ({"approvals_reviewer": "auto_review"}, "requirements"),
    ],
)
def test_validation_enforces_catalog_and_managed_settings(settings, message):
    async def scenario():
        peer = Peer()
        peer.requirements = {
            "allowedApprovalPolicies": ["on-request"],
            "allowedSandboxModes": ["workspace-write"],
            "chatgptBaseUrl": "SECRET-POLICY",
        }
        reader = RuntimeCapabilities()
        with pytest.raises(ValueError, match=message):
            await reader.validate(peer, "cwd", settings)
        assert not any(method in {"thread/start", "turn/start"} for method, _ in peer.calls)
        await reader.validate(peer, "cwd", {"model": "catalog-model", "effort": "future-effort"})
        await reader.validate(
            peer,
            "cwd",
            {"model": "custom", "effort": "custom-effort"},
            {"model": "custom", "effort": "custom-effort"},
        )

    asyncio.run(scenario())


def test_auto_review_requires_native_signal_and_honors_managed_restriction():
    async def scenario():
        peer, reader = Peer(), RuntimeCapabilities()
        peer.config["approvals_reviewer"] = "auto_review"
        result = await reader.read(peer)
        assert result["supported"]["auto_review"] is True
        assert result["constraints"]["approvals_reviewers"] == ["user", "auto_review"]
        peer.requirements = {"allowedApprovalsReviewers": ["user"]}
        assert (await reader.read(peer))["supported"]["auto_review"] is False
        peer.requirements = {"autoReview": {"requiredOnModels": ["catalog-model"]}}
        await reader.validate(peer, "cwd", {"model": "catalog-model"})
        with pytest.raises(ValueError, match="require automatic"):
            await reader.validate(peer, "cwd", {"approvals_reviewer": "user"})

    asyncio.run(scenario())


def test_sandbox_translation_preserves_native_permissions_and_truthful_network():
    baseline = {"sandbox_mode": "workspace-write", "network_access": False}
    native = {
        "type": "workspaceWrite",
        "writableRoots": ["extra"],
        "readOnlyAccess": {"type": "restricted", "readableRoots": ["one"]},
    }
    original = copy.deepcopy(native)
    translated = turn_overrides({"network_access": True}, baseline, native)
    assert translated["sandboxPolicy"] == {**native, "networkAccess": True}
    assert native == original
    with pytest.raises(ValueError, match="cannot disable"):
        turn_overrides(
            {"sandbox_mode": "danger-full-access", "network_access": False}, baseline, native
        )
    assert turn_overrides(
        {"sandbox_mode": "read-only", "network_access": True}, baseline, native
    ) == {"sandboxPolicy": {"type": "readOnly", "networkAccess": True}}
    assert turn_overrides(
        {"sandbox_mode": "workspace-write"},
        {"sandbox_mode": "danger-full-access", "network_access": True},
        {"type": "dangerFullAccess"},
    ) == {"sandboxPolicy": {"type": "workspaceWrite"}}
    assert effective_settings({"sandbox": {"type": "dangerFullAccess"}})["network_access"] is True
    assert thread_overrides({"effort": "future-effort", "network_access": True}, new=True) == {
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
        "config": {
            "model_reasoning_effort": "future-effort",
            "sandbox_workspace_write.network_access": True,
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"execution_settings": {"network_access": 1}},
        {"execution_settings": {"model": "  x"}},
        {"execution_settings": {"unknown": True}},
        {"execution_settings": []},
        {"settings_revision": True},
        {"settings_revision": -1},
    ],
)
def test_invalid_settings_shape_rejected(payload):
    with pytest.raises(ValueError):
        requested_settings(payload)


def test_new_thread_reports_actual_native_clamped_settings_and_revision(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        try:
            result = await service.handle_command(
                {
                    "id": "start",
                    "kind": "thread.start",
                    "payload": {
                        "cwd": str(tmp_path),
                        "execution_settings": {"model": "other-model", "effort": "low"},
                        "settings_revision": 7,
                    },
                }
            )
            assert "error" not in result
            assert result["result"]["effective_settings"]["model"] == "catalog-model"
            assert result["result"]["settings_revision"] == 7
            params = next(params for method, params in peer.calls if method == "thread/start")
            assert params["model"] == "other-model"
            assert params["config"] == {"model_reasoning_effort": "low"}
            event = service.store.pending_events()[-1]
            assert event["method"] == "anywhere/execution_settings"
            assert event["params"]["commandId"] == "start"
            assert event["params"]["settingsRevision"] == 7
            assert event["params"]["settings"]["model"] == "catalog-model"
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_imported_resume_inherits_then_explicit_turn_preserves_native_roots(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        service.store.save_thread("imported", str(tmp_path))
        try:
            result = await service.handle_command(
                {
                    "id": "turn",
                    "kind": "turn.start",
                    "payload": {
                        "native_thread_id": "imported",
                        "input": [{"type": "text", "text": "hello"}],
                        "execution_settings": {
                            "model": None,
                            "effort": None,
                            "network_access": True,
                        },
                        "settings_revision": 8,
                    },
                }
            )
            assert "error" not in result
            resume = next(params for method, params in peer.calls if method == "thread/resume")
            assert resume == {
                "threadId": "imported",
                "cwd": str(tmp_path.resolve()),
                "excludeTurns": True,
            }
            turn = next(params for method, params in peer.calls if method == "turn/start")
            assert "model" not in turn and "effort" not in turn and "approvalPolicy" not in turn
            assert turn["sandboxPolicy"]["writableRoots"] == ["native-extra-root"]
            assert turn["sandboxPolicy"]["readOnlyAccess"]["type"] == "restricted"
            actual = result["result"]["effective_settings"]
            assert actual["model"] == "custom-imported-model"
            assert actual["approval_policy"] == "untrusted"
            assert actual["network_access"] is True
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_no_settings_causes_no_capability_queries_and_steer_ignores_overrides(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        service.store.save_thread("imported", str(tmp_path))
        try:
            result = await service.handle_command(
                {
                    "id": "turn",
                    "kind": "turn.start",
                    "payload": {
                        "native_thread_id": "imported",
                        "input": [{"type": "text", "text": "hello"}],
                    },
                }
            )
            assert result["result"]["effective_settings"]["model"] == "custom-imported-model"
            assert [method for method, _ in peer.calls] == ["thread/resume", "turn/start"]
            await service.handle_command(
                {
                    "id": "steer",
                    "kind": "turn.steer",
                    "payload": {
                        "native_thread_id": "imported",
                        "input": [{"type": "text", "text": "more"}],
                        "expected_turn_id": "turn-native",
                        "execution_settings": {"model": "invalid"},
                    },
                }
            )
            assert peer.calls[-1][0] == "turn/steer"
            assert set(peer.calls[-1][1]) == {"threadId", "input", "expectedTurnId"}
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_invalid_model_prevents_thread_start_and_cached_command_stays_definitive(tmp_path):
    async def scenario():
        service, peer = node(tmp_path)
        command = {
            "id": "bad",
            "kind": "thread.start",
            "payload": {
                "cwd": str(tmp_path),
                "execution_settings": {"model": "invalid"},
            },
        }
        try:
            first = await service.handle_command(command)
            assert "model" in first["error"] and not first.get("unknown")
            before = len(peer.calls)
            assert await service.handle_command(command) == first
            assert len(peer.calls) == before
            assert not any(method == "thread/start" for method, _ in peer.calls)
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["validation", "turn"])
def test_resumed_baseline_does_not_publish_unapplied_revision(tmp_path, failure):
    async def scenario():
        service, peer = node(tmp_path)
        service.store.save_thread("imported", str(tmp_path))
        original = peer.request

        async def request(method, params):
            if method == "turn/start" and failure == "turn":
                raise RpcError("Native turn rejected", code=-32000)
            return await original(method, params)

        peer.request = request
        settings = {"model": "invalid"} if failure == "validation" else {"network_access": True}
        try:
            result = await service.handle_command(
                {
                    "id": "failed-turn",
                    "kind": "turn.start",
                    "payload": {
                        "native_thread_id": "imported",
                        "input": [{"type": "text", "text": "hello"}],
                        "execution_settings": settings,
                        "settings_revision": 12,
                    },
                }
            )
            assert "error" in result
            assert (
                service.thread_execution["imported"]["settings"]["model"] == "custom-imported-model"
            )
            assert not any(
                event["method"] == "anywhere/execution_settings"
                for event in service.store.pending_events()
            )
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_capabilities_repeated_cursor_and_unexpected_native_failures_are_bounded():
    async def scenario():
        peer = Peer()
        original = peer.request

        async def request(method, params):
            if method == "model/list":
                return {"data": [], "nextCursor": "same"}
            return await original(method, params)

        peer.request = request
        with pytest.raises(ValueError, match="repeated"):
            await RuntimeCapabilities().read(peer)

        async def denied(method, params):
            if method == "configRequirements/read":
                raise RpcError("Policy unavailable", code=-32000)
            return await original(method, params)

        peer.request = denied
        with pytest.raises(RpcError, match="Policy unavailable"):
            await RuntimeCapabilities().validate(peer, "cwd", {"approval_policy": "never"})

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["fast", "default", "priority", None, "future-custom-tier"])
def test_native_service_tier_is_readback_only_and_preserves_nullable_unknown_values(tier):
    baseline = {"model": "inherited-model", "service_tier": "baseline-tier"}
    original = copy.deepcopy(baseline)
    settings = effective_settings({"serviceTier": tier}, baseline)
    assert settings["service_tier"] == tier
    assert settings["model"] == "inherited-model"
    assert baseline == original
    assert thread_overrides({"service_tier": tier}) == {}
    assert turn_overrides({"service_tier": tier}, baseline, None) == {}
    with pytest.raises(ValueError, match="Unsupported execution_settings"):
        requested_settings({"execution_settings": {"service_tier": tier}})


@pytest.mark.parametrize("tier", ["fast", "default", "priority", None, "future-custom-tier"])
def test_missing_native_service_tier_preserves_baseline_without_inventing_defaults(tier):
    assert effective_settings({}, {"service_tier": tier})["service_tier"] == tier
    assert "service_tier" not in effective_settings({})
    assert "service_tier" not in effective_settings({"thread": {"serviceTier": "fast"}})


@pytest.mark.parametrize("tier", ["fast", "default", "priority", None, "future-custom-tier"])
def test_capabilities_read_config_service_tier_without_private_config(tier):
    async def scenario():
        peer = Peer()
        peer.config["service_tier"] = tier
        result = await RuntimeCapabilities().read(peer, "cwd")
        assert result["defaults"]["service_tier"] == tier
        assert result["supported"]["service_tier"] is True
        assert "SECRET" not in str(result)
        assert "service_tier" not in next(
            params for method, params in peer.calls if method == "model/list"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("config_has_null", [False, True])
def test_new_conversation_default_service_tier_can_come_from_selected_model(config_has_null):
    async def scenario():
        peer = Peer()
        peer.config["model"] = "other-model"
        if config_has_null:
            peer.config["service_tier"] = None
        peer.models[0]["defaultServiceTier"] = "priority"
        peer.models[1]["defaultServiceTier"] = "future-model-tier"
        result = await RuntimeCapabilities().read(peer)
        assert result["defaults"]["service_tier"] == "future-model-tier"
        assert result["supported"]["service_tier"] is True
        # An explicit configured tier wins over a catalog default.
        peer.config["service_tier"] = "fast"
        result = await RuntimeCapabilities().read(peer)
        assert result["defaults"]["service_tier"] == "fast"

    asyncio.run(scenario())


def test_default_model_service_tier_is_only_used_for_the_selected_model():
    async def scenario():
        peer = Peer()
        peer.config.pop("model")
        peer.models[0]["defaultServiceTier"] = "default"
        reader = RuntimeCapabilities()
        assert (await reader.read(peer))["defaults"]["service_tier"] == "default"
        peer.config["model"] = "uncatalogued-custom-model"
        result = await reader.read(peer)
        assert "service_tier" not in result["defaults"]
        assert result["supported"]["service_tier"] is False

    asyncio.run(scenario())


def test_capabilities_without_service_tier_evidence_remain_unknown():
    async def scenario():
        peer = Peer()
        result = await RuntimeCapabilities().read(peer)
        assert "service_tier" not in result["defaults"]
        assert result["supported"]["service_tier"] is False

    asyncio.run(scenario())


def test_service_tier_config_changes_invalidate_capabilities_cache():
    async def scenario():
        peer, reader = Peer(), RuntimeCapabilities()

        def catalog_reads():
            return sum(method == "model/list" for method, _ in peer.calls)

        await reader.read(peer, "cwd")
        assert catalog_reads() == 2
        peer.config["service_tier"] = "fast"
        fast = await reader.read(peer, "cwd")
        assert fast["defaults"]["service_tier"] == "fast" and catalog_reads() == 4
        fast["defaults"]["service_tier"] = "caller-mutation"
        cached = await reader.read(peer, "cwd")
        assert cached["defaults"]["service_tier"] == "fast" and catalog_reads() == 4
        peer.config["service_tier"] = "priority"
        priority = await reader.read(peer, "cwd")
        assert priority["defaults"]["service_tier"] == "priority" and catalog_reads() == 6
        peer.config["service_tier"] = None
        nullable = await reader.read(peer, "cwd")
        assert nullable["defaults"]["service_tier"] is None and catalog_reads() == 8
        peer.config.pop("service_tier")
        missing = await reader.read(peer, "cwd")
        assert "service_tier" not in missing["defaults"]
        assert missing["supported"]["service_tier"] is False and catalog_reads() == 10
        peer.models[0]["defaultServiceTier"] = "fast"
        model_default = await reader.read(peer, "cwd", refresh=True)
        assert model_default["defaults"]["service_tier"] == "fast" and catalog_reads() == 12
        peer.models[0]["defaultServiceTier"] = "priority"
        refreshed = await reader.read(peer, "cwd", refresh=True)
        assert refreshed["defaults"]["service_tier"] == "priority" and catalog_reads() == 14

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["fast", "default", "priority", None, "future-custom-tier"])
def test_service_tier_cannot_be_submitted_to_runtime_settings_validation(tier):
    async def scenario():
        peer = Peer()
        with pytest.raises(ValueError, match="Unsupported execution_settings"):
            await RuntimeCapabilities().validate(peer, "cwd", {"service_tier": tier})
        assert peer.calls == []

    asyncio.run(scenario())


def test_invalid_service_tier_structures_do_not_expose_config_or_native_secrets():
    async def scenario():
        peer = Peer()
        peer.config["service_tier"] = {"token": "SECRET-TIER"}
        result = await RuntimeCapabilities().read(peer)
        assert "service_tier" not in result["defaults"]
        assert "SECRET" not in str(result)
        assert "service_tier" not in effective_settings({"serviceTier": {"token": "SECRET-TIER"}})

    asyncio.run(scenario())


def test_missing_and_explicit_null_tiers_remain_distinct_in_readback():
    assert "service_tier" not in effective_settings({})
    assert effective_settings({"serviceTier": None})["service_tier"] is None
    assert effective_settings({}, {"service_tier": None})["service_tier"] is None
    assert (
        effective_settings({"serviceTier": None}, {"service_tier": "fast"})["service_tier"] is None
    )

    async def scenario():
        peer, reader = Peer(), RuntimeCapabilities()
        missing = await reader.read(peer)
        assert "service_tier" not in missing["defaults"]
        peer.config["service_tier"] = None
        configured_null = await reader.read(peer)
        assert "service_tier" in configured_null["defaults"]
        assert configured_null["defaults"]["service_tier"] is None
        peer.config.pop("service_tier")
        peer.models[0]["defaultServiceTier"] = None
        model_null = await reader.read(peer, refresh=True)
        assert model_null["defaults"]["service_tier"] is None
        assert model_null["supported"]["service_tier"] is True
        peer.config["service_tier"] = None
        peer.models[0]["defaultServiceTier"] = ""
        assert (await reader.read(peer))["defaults"]["service_tier"] is None

    asyncio.run(scenario())
