import { useState } from "react";
import { RefreshCw, Settings2 } from "lucide-react";
import { api, errorText } from "./api";
import { ErrorMessage, Modal, Spinner } from "./components";
import {
  availablePresets,
  changeModel,
  changedOverrides,
  modelId,
  permissionLabel,
  selectedModel,
  settingsLabel,
} from "./execution-settings";
import { useRuntimeCapabilities } from "./useRuntimeCapabilities";
import type {
  ExecutionSettings,
  NativeExecutionSettings,
  Host,
  Project,
  RuntimeCapabilities,
  Thread,
} from "./types";

export function ExecutionFields({
  capabilities,
  settings,
  disabled,
  onChange,
  inheritedPermissionLabel,
  inheritedModelLabel,
  allowReadonlyNetwork = true,
}: {
  capabilities: RuntimeCapabilities;
  settings: NativeExecutionSettings;
  disabled?: boolean;
  onChange: (
    settings: NativeExecutionSettings,
    edited?: (keyof ExecutionSettings)[],
  ) => void;
  inheritedPermissionLabel?: string;
  inheritedModelLabel?: string;
  allowReadonlyNetwork?: boolean;
}) {
  const effective = { ...capabilities.defaults, ...settings };
  const model =
    !effective.model && inheritedModelLabel
      ? undefined
      : selectedModel(capabilities, settings);
  const currentId = model ? modelId(model) : effective.model || "";
  const models = capabilities.models.filter(
    (entry) => !entry.hidden || modelId(entry) === currentId,
  );
  const unknownModel = Boolean(
    currentId &&
    !models.some(
      (entry) => modelId(entry) === currentId || entry.id === currentId,
    ),
  );
  const efforts = model?.supportedReasoningEfforts || [];
  const currentEffort =
    effective.effort ||
    (inheritedModelLabel ? "" : model?.defaultReasoningEffort) ||
    "";
  const presets = availablePresets(capabilities);
  const preset = presets.find(
    (entry) =>
      entry.sandbox_mode === effective.sandbox_mode &&
      entry.approval_policy === effective.approval_policy,
  );
  const supported = capabilities.supported;
  const allowedReviewers = capabilities.constraints?.approvals_reviewers || [];
  const autoReviewer = allowedReviewers.find(
    (value) => value === "auto_review",
  );
  const humanReviewer = allowedReviewers.find((value) => value === "user");
  const fullAccess = effective.sandbox_mode === "danger-full-access";
  const readonlyNetworkBlocked =
    effective.sandbox_mode === "read-only" && !allowReadonlyNetwork;
  return (
    <div className="execution-fields">
      <label title={model?.description}>
        <span>模型</span>
        <select
          aria-label="模型"
          value={currentId}
          disabled={disabled || supported?.model === false || !models.length}
          onChange={(event) => {
            const chosen = models.find(
              (entry) => modelId(entry) === event.target.value,
            );
            if (chosen)
              onChange(
                changeModel(
                  {
                    ...settings,
                    ...(effective.effort ? { effort: effective.effort } : {}),
                  },
                  chosen,
                ),
                ["model", "effort"],
              );
          }}
        >
          {!currentId && (
            <option value="">{inheritedModelLabel || "节点默认模型"}</option>
          )}
          {unknownModel && (
            <option value={currentId}>{currentId}（当前会话）</option>
          )}
          {models.map((entry) => (
            <option key={entry.id} value={modelId(entry)}>
              {entry.displayName || modelId(entry)}
              {entry.isDefault ? " · 默认" : ""}
            </option>
          ))}
          {!models.length && !currentId && (
            <option value="" disabled>
              节点未返回可用模型
            </option>
          )}
        </select>
      </label>
      <label>
        <span>推理强度</span>
        <select
          aria-label="推理强度"
          value={currentEffort}
          disabled={disabled || supported?.effort === false || !efforts.length}
          onChange={(event) =>
            onChange({ ...settings, effort: event.target.value }, ["effort"])
          }
        >
          {(!currentEffort || !efforts.length) && (
            <option value={currentEffort}>
              {currentEffort ||
                (unknownModel
                  ? "当前模型未报告强度"
                  : inheritedModelLabel || "节点默认")}
            </option>
          )}
          {currentEffort &&
            efforts.length > 0 &&
            !efforts.some(
              (entry) => entry.reasoningEffort === currentEffort,
            ) && (
              <option value={currentEffort}>{currentEffort}（当前设置）</option>
            )}
          {efforts.map((entry) => (
            <option
              key={entry.reasoningEffort}
              value={entry.reasoningEffort}
              title={entry.description}
            >
              {entry.reasoningEffort}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>权限</span>
        <select
          aria-label="权限"
          value={preset?.id || "custom"}
          disabled={
            disabled ||
            supported?.sandbox_mode === false ||
            supported?.approval_policy === false ||
            !presets.length
          }
          onChange={(event) => {
            const chosen = presets.find(
              (entry) => entry.id === event.target.value,
            );
            if (chosen)
              onChange(
                {
                  ...settings,
                  sandbox_mode: chosen.sandbox_mode,
                  approval_policy: chosen.approval_policy,
                  network_access: chosen.id === "danger-full-access",
                },
                ["sandbox_mode", "approval_policy", "network_access"],
              );
          }}
        >
          {!preset && (
            <option value="custom">
              {!effective.sandbox_mode &&
              !effective.approval_policy &&
              inheritedPermissionLabel
                ? inheritedPermissionLabel
                : permissionLabel(effective)}
            </option>
          )}
          {presets.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </select>
      </label>
      {(supported?.network_access === true ||
        (supported?.auto_review === true &&
          supported?.approvals_reviewer === true &&
          autoReviewer &&
          humanReviewer)) && (
        <details className="execution-advanced">
          <summary aria-label="更多权限设置">
            <Settings2 size={14} />
          </summary>
          <div>
            {supported?.network_access === true && (
              <div>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={
                      fullAccess ||
                      (!readonlyNetworkBlocked &&
                        effective.network_access === true)
                    }
                    disabled={disabled || fullAccess || readonlyNetworkBlocked}
                    onChange={(event) =>
                      onChange(
                        { ...settings, network_access: event.target.checked },
                        ["network_access"],
                      )
                    }
                  />
                  允许沙箱联网
                </label>
                {fullAccess && (
                  <p className="field-hint">
                    完全访问始终允许联网。限制联网请选择工作区写入。
                  </p>
                )}
                {readonlyNetworkBlocked && (
                  <p className="field-hint">
                    新会话的只读沙箱不支持联网。需要联网请选择工作区写入。
                  </p>
                )}
              </div>
            )}
            {supported?.auto_review === true &&
              supported?.approvals_reviewer === true &&
              autoReviewer &&
              humanReviewer && (
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={effective.approvals_reviewer === autoReviewer}
                    disabled={disabled}
                    onChange={(event) =>
                      onChange(
                        {
                          ...settings,
                          approvals_reviewer: event.target.checked
                            ? autoReviewer
                            : humanReviewer,
                        },
                        ["approvals_reviewer"],
                      )
                    }
                  />
                  使用原生自动审阅
                </label>
              )}
          </div>
        </details>
      )}
    </div>
  );
}

export function RuntimeSettingsEditor({
  host,
  cwd,
  settings,
  displaySettings,
  disabled,
  onChange,
  permissionsMode = "new",
}: {
  host?: Host;
  cwd: string;
  settings: ExecutionSettings;
  disabled?: boolean;
  onChange: (settings: ExecutionSettings) => void;
  permissionsMode?: "new" | "existing";
  displaySettings?: NativeExecutionSettings;
}) {
  const runtime = useRuntimeCapabilities(host, cwd);
  // Capability defaults describe this Codex installation, which may itself run
  // unrestricted. New tasks use our safe defaults; imported tasks keep native
  // settings unless the user explicitly selects an override.
  const capabilities = runtime.value
    ? {
        ...runtime.value,
        defaults: {
          ...(permissionsMode === "new"
            ? {
                model: runtime.value.defaults.model,
                effort: runtime.value.defaults.effort,
                sandbox_mode: "workspace-write",
                approval_policy: "on-request",
              }
            : {}),
        },
      }
    : null;
  const displayed = { ...displaySettings, ...settings };
  return (
    <div className="runtime-settings-editor">
      {capabilities ? (
        <ExecutionFields
          capabilities={capabilities}
          settings={displayed}
          disabled={disabled || !host?.online || !!runtime.reason}
          onChange={(next, edited) =>
            onChange(changedOverrides(settings, displayed, next, edited))
          }
          inheritedPermissionLabel={
            permissionsMode === "existing" ? "保留原会话设置" : undefined
          }
          inheritedModelLabel={
            permissionsMode === "existing" ? "保留原会话设置" : undefined
          }
          allowReadonlyNetwork={permissionsMode === "existing"}
        />
      ) : (
        <div className="execution-unavailable">
          <span>
            {runtime.loading ? (
              <Spinner label="读取模型与运行设置" />
            ) : (
              runtime.reason || "模型与运行设置暂不可用"
            )}
          </span>
          <div className="execution-fields" aria-label="运行设置不可用">
            {["模型", "推理强度", "权限"].map((label) => (
              <label key={label}>
                <span>{label}</span>
                <select disabled aria-label={label}>
                  <option>
                    {label === "模型"
                      ? settings.model ||
                        (permissionsMode === "existing"
                          ? "保留原会话设置"
                          : "节点默认")
                      : label === "推理强度"
                        ? settings.effort ||
                          (permissionsMode === "existing"
                            ? "保留原会话设置"
                            : "节点默认")
                        : permissionsMode === "existing" &&
                            !settings.sandbox_mode &&
                            !settings.approval_policy
                          ? "保留原会话设置"
                          : permissionLabel(settings)}
                  </option>
                </select>
              </label>
            ))}
          </div>
        </div>
      )}
      {!runtime.reason && (
        <button
          type="button"
          className="icon-button runtime-refresh"
          aria-label="刷新模型与运行设置"
          disabled={runtime.loading || disabled}
          onClick={runtime.refresh}
        >
          <RefreshCw size={13} />
        </button>
      )}
      {runtime.error && <ErrorMessage>{runtime.error}</ErrorMessage>}
    </div>
  );
}

export function ThreadExecutionControls({
  thread,
  host,
  project,
  onSave,
  onProjectSettings,
  onBusyChange,
}: {
  thread: Thread;
  host?: Host;
  project?: Project;
  onSave?: (settings: ExecutionSettings) => Promise<void>;
  onProjectSettings?: (project: Project) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const pending =
    (thread.settings_revision ?? 0) > (thread.effective_settings_revision ?? 0);
  const save = async (settings: ExecutionSettings) => {
    if (!onSave || saving) return;
    setSaving(true);
    onBusyChange?.(true);
    setError("");
    try {
      await onSave(settings);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setSaving(false);
      onBusyChange?.(false);
    }
  };
  return (
    <div className="thread-execution-controls">
      <RuntimeSettingsEditor
        host={host}
        cwd={thread.cwd}
        settings={thread.execution_settings || {}}
        displaySettings={thread.effective_settings}
        permissionsMode="existing"
        disabled={saving || !onSave}
        onChange={(settings) => void save(settings)}
      />
      <div className="execution-status">
        <span title={settingsLabel(thread.effective_settings)}>
          {saving ? "保存设置…" : pending ? "已保存，下次执行生效" : "运行设置"}
        </span>
        {pending && (
          <span className="effective-settings">
            当前：{settingsLabel(thread.effective_settings)}
          </span>
        )}
        {project && onProjectSettings && (
          <button
            type="button"
            className="text-button"
            onClick={() => onProjectSettings(project)}
          >
            项目默认设置
          </button>
        )}
      </div>
      {error && <ErrorMessage>{error}</ErrorMessage>}
    </div>
  );
}

export function ProjectSettingsDialog({
  project,
  host,
  onClose,
  onSaved,
}: {
  project: Project;
  host?: Host;
  onClose: () => void;
  onSaved: (project: Project) => void;
}) {
  const [settings, setSettings] = useState<ExecutionSettings>(
    project.execution_settings || {},
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ project: Project }>(
        `/projects/${encodeURIComponent(project.id)}/settings`,
        {
          method: "PATCH",
          body: JSON.stringify({ execution_settings: settings }),
        },
      );
      onSaved(result.project);
      onClose();
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="项目默认设置"
      subtitle={`${project.name} · ${project.cwd}`}
      onClose={onClose}
    >
      <div className="form-stack">
        <p className="field-hint">
          仅用于此项目新建的会话。已有会话可在输入框下方分别调整。
        </p>
        <RuntimeSettingsEditor
          host={host}
          cwd={project.cwd}
          settings={settings}
          disabled={busy}
          onChange={setSettings}
        />
        {error && <ErrorMessage>{error}</ErrorMessage>}
        <div className="modal-actions">
          <button
            className="button button-quiet"
            onClick={onClose}
            disabled={busy}
          >
            取消
          </button>
          <button
            className="button button-primary"
            onClick={() => void save()}
            disabled={
              busy ||
              !host?.online ||
              !host.capabilities?.includes("runtimeSettings")
            }
          >
            {busy ? "保存中…" : "保存项目默认"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
