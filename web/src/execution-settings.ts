import type {
  ExecutionSettings,
  NativeExecutionSettings,
  RuntimeCapabilities,
  RuntimeModel,
  Thread,
} from "./types";

export const permissionPresets = [
  {
    id: "read-only",
    label: "只读 · 按需审批",
    sandbox_mode: "read-only",
    approval_policy: "on-request",
  },
  {
    id: "workspace-write",
    label: "工作区写入 · 按需审批",
    sandbox_mode: "workspace-write",
    approval_policy: "on-request",
  },
  {
    id: "danger-full-access",
    label: "完全访问 · 无需审批",
    sandbox_mode: "danger-full-access",
    approval_policy: "never",
  },
] as const;

export function availablePresets(capabilities: RuntimeCapabilities) {
  const constraints = capabilities.constraints;
  return permissionPresets.filter(
    (preset) =>
      (!constraints?.sandbox_modes ||
        constraints.sandbox_modes.includes(preset.sandbox_mode)) &&
      (!constraints?.approval_policies ||
        constraints.approval_policies.includes(preset.approval_policy)),
  );
}
export function modelId(model: RuntimeModel) {
  return model.model || model.id;
}
export function selectedModel(
  capabilities: RuntimeCapabilities,
  settings: NativeExecutionSettings,
) {
  const id = settings.model || capabilities.defaults.model;
  return (
    capabilities.models.find(
      (model) => modelId(model) === id || model.id === id,
    ) ||
    (!id ? capabilities.models.find((model) => model.isDefault) : undefined)
  );
}
/** A model change must never leave an effort which its native descriptor rejects. */
export function changeModel(
  settings: NativeExecutionSettings,
  model: RuntimeModel,
): NativeExecutionSettings {
  const next = { ...settings, model: modelId(model) };
  const supported =
    model.supportedReasoningEfforts?.map((entry) => entry.reasoningEffort) ||
    [];
  const effort =
    settings.effort && supported.includes(settings.effort)
      ? settings.effort
      : model.defaultReasoningEffort &&
          supported.includes(model.defaultReasoningEffort)
        ? model.defaultReasoningEffort
        : supported[0];
  if (effort) next.effort = effort;
  else delete next.effort;
  return next;
}
export function permissionLabel(settings?: NativeExecutionSettings) {
  if (!settings?.sandbox_mode && !settings?.approval_policy)
    return "节点默认权限";
  return (
    permissionPresets.find(
      (preset) =>
        preset.sandbox_mode === settings?.sandbox_mode &&
        preset.approval_policy === settings?.approval_policy,
    )?.label ||
    `${settings?.sandbox_mode || "默认沙箱"} · ${typeof settings?.approval_policy === "object" && settings.approval_policy ? "自定义原生审批" : settings?.approval_policy || "默认审批"}`
  );
}
export function settingsLabel(settings?: NativeExecutionSettings) {
  if (!settings || !Object.keys(settings).length) return "节点默认设置";
  return [settings.model, settings.effort, permissionLabel(settings)]
    .filter(Boolean)
    .join(" · ");
}
/** Keep inherited native values for display without pinning unrelated settings. */
export function changedOverrides(
  desired: ExecutionSettings,
  before: NativeExecutionSettings,
  after: NativeExecutionSettings,
  edited?: (keyof ExecutionSettings)[],
): ExecutionSettings {
  const result = { ...desired };
  for (const key of new Set(
    edited || [...Object.keys(before), ...Object.keys(after)],
  ) as Set<keyof ExecutionSettings>) {
    if (!edited && JSON.stringify(before[key]) === JSON.stringify(after[key]))
      continue;
    if (after[key] === undefined || after[key] === null) delete result[key];
    else Object.assign(result, { [key]: after[key] });
  }
  return result;
}
/** GETs begun before a PATCH may still carry old desired settings. */
export function preserveNewerSettings(
  incoming: Thread,
  current?: Thread | null,
): Thread {
  if (!current || incoming.id !== current.id) return incoming;
  let next = incoming;
  if ((current.settings_revision ?? -1) > (incoming.settings_revision ?? -1))
    next = {
      ...next,
      execution_settings: current.execution_settings,
      settings_revision: current.settings_revision,
    };
  if (
    (current.effective_settings_revision ?? -1) >
    (incoming.effective_settings_revision ?? -1)
  )
    next = {
      ...next,
      effective_settings: current.effective_settings,
      effective_settings_revision: current.effective_settings_revision,
    };
  return next;
}
export function applySettingsUpdate(current: Thread, update: Thread): Thread {
  return preserveNewerSettings(
    {
      ...current,
      execution_settings: update.execution_settings,
      settings_revision: update.settings_revision,
      effective_settings: update.effective_settings,
      effective_settings_revision: update.effective_settings_revision,
    },
    current,
  );
}
