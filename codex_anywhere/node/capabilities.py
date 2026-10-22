"""Safe native execution capabilities and lossless per-thread setting overrides."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import OrderedDict

from .runtime import RpcError

SETTINGS = (
    "model",
    "effort",
    "approval_policy",
    "sandbox_mode",
    "approvals_reviewer",
    "network_access",
)
POLICIES = ("untrusted", "on-request", "never")
MODES = ("read-only", "workspace-write", "danger-full-access")
REVIEWERS = ("user", "auto_review", "guardian_subagent")
MODE_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}


def requested_settings(payload: dict) -> tuple[dict, int]:
    revision = payload.get("settings_revision", 0)
    if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
        raise ValueError("settings_revision must be a non-negative 64-bit integer")
    value = payload.get("execution_settings")
    if value is None:
        value = {"model": payload["model"]} if payload.get("model") else {}
    if not isinstance(value, dict) or set(value) - set(SETTINGS):
        raise ValueError("Unsupported execution_settings fields")
    result = {}
    for key, setting in value.items():
        if setting is None:
            continue  # Inherit means omit the native override, never send a cached default.
        if key == "network_access":
            if type(setting) is not bool:
                raise ValueError("network_access must be a boolean or null")
        elif (
            not isinstance(setting, str)
            or not setting
            or setting.strip() != setting
            or len(setting) > 200
        ):
            raise ValueError(f"{key} must be a non-empty string or null")
        result[key] = setting
    return result, revision


def effective_settings(result: dict, baseline: dict | None = None) -> dict:
    """Read only actual native fields; keep unknown inherited values nullable."""
    settings = {key: (baseline or {}).get(key) for key in SETTINGS}
    # Service tier is native readback only. Preserve an explicit native null,
    # but omit an absent field so older peers do not appear to report a tier.
    if "service_tier" in (baseline or {}):
        tier = baseline["service_tier"]
        if tier is None or isinstance(tier, str):
            settings["service_tier"] = tier
    for native, public in (
        ("model", "model"),
        ("reasoningEffort", "effort"),
        ("approvalPolicy", "approval_policy"),
        ("approvalsReviewer", "approvals_reviewer"),
    ):
        if native in result:
            settings[public] = copy.deepcopy(result[native])
    if "serviceTier" in result:
        tier = result["serviceTier"]
        if tier is None or isinstance(tier, str):
            settings["service_tier"] = tier
    sandbox = result.get("sandbox")
    if isinstance(sandbox, dict):
        settings["sandbox_mode"] = next(
            (mode for mode, tag in MODE_TYPES.items() if tag == sandbox.get("type")), None
        )
        if sandbox.get("type") == "dangerFullAccess":
            settings["network_access"] = True
        elif sandbox.get("type") in {"readOnly", "workspaceWrite"}:
            settings["network_access"] = sandbox.get("networkAccess", False)
        elif sandbox.get("type") == "externalSandbox":
            settings["network_access"] = sandbox.get("networkAccess", "restricted") == "enabled"
        else:
            settings["network_access"] = None
    return settings


def thread_overrides(settings: dict, *, new: bool = False) -> dict:
    params = {"approvalPolicy": "on-request", "sandbox": "workspace-write"} if new else {}
    for public, native in (
        ("model", "model"),
        ("approval_policy", "approvalPolicy"),
        ("sandbox_mode", "sandbox"),
        ("approvals_reviewer", "approvalsReviewer"),
    ):
        if public in settings:
            params[native] = settings[public]
    config = {}
    if "effort" in settings:
        config["model_reasoning_effort"] = settings["effort"]
    if "network_access" in settings and (new or "sandbox_mode" in settings):
        mode = params.get("sandbox")
        network = settings["network_access"]
        if mode == "workspace-write":
            config["sandbox_workspace_write.network_access"] = network
        elif mode == "danger-full-access" and not network:
            raise ValueError("Full access cannot disable network access")
        elif mode == "read-only" and network:
            raise ValueError(
                "Native thread creation cannot enable read-only network access; set it for the next turn"
            )
    if config:
        params["config"] = config
    return params


def turn_overrides(settings: dict, baseline: dict, sandbox: dict | None) -> dict:
    params = {}
    for public, native in (
        ("model", "model"),
        ("effort", "effort"),
        ("approval_policy", "approvalPolicy"),
        ("approvals_reviewer", "approvalsReviewer"),
    ):
        if public in settings:
            params[native] = settings[public]
    if "sandbox_mode" not in settings and "network_access" not in settings:
        return params
    mode = settings.get("sandbox_mode", baseline.get("sandbox_mode"))
    tag = MODE_TYPES.get(mode)
    if tag is None:
        # Unknown/external native sandbox must never be silently replaced.
        if sandbox and sandbox.get("type") == "externalSandbox" and "sandbox_mode" not in settings:
            policy = copy.deepcopy(sandbox)
            policy["networkAccess"] = "enabled" if settings["network_access"] else "restricted"
            params["sandboxPolicy"] = policy
            return params
        raise ValueError("Choose a sandbox mode before changing network access")
    policy = copy.deepcopy(sandbox) if sandbox and sandbox.get("type") == tag else {"type": tag}
    # A different sandbox uses its native network default. Carrying the old
    # full-access value into workspaceWrite would silently broaden that policy.
    same_mode = sandbox and sandbox.get("type") == tag
    network = settings.get("network_access", baseline.get("network_access") if same_mode else None)
    if tag == "dangerFullAccess":
        if settings.get("network_access") is False:
            raise ValueError("Full access cannot disable network access")
    elif network is not None:
        policy["networkAccess"] = network
    params["sandboxPolicy"] = policy
    return params


class RuntimeCapabilities:
    """Bounded metadata cache; config is re-read before accepting desired overrides."""

    def __init__(self):
        self._cache: OrderedDict[tuple, tuple] = OrderedDict()
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cache.clear()

    async def read(self, runtime, cwd: str | None = None, *, refresh: bool = False) -> dict:
        async with self._lock:
            params = {"includeLayers": False, **({"cwd": cwd} if cwd else {})}
            try:
                response = await runtime.request("config/read", params)
            except RpcError as exc:
                if exc.code != -32601:
                    raise
                response = {}
            config = response.get("config") or {}
            defaults = {
                "model": config.get("model"),
                "effort": config.get("model_reasoning_effort"),
                "approval_policy": config.get("approval_policy"),
                "sandbox_mode": config.get("sandbox_mode"),
                "approvals_reviewer": config.get("approvals_reviewer"),
                "network_access": (config.get("sandbox_workspace_write") or {}).get(
                    "network_access"
                ),
            }
            if "service_tier" in config:
                tier = config["service_tier"]
                if tier is None or isinstance(tier, str):
                    defaults["service_tier"] = tier
            if defaults["sandbox_mode"] == "danger-full-access":
                defaults["network_access"] = True
            elif defaults["sandbox_mode"] == "read-only":
                defaults["network_access"] = False
            try:
                requirements_response = await runtime.request("configRequirements/read", {})
            except RpcError as exc:
                if exc.code != -32601:
                    raise
                requirements_response = {}
            requirements = requirements_response.get("requirements") or {}
            constraints = {}
            for native, public in (
                ("allowedApprovalPolicies", "approval_policies"),
                ("allowedSandboxModes", "sandbox_modes"),
                ("allowedApprovalsReviewers", "approvals_reviewers"),
            ):
                if isinstance(requirements.get(native), list):
                    constraints[public] = copy.deepcopy(requirements[native])
            required_auto = (requirements.get("autoReview") or {}).get("requiredOnModels") or []
            required_auto = [model for model in required_auto if isinstance(model, str)]
            reviewer_known = "approvals_reviewer" in config
            auto = defaults["approvals_reviewer"] in {"auto_review", "guardian_subagent"} or bool(
                required_auto
            )
            reviewers = ["user"] if reviewer_known else []
            if auto:
                reviewers.append("auto_review")
            if defaults["approvals_reviewer"] == "guardian_subagent":
                reviewers.append("guardian_subagent")
            if "approvals_reviewers" in constraints:
                reviewers = [
                    value for value in reviewers if value in constraints["approvals_reviewers"]
                ]
            constraints["approvals_reviewers"] = reviewers
            if required_auto:
                constraints["auto_review_required_models"] = required_auto
            safe = (
                defaults,
                constraints,
                config.get("model_provider"),
                "service_tier" in config,
            )
            fingerprint = json.dumps(safe, sort_keys=True, ensure_ascii=False)
            key = (runtime.runtime_id, cwd)
            cached = self._cache.get(key)
            now = time.time()
            if not refresh and cached and cached[0] == fingerprint and now - cached[1] < 60:
                self._cache.move_to_end(key)
                return copy.deepcopy(cached[2])
            models, cursor, seen, catalog_bytes = [], None, set(), 0
            model_supported = True
            for _ in range(100):
                try:
                    page = await runtime.request(
                        "model/list",
                        {
                            "limit": 100,
                            "includeHidden": False,
                            **({"cursor": cursor} if cursor else {}),
                        },
                    )
                except RpcError as exc:
                    if exc.code != -32601:
                        raise
                    model_supported = False
                    break
                data = page.get("data")
                if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                    raise ValueError("Native model catalog returned invalid data")
                catalog_bytes += len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                if len(models) + len(data) > 10_000 or catalog_bytes > 4 * 1024 * 1024:
                    raise ValueError("Native model catalog exceeds the metadata size limit")
                models.extend(data)
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("Native model catalog repeated a pagination cursor")
                seen.add(cursor)
            else:
                raise ValueError("Native model catalog exceeded 100 pages")
            default_model = next((model for model in models if model.get("isDefault")), {})
            if defaults["model"] is None:
                defaults["model"] = default_model.get("model")
            selected = next(
                (model for model in models if model.get("model") == defaults["model"]), {}
            )
            if defaults["effort"] is None:
                defaults["effort"] = selected.get("defaultReasoningEffort")
            if "defaultServiceTier" in selected:
                tier = selected["defaultServiceTier"]
                if "service_tier" not in defaults:
                    if tier is None or isinstance(tier, str):
                        defaults["service_tier"] = tier
                elif defaults["service_tier"] is None and isinstance(tier, str) and tier:
                    defaults["service_tier"] = tier
            result = {
                "models": models,
                "defaults": defaults,
                "constraints": constraints,
                "supported": {
                    "model": model_supported,
                    "effort": model_supported,
                    "service_tier": "service_tier" in config or "defaultServiceTier" in selected,
                    "approval_policy": "approval_policy" in config,
                    "sandbox_mode": "sandbox_mode" in config,
                    "approvals_reviewer": reviewer_known,
                    "network_access": "sandbox_mode" in config,
                    "auto_review": auto and "auto_review" in reviewers,
                },
                "cwd": cwd,
                "runtime_id": runtime.runtime_id,
                "fetched_at": now,
            }
            self._cache[key] = (fingerprint, now, copy.deepcopy(result))
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            return result

    async def validate(
        self, runtime, cwd: str, settings: dict, baseline: dict | None = None
    ) -> dict:
        if not isinstance(settings, dict) or set(settings) - set(SETTINGS):
            raise ValueError("Unsupported execution_settings fields")
        if not settings:
            return {}
        capabilities = await self.read(runtime, cwd)
        defaults, constraints = capabilities["defaults"], capabilities["constraints"]
        baseline = baseline or {}
        models = capabilities["models"]
        selected_model = settings.get("model", baseline.get("model") or defaults["model"])
        selected = next((entry for entry in models if entry.get("model") == selected_model), None)
        if (
            "model" in settings
            and selected is None
            and selected_model not in {defaults.get("model"), baseline.get("model")}
        ):
            raise ValueError("Selected model is not in the native runtime catalog")
        if "effort" in settings:
            options = (selected or {}).get("supportedReasoningEfforts", [])
            if not isinstance(options, list):
                raise ValueError("Native model reasoning options are invalid")
            efforts = {
                effort
                for option in options
                if isinstance(
                    effort := (
                        option.get("reasoningEffort") if isinstance(option, dict) else option
                    ),
                    str,
                )
            }
            if settings["effort"] not in efforts and not (
                selected is None
                and (
                    settings["effort"] == baseline.get("effort")
                    or (
                        selected_model == defaults.get("model")
                        and settings["effort"] == defaults.get("effort")
                    )
                )
            ):
                raise ValueError("Selected reasoning effort is not supported by this model")
        for setting, valid, constraint in (
            ("approval_policy", POLICIES, "approval_policies"),
            ("sandbox_mode", MODES, "sandbox_modes"),
            ("approvals_reviewer", REVIEWERS, "approvals_reviewers"),
        ):
            if setting not in settings:
                continue
            if settings[setting] not in valid:
                raise ValueError(f"Unsupported {setting}")
            allowed = constraints.get(constraint)
            if allowed is not None and settings[setting] not in allowed:
                raise ValueError(f"Managed/native requirements reject {setting}")
        required = constraints.get("auto_review_required_models", [])
        if selected_model and selected_model.rsplit("/", 1)[-1] in required:
            reviewer = settings.get(
                "approvals_reviewer",
                baseline.get("approvals_reviewer") or defaults["approvals_reviewer"],
            )
            if reviewer not in {"auto_review", "guardian_subagent"}:
                raise ValueError("Managed requirements require automatic review for this model")
        return settings
