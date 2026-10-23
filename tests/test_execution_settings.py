from types import SimpleNamespace

import pytest

from codex_anywhere.hub.execution import execution_settings, project_effective_settings


@pytest.mark.parametrize("tier", ["default", "fast"])
def test_speed_selection_is_an_execution_override(tier):
    assert execution_settings({"service_tier": tier}) == {"service_tier": tier}


def test_null_speed_selection_removes_the_override():
    assert execution_settings({"service_tier": None, "effort": "high"}) == {"effort": "high"}


@pytest.mark.parametrize(
    "tier", ["priority", "future-tier", "normal", " fast", "fast ", True, {}, []]
)
def test_only_public_normal_and_fast_are_writable(tier):
    with pytest.raises(ValueError):
        execution_settings({"service_tier": tier})


@pytest.mark.parametrize("tier", [None, "priority", "future-native-tier"])
def test_native_readback_remains_lossless_and_separate_from_write_choices(tier):
    thread = SimpleNamespace(effective_settings_revision=0, effective_settings={})
    project_effective_settings(thread, {"service_tier": tier}, 1)
    assert thread.effective_settings == {"service_tier": tier}


def test_invalid_native_speed_structure_cannot_escape_readback_sanitization():
    thread = SimpleNamespace(effective_settings_revision=0, effective_settings={})
    project_effective_settings(thread, {"service_tier": {"secret": "private"}, "effort": "high"}, 1)
    assert thread.effective_settings == {"effort": "high"}


def test_unrelated_readback_preserves_confirmed_native_speed():
    thread = SimpleNamespace(
        effective_settings_revision=0, effective_settings={"service_tier": "priority"}
    )
    project_effective_settings(thread, {"effort": "high"}, 1)
    assert thread.effective_settings == {"effort": "high", "service_tier": "priority"}
