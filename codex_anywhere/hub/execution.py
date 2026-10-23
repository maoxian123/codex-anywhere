"""Browser execution preferences; the node validates native capabilities again."""

from __future__ import annotations

import copy

from werkzeug.exceptions import Conflict

SETTINGS_KEYS = {
    "model",
    "effort",
    "approval_policy",
    "sandbox_mode",
    "approvals_reviewer",
    "network_access",
    "service_tier",
}


def execution_settings(value):
    if not isinstance(value, dict):
        raise ValueError("execution_settings 必须是对象")
    if set(value) - SETTINGS_KEYS:
        raise ValueError("包含未知的运行设置")
    result = {}
    for key, option in value.items():
        if option is None:
            continue  # Explicit null removes an override and inherits the native setting.
        if key == "network_access":
            if not isinstance(option, bool):
                raise ValueError("network_access 必须是布尔值")
        elif not isinstance(option, str) or not option.strip() or len(option) > 200:
            raise ValueError(f"{key} 必须是 1–200 个字符")
        elif key == "sandbox_mode" and option not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ValueError("不支持的沙箱设置")
        elif key == "approval_policy" and option not in {"on-request", "never", "untrusted"}:
            raise ValueError("不支持的审批策略")
        elif key == "approvals_reviewer" and option not in {
            "user",
            "auto_review",
            "guardian_subagent",
        }:
            raise ValueError("不支持的审批执行者")
        elif key == "service_tier" and option not in {"default", "fast"}:
            raise ValueError("不支持的速度模式")
        result[key] = option
    return result


def require_runtime_settings(host, settings):
    if settings and "runtimeSettings" not in (host.capabilities or []):
        raise Conflict("该节点尚不支持运行设置，请更新并重启节点")
    if "service_tier" in settings and "serviceTierWrite" not in (host.capabilities or []):
        raise Conflict("该节点尚不支持切换速度模式，请更新并重启节点")


def project_effective_settings(thread, settings, revision):
    """Never let an old turn's late readback replace a newer applied revision."""
    if not isinstance(settings, dict):
        return
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return
    current = thread.effective_settings_revision
    if current is not None and revision < current:
        return
    # Readbacks can contain native legacy values. Preserve them without making
    # those values newly selectable in the browser.
    safe = {
        key: copy.deepcopy(value)
        for key, value in settings.items()
        if key in SETTINGS_KEYS and key != "service_tier"
    }
    if "service_tier" in settings:
        tier = settings["service_tier"]
        if tier is None or isinstance(tier, str):
            safe["service_tier"] = tier
    elif "service_tier" in (thread.effective_settings or {}):
        # Older nodes can forward native mode events while omitting this newer
        # readback field. Do not erase a confirmed mode with an absent value.
        safe["service_tier"] = thread.effective_settings["service_tier"]
    thread.effective_settings = safe
    thread.effective_settings_revision = revision
