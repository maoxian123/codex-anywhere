import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import {
  ExecutionFields,
  ProjectSettingsDialog,
  RuntimeSettingsEditor,
  ThreadExecutionControls,
  ThreadServiceTierControl,
} from "./ExecutionControls";
import { NewTaskDialog } from "./Dialogs";
import {
  availablePresets,
  availableServiceTiers,
  changeModel,
  changedOverrides,
  preserveNewerSettings,
  serviceTierLabel,
  serviceTierSummary,
} from "./execution-settings";
import { fetchRuntimeCapabilities } from "./useRuntimeCapabilities";
import type {
  ExecutionSettings,
  NativeExecutionSettings,
  Host,
  Project,
  RuntimeCapabilities,
  Thread,
} from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});
const capabilities: RuntimeCapabilities = {
  models: [
    {
      id: "id-a",
      model: "model-a",
      displayName: "Model A",
      isDefault: true,
      defaultReasoningEffort: "medium",
      supportedReasoningEfforts: [
        { reasoningEffort: "medium" },
        { reasoningEffort: "high" },
      ],
    },
    {
      id: "id-b",
      model: "model-b",
      displayName: "Model B",
      defaultReasoningEffort: "low",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "medium" },
      ],
    },
  ],
  defaults: {
    model: "model-a",
    effort: "medium",
    sandbox_mode: "workspace-write",
    approval_policy: "on-request",
  },
  constraints: {
    sandbox_modes: ["read-only", "workspace-write", "danger-full-access"],
    approval_policies: ["on-request", "never"],
    approvals_reviewers: ["user"],
  },
  supported: {
    model: true,
    effort: true,
    approval_policy: true,
    sandbox_mode: true,
    auto_review: false,
    approvals_reviewer: true,
    network_access: false,
  },
};
const host = (id: string): Host => ({
  id,
  name: id,
  platform: "windows",
  online: true,
  capabilities: ["runtimeSettings"],
});
const project: Project = {
  id: "p-settings",
  host_id: "h-settings",
  name: "App",
  cwd: "C:\\app",
  thread_count: 0,
  created_at: "now",
  updated_at: "now",
};
const thread: Thread = {
  id: "t-settings",
  host_id: project.host_id,
  project_id: project.id,
  title: "Task",
  cwd: project.cwd,
  status: "idle",
  created_at: "now",
  updated_at: "now",
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

test("a runtime restart invalidates the client capability cache immediately", async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    reads += 1;
    return response({ capabilities });
  };
  await fetchRuntimeCapabilities("epoch-host", "/work", false, "epoch-1");
  await fetchRuntimeCapabilities("epoch-host", "/work", false, "epoch-1");
  assert.equal(reads, 1);
  await fetchRuntimeCapabilities("epoch-host", "/work", false, "epoch-2");
  assert.equal(reads, 2);
});

test("model changes choose only a supported native effort and preserve compatible values", () => {
  assert.deepEqual(
    changeModel(
      { model: "model-a", effort: "high", sandbox_mode: "read-only" },
      capabilities.models[1],
    ),
    { model: "model-b", effort: "low", sandbox_mode: "read-only" },
  );
  assert.equal(
    changeModel({ effort: "medium" }, capabilities.models[1]).effort,
    "medium",
  );
  assert.equal(
    changeModel(
      { effort: "high" },
      { id: "plain", model: "plain", displayName: "Plain" },
    ).effort,
    undefined,
  );
});

test("native service tiers distinguish fast, normal, ultrafast, missing and future values", () => {
  for (const [tier, label] of [
    ["fast", "快速模式"],
    ["priority", "快速模式"],
    ["default", "正常模式"],
    ["ultrafast", "超快模式"],
    ["flex", "其他模式（flex）"],
    [undefined, "尚未读取模式"],
    [null, "正常模式（默认）"],
    ["", "尚未读取模式"],
  ] as const)
    assert.equal(serviceTierLabel(tier), label);
});

test("unrelated edits preserve an explicit mode override without copying native readback", () => {
  const desired: ExecutionSettings = { model: "model-a", service_tier: "fast" };
  assert.deepEqual(
    changedOverrides(
      desired,
      { model: "model-a", effort: "medium", service_tier: "default" },
      { model: "model-a", effort: "high", service_tier: "priority" },
    ),
    { model: "model-a", effort: "high", service_tier: "fast" },
  );
  assert.deepEqual(
    changedOverrides(
      {},
      { service_tier: "priority" },
      { service_tier: "priority", effort: "high" },
      ["effort"],
    ),
    { effort: "high" },
  );
});

test("existing settings show their native mode rather than the new-conversation default", async () => {
  globalThis.fetch = async () =>
    response({
      capabilities: {
        ...capabilities,
        defaults: { ...capabilities.defaults, service_tier: "fast" },
      },
    });
  const h = host("mode-existing-host");
  const view = render(
    <RuntimeSettingsEditor
      host={h}
      cwd="F:\\mode-existing"
      settings={{}}
      displaySettings={{ service_tier: "default" }}
      permissionsMode="existing"
      onChange={() => {}}
    />,
  );
  await screen.findByRole("option", { name: /Model A/ });
  const mode = screen.getByRole("status", { name: "运行模式" });
  assert.match(mode.textContent || "", /当前：正常模式/);
  assert.doesNotMatch(mode.textContent || "", /快速模式/);
  view.rerender(
    <RuntimeSettingsEditor
      host={h}
      cwd="F:\\mode-existing"
      settings={{}}
      permissionsMode="existing"
      onChange={() => {}}
    />,
  );
  assert.match(mode.textContent || "", /当前模式未读取/);
  assert.match(mode.textContent || "", /节点默认：快速模式（参考）/);
  assert.doesNotMatch(mode.textContent || "", /当前：快速模式|下次：快速模式/);
});

test("settings display cached native modes offline and unknown defaults when a read fails", async () => {
  const h = host("mode-offline-host");
  const view = render(
    <RuntimeSettingsEditor
      host={{ ...h, online: false }}
      cwd="F:\\mode-offline"
      settings={{}}
      displaySettings={{ service_tier: "priority" }}
      permissionsMode="existing"
      onChange={() => {}}
    />,
  );
  assert.match(
    screen.getByRole("status", { name: "运行模式" }).textContent || "",
    /快速模式.*离线缓存/,
  );
  view.unmount();
  globalThis.fetch = async () => {
    throw new Error("cannot read settings");
  };
  render(
    <RuntimeSettingsEditor
      host={host("mode-unavailable-host")}
      cwd="F:\\unavailable-mode"
      settings={{}}
      onChange={() => {}}
    />,
  );
  await screen.findByText("无法连接管理中心，请检查网络后重试。");
  const mode = screen.getByRole("status", { name: "运行模式" });
  assert.match(mode.textContent || "", /新对话模式：模式待读取/);
  assert.doesNotMatch(mode.textContent || "", /正常模式/);
});

test("new-task effort reflects the native configured value before the catalog model default", async () => {
  const configured: RuntimeCapabilities = {
    ...capabilities,
    defaults: { ...capabilities.defaults, effort: "xhigh" },
    models: [
      {
        ...capabilities.models[0],
        defaultReasoningEffort: "low",
        supportedReasoningEfforts: [
          { reasoningEffort: "low" },
          { reasoningEffort: "xhigh" },
        ],
      },
    ],
  };
  globalThis.fetch = async () => response({ capabilities: configured });
  const h = host("configured-effort-host");
  render(
    <NewTaskDialog
      hosts={[h]}
      projects={[{ ...project, host_id: h.id }]}
      onClose={() => {}}
      onCreated={() => {}}
      onNewProject={() => {}}
    />,
  );
  await screen.findByRole("option", { name: "xhigh" });
  assert.equal(
    (screen.getByRole("combobox", { name: "推理强度" }) as HTMLSelectElement)
      .value,
    "xhigh",
  );
});

test("full access explicitly replaces a prior network restriction and cannot disable networking", () => {
  let latest: NativeExecutionSettings = {};
  function Editor() {
    const [settings, setSettings] = useState<NativeExecutionSettings>({
      sandbox_mode: "workspace-write",
      approval_policy: "on-request",
      network_access: false,
    });
    return (
      <ExecutionFields
        capabilities={{
          ...capabilities,
          supported: { ...capabilities.supported, network_access: true },
        }}
        settings={settings}
        onChange={(value) => {
          latest = value;
          setSettings(value);
        }}
      />
    );
  }
  render(<Editor />);
  fireEvent.change(screen.getByRole("combobox", { name: "权限" }), {
    target: { value: "danger-full-access" },
  });
  assert.equal(latest.network_access, true);
  assert.equal(latest.sandbox_mode, "danger-full-access");
  fireEvent.click(screen.getByLabelText("更多权限设置"));
  const networking = screen.getByRole("checkbox", {
    name: "允许沙箱联网",
  }) as HTMLInputElement;
  assert.equal(networking.checked, true);
  assert.equal(networking.disabled, true);
  assert.ok(
    screen.getByText("完全访问始终允许联网。限制联网请选择工作区写入。"),
  );
});

test("new-task read-only permissions cannot opt into an unsupported native networking combination", async () => {
  globalThis.fetch = async () =>
    response({
      capabilities: {
        ...capabilities,
        supported: { ...capabilities.supported, network_access: true },
      },
    });
  const h = host("readonly-network-host");
  render(
    <NewTaskDialog
      hosts={[h]}
      projects={[{ ...project, host_id: h.id }]}
      onClose={() => {}}
      onCreated={() => {}}
      onNewProject={() => {}}
    />,
  );
  await screen.findByRole("option", { name: /Model A/ });
  fireEvent.change(screen.getByRole("combobox", { name: "权限" }), {
    target: { value: "read-only" },
  });
  fireEvent.click(screen.getByLabelText("更多权限设置"));
  const networking = screen.getByRole("checkbox", {
    name: "允许沙箱联网",
  }) as HTMLInputElement;
  assert.equal(networking.checked, false);
  assert.equal(networking.disabled, true);
  assert.ok(
    screen.getByText("新会话的只读沙箱不支持联网。需要联网请选择工作区写入。"),
  );
});

test("an imported thread without effective model metadata displays inherited settings without saving an override", async () => {
  globalThis.fetch = async () => response({ capabilities });
  let saves = 0;
  render(
    <ThreadExecutionControls
      thread={{
        ...thread,
        imported: true,
        execution_settings: {},
        effective_settings: {},
      }}
      host={host("unknown-imported-settings")}
      onSave={async () => {
        saves += 1;
      }}
    />,
  );
  await screen.findByRole("option", { name: /Model A/ });
  const model = screen.getByRole("combobox", {
    name: "模型",
  }) as HTMLSelectElement;
  const effort = screen.getByRole("combobox", {
    name: "推理强度",
  }) as HTMLSelectElement;
  assert.equal(model.value, "");
  assert.equal(model.selectedOptions[0].textContent, "保留原会话设置");
  assert.equal(effort.selectedOptions[0].textContent, "保留原会话设置");
  assert.equal(effort.disabled, true);
  assert.equal(saves, 0);
});

test("dynamic pickers preserve an imported unknown model and never widen permission automatically", () => {
  let latest: NativeExecutionSettings = {};
  function Editor() {
    const [settings, setSettings] = useState<NativeExecutionSettings>({
      model: "old-custom",
      effort: "old-strength",
      sandbox_mode: "read-only",
      approval_policy: "on-request",
    });
    return (
      <ExecutionFields
        capabilities={capabilities}
        settings={settings}
        onChange={(value) => {
          latest = value;
          setSettings(value);
        }}
      />
    );
  }
  render(<Editor />);
  assert.equal(
    (screen.getByRole("combobox", { name: "模型" }) as HTMLSelectElement).value,
    "old-custom",
  );
  assert.equal(
    (screen.getByRole("combobox", { name: "推理强度" }) as HTMLSelectElement)
      .disabled,
    true,
  );
  assert.equal(
    (screen.getByRole("combobox", { name: "权限" }) as HTMLSelectElement).value,
    "read-only",
  );
  assert.equal(screen.queryByText("使用原生自动审阅"), null);
  fireEvent.change(screen.getByRole("combobox", { name: "模型" }), {
    target: { value: "model-b" },
  });
  assert.equal(latest.effort, "low");
  assert.equal(latest.sandbox_mode, "read-only");
  fireEvent.change(screen.getByRole("combobox", { name: "权限" }), {
    target: { value: "danger-full-access" },
  });
  assert.equal(latest.sandbox_mode, "danger-full-access");
  assert.equal(latest.approval_policy, "never");
});

test("managed constraints remove disallowed presets and automatic review needs both support and an allowed native value", () => {
  const narrowed = {
    ...capabilities,
    constraints: {
      sandbox_modes: ["read-only"],
      approval_policies: ["on-request"],
      approvals_reviewers: ["user", "auto_review"],
    },
  };
  assert.deepEqual(
    availablePresets(narrowed).map((entry) => entry.id),
    ["read-only"],
  );
  const view = render(
    <ExecutionFields
      capabilities={narrowed}
      settings={{}}
      onChange={() => {}}
    />,
  );
  assert.equal(
    screen.queryByRole("option", { name: "完全访问 · 无需审批" }),
    null,
  );
  assert.equal(screen.queryByText("使用原生自动审阅"), null);
  view.rerender(
    <ExecutionFields
      capabilities={{
        ...narrowed,
        supported: { ...capabilities.supported, auto_review: true },
      }}
      settings={{ approvals_reviewer: "user" }}
      onChange={() => {}}
    />,
  );
  assert.ok(screen.getByText("使用原生自动审阅"));
});

test("capability reads share an in-flight command and cache by host plus directory", async () => {
  const requests: string[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    requests.push(`${init?.method || "GET"} ${path}`);
    if (init?.method === "POST")
      return response(
        {
          command: {
            id: "caps",
            kind: "capabilities",
            status: "completed",
            result: capabilities,
          },
        },
        202,
      );
    return response({ capabilities: null });
  };
  const [first, second] = await Promise.all([
    fetchRuntimeCapabilities("cache-test", "C:\\one"),
    fetchRuntimeCapabilities("cache-test", "C:\\one"),
  ]);
  assert.equal(first, second);
  assert.equal(requests.length, 2);
  await fetchRuntimeCapabilities("cache-test", "C:\\one");
  assert.equal(requests.length, 2);
  await fetchRuntimeCapabilities("cache-test", "C:\\two");
  assert.equal(requests.length, 4);
});

test("an old node disables runtime controls without sending a capability request", () => {
  globalThis.fetch = async () => {
    throw new Error("old node must not request runtime settings");
  };
  render(
    <RuntimeSettingsEditor
      host={{ ...host("old-host"), capabilities: [] }}
      cwd="/work"
      settings={{ model: "old-model" }}
      onChange={() => {}}
    />,
  );
  assert.ok(screen.getByText(/此节点未提供运行设置能力/));
  assert.equal(
    (screen.getByRole("combobox", { name: "模型" }) as HTMLSelectElement)
      .disabled,
    true,
  );
});

test("unrestricted native defaults never become implicit new-task permissions or imported overrides", async () => {
  const danger = {
    ...capabilities,
    defaults: {
      ...capabilities.defaults,
      sandbox_mode: "danger-full-access",
      approval_policy: "never",
    },
  };
  let submitted: Record<string, unknown> | undefined;
  globalThis.fetch = async (_input, init) => {
    if (init?.method === "POST") {
      submitted = JSON.parse(String(init.body));
      return response(
        {
          thread,
          command: { id: "safe-start", kind: "thread.start", status: "queued" },
        },
        202,
      );
    }
    return response({ capabilities: danger });
  };
  const h = host("danger-config-host"),
    p = { ...project, host_id: h.id };
  const view = render(
    <NewTaskDialog
      hosts={[h]}
      projects={[p]}
      onClose={() => {}}
      onCreated={() => {}}
      onNewProject={() => {}}
    />,
  );
  await screen.findByRole("option", { name: /Model A/ });
  assert.equal(
    (screen.getByRole("combobox", { name: "权限" }) as HTMLSelectElement).value,
    "workspace-write",
  );
  fireEvent.change(screen.getByRole("combobox", { name: "模型" }), {
    target: { value: "model-b" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
  await waitFor(() => assert.ok(submitted));
  const settings = submitted!.execution_settings as ExecutionSettings;
  assert.equal(settings.sandbox_mode, undefined);
  assert.equal(settings.approval_policy, undefined);
  view.unmount();
  render(
    <ThreadExecutionControls
      thread={{ ...thread, host_id: h.id }}
      host={h}
      onSave={async () => {}}
    />,
  );
  await screen.findAllByRole("option", { name: "保留原会话设置" });
  assert.equal(
    (screen.getByRole("combobox", { name: "权限" }) as HTMLSelectElement).value,
    "custom",
  );
});

test("late capabilities for a previous directory never replace the selected project's models", async () => {
  let finish!: (value: Response) => void;
  globalThis.fetch = async (input) => {
    if (String(input).includes(encodeURIComponent("C:\\slow")))
      return new Promise((resolve) => {
        finish = resolve;
      });
    return response({
      capabilities: {
        ...capabilities,
        models: [{ id: "fast", model: "fast", displayName: "Fast directory" }],
        defaults: { model: "fast" },
      },
    });
  };
  const h = host("context-test");
  const view = render(
    <RuntimeSettingsEditor
      host={h}
      cwd={"C:\\slow"}
      settings={{}}
      onChange={() => {}}
    />,
  );
  view.rerender(
    <RuntimeSettingsEditor
      host={h}
      cwd={"C:\\fast"}
      settings={{}}
      onChange={() => {}}
    />,
  );
  await screen.findByRole("option", { name: "Fast directory" });
  await act(async () => finish(response({ capabilities })));
  assert.equal(screen.queryByRole("option", { name: /Model A/ }), null);
  assert.equal(
    (screen.getByRole("combobox", { name: "模型" }) as HTMLSelectElement).value,
    "fast",
  );
});

test("project defaults save native settings and new tasks submit selected dynamic settings", async () => {
  const bodies: { path: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (!init?.method)
      return response({
        capabilities: {
          ...capabilities,
          defaults: { ...capabilities.defaults, service_tier: "fast" },
        },
      });
    const body = JSON.parse(String(init.body));
    bodies.push({ path, body });
    if (init.method === "PATCH")
      return response({
        project: { ...project, execution_settings: body.execution_settings },
      });
    return response(
      {
        thread,
        command: { id: "start", kind: "thread.start", status: "queued" },
      },
      202,
    );
  };
  let saved: Project | undefined;
  const view = render(
    <ProjectSettingsDialog
      project={project}
      host={host(project.host_id)}
      onClose={() => {}}
      onSaved={(value) => {
        saved = value;
      }}
    />,
  );
  await screen.findByRole("option", { name: "Model B" });
  fireEvent.change(screen.getByRole("combobox", { name: "模型" }), {
    target: { value: "model-b" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存项目默认" }));
  await waitFor(() =>
    assert.equal(saved?.execution_settings?.model, "model-b"),
  );
  assert.equal(bodies[0].path, `/api/projects/${project.id}/settings`);
  assert.equal(
    Object.hasOwn(bodies[0].body.execution_settings as object, "service_tier"),
    false,
  );
  view.unmount();
  render(
    <NewTaskDialog
      hosts={[host(project.host_id)]}
      projects={[saved!]}
      onClose={() => {}}
      onCreated={() => {}}
      onNewProject={() => {}}
    />,
  );
  await screen.findByRole("option", { name: "Model B" });
  assert.equal(
    (screen.getByRole("combobox", { name: "推理强度" }) as HTMLSelectElement)
      .value,
    "medium",
  );
  fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
  await waitFor(() => assert.equal(bodies.length, 2));
  assert.deepEqual(bodies[1].body.execution_settings, {
    model: "model-b",
    effort: "medium",
  });
});

test("pending next-turn settings keep actual settings visible, and failed saves expose errors", async () => {
  globalThis.fetch = async () => response({ capabilities });
  render(
    <ThreadExecutionControls
      thread={{
        ...thread,
        settings_revision: 2,
        effective_settings_revision: 1,
        execution_settings: { model: "model-b" },
        effective_settings: {
          model: "model-a",
          sandbox_mode: "read-only",
          approval_policy: "on-request",
        },
      }}
      host={host("pending-host")}
      onSave={async () => {
        throw new Error("settings revision conflict");
      }}
    />,
  );
  await screen.findByRole("option", { name: "Model B" });
  assert.ok(screen.getByText("已保存，下次执行生效"));
  assert.match(screen.getByText(/^当前：/).textContent || "", /model-a/);
  fireEvent.change(screen.getByRole("combobox", { name: "模型" }), {
    target: { value: "model-a" },
  });
  await screen.findByText("settings revision conflict");
});

test("editing an imported model saves only explicit overrides and leaves granular native approvals inherited", async () => {
  globalThis.fetch = async () => response({ capabilities });
  let saved: ExecutionSettings | undefined;
  render(
    <ThreadExecutionControls
      thread={{
        ...thread,
        execution_settings: { effort: "high" },
        effective_settings: {
          model: "old-custom",
          service_tier: "fast",
          approval_policy: { granular: { sandbox_approval: true } },
          sandbox_mode: "danger-full-access",
          approvals_reviewer: "guardian_subagent",
          network_access: true,
        },
      }}
      host={host("granular-native-host")}
      onSave={async (value) => {
        saved = value;
      }}
    />,
  );
  await screen.findByRole("option", { name: "Model B" });
  fireEvent.change(screen.getByRole("combobox", { name: "模型" }), {
    target: { value: "model-b" },
  });
  await waitFor(() => assert.ok(saved));
  assert.deepEqual({ ...saved }, { model: "model-b", effort: "low" });
  fireEvent.change(screen.getByRole("combobox", { name: "权限" }), {
    target: { value: "workspace-write" },
  });
  await waitFor(() => assert.equal(saved?.sandbox_mode, "workspace-write"));
  assert.equal(saved?.network_access, false);
  assert.equal(saved?.approvals_reviewer, undefined);
});

test("a snapshot begun before a settings patch cannot roll its revision backwards", () => {
  const incoming = {
    ...thread,
    settings_revision: 1,
    effective_settings_revision: 1,
    execution_settings: { model: "model-a" },
    effective_settings: { model: "model-a" },
  };
  const current = {
    ...thread,
    settings_revision: 2,
    effective_settings_revision: 2,
    execution_settings: { model: "model-b" },
    effective_settings: { model: "model-b" },
  };
  const merged = preserveNewerSettings(incoming, current);
  assert.equal(merged.execution_settings?.model, "model-b");
  assert.equal(merged.effective_settings?.model, "model-b");
  assert.equal(merged.settings_revision, 2);
});

test("historical missing modes stay unknown and node defaults only predict new conversations", () => {
  const historical = serviceTierSummary({}, {}, { service_tier: "default" });
  assert.equal(historical.currentKnown, false);
  assert.equal(historical.next, undefined);
  assert.match(historical.text, /当前模式未读取.*节点默认：正常模式（参考）/);
  assert.doesNotMatch(historical.text, /下次：正常模式|当前：正常模式/);
  assert.match(
    serviceTierSummary({}, {}, { service_tier: "priority" }, true).text,
    /新对话模式：快速模式（节点默认）/,
  );
  assert.match(
    serviceTierSummary(
      { service_tier: "default" },
      {},
      { service_tier: "fast" },
    ).text,
    /当前模式未读取 · 下次：正常模式（会话设置）/,
  );
  assert.equal(
    serviceTierSummary(
      { service_tier: null },
      { service_tier: "priority" },
      { service_tier: "default" },
    ).next,
    "priority",
  );
});

test("native priority enables fast while an explicit empty model tier catalog disables it", () => {
  const withTiers: RuntimeCapabilities = {
    ...capabilities,
    service_tiers: ["default", "fast"],
    models: [
      {
        ...capabilities.models[0],
        serviceTiers: [{ id: "priority", name: "Fast" }],
      },
    ],
  };
  assert.deepEqual(availableServiceTiers(withTiers, {}), ["default", "fast"]);
  assert.deepEqual(
    availableServiceTiers(
      {
        ...withTiers,
        models: [
          {
            ...withTiers.models[0],
            serviceTiers: [],
            additionalSpeedTiers: ["fast"],
          },
        ],
      },
      {},
    ),
    ["default"],
  );
  assert.deepEqual(
    availableServiceTiers(
      {
        ...withTiers,
        models: [{ ...capabilities.models[0], additionalSpeedTiers: ["fast"] }],
      },
      {},
    ),
    ["default", "fast"],
  );
});

const writableModes: RuntimeCapabilities = {
  ...capabilities,
  defaults: { ...capabilities.defaults, service_tier: "default" },
  service_tiers: ["default", "fast"],
  supported: { ...capabilities.supported, service_tier_write: true },
};
const modeHost = (id: string): Host => ({
  ...host(id),
  capabilities: ["runtimeSettings", "serviceTierWrite"],
});

test("compact mode changes save next-turn overrides while the actual mode remains visible", async () => {
  const requests: string[] = [];
  const saves: ExecutionSettings[] = [];
  globalThis.fetch = async (input) => {
    requests.push(String(input));
    return response({ capabilities: writableModes });
  };
  function Editor() {
    const [current, setCurrent] = useState<Thread>({
      ...thread,
      effective_settings: { model: "model-a", service_tier: "default" },
      execution_settings: { effort: "high" },
    });
    return (
      <ThreadServiceTierControl
        thread={current}
        host={modeHost("compact-write")}
        onSave={async (settings) => {
          saves.push(settings);
          setCurrent((value) => ({ ...value, execution_settings: settings }));
        }}
      />
    );
  }
  render(<Editor />);
  const picker = await screen.findByRole("combobox", {
    name: "切换 Codex 模式",
  });
  await waitFor(() =>
    assert.equal((picker as HTMLSelectElement).disabled, false),
  );
  fireEvent.change(picker, { target: { value: "fast" } });
  await waitFor(() =>
    assert.deepEqual(saves[0], { effort: "high", service_tier: "fast" }),
  );
  const mode = screen.getByRole("status", { name: "当前 Codex 模式" });
  assert.match(mode.textContent || "", /当前：正常模式 · 下次：快速模式/);
  await waitFor(() =>
    assert.equal((picker as HTMLSelectElement).disabled, false),
  );
  fireEvent.change(picker, { target: { value: "default" } });
  await waitFor(() =>
    assert.deepEqual(saves[1], { effort: "high", service_tier: "default" }),
  );
  assert.match(mode.textContent || "", /当前：正常模式/);
  assert.doesNotMatch(mode.textContent || "", /下次：快速模式/);
  assert.ok(requests.every((path) => path.includes("/capabilities")));
});

test("failed mode saves retain the native mode and do not claim a pending selection", async () => {
  globalThis.fetch = async () => response({ capabilities: writableModes });
  render(
    <ThreadServiceTierControl
      thread={{ ...thread, effective_settings: { service_tier: "priority" } }}
      host={modeHost("mode-save-failure")}
      onSave={async () => {
        throw new Error("cannot save mode");
      }}
    />,
  );
  const picker = screen.getByRole("combobox", {
    name: "切换 Codex 模式",
  }) as HTMLSelectElement;
  await waitFor(() => assert.equal(picker.disabled, false));
  fireEvent.change(picker, { target: { value: "default" } });
  await screen.findByText("cannot save mode");
  assert.equal(picker.value, "fast");
  const mode = screen.getByRole("status", { name: "当前 Codex 模式" });
  assert.match(mode.textContent || "", /当前：快速模式/);
  assert.doesNotMatch(mode.textContent || "", /下次：正常模式/);
});

test("a historical unknown mode starts with preserve-mode placeholder until explicitly chosen", async () => {
  let saved: ExecutionSettings | undefined;
  globalThis.fetch = async () => response({ capabilities: writableModes });
  render(
    <ThreadServiceTierControl
      thread={thread}
      host={modeHost("mode-unknown-pick")}
      onSave={async (settings) => {
        saved = settings;
      }}
    />,
  );
  const picker = screen.getByRole("combobox", {
    name: "切换 Codex 模式",
  }) as HTMLSelectElement;
  await waitFor(() => assert.equal(picker.disabled, false));
  assert.equal(picker.value, "");
  assert.equal(picker.selectedOptions[0].textContent, "沿用会话模式");
  assert.equal(saved, undefined);
  fireEvent.change(picker, { target: { value: "default" } });
  await waitFor(() => assert.deepEqual(saved, { service_tier: "default" }));
});

test("mode editing is disabled offline and on nodes without the write capability", async () => {
  let saves = 0;
  globalThis.fetch = async () => response({ capabilities: writableModes });
  const h = modeHost("mode-compatibility");
  const view = render(
    <ThreadServiceTierControl
      thread={{ ...thread, effective_settings: { service_tier: "fast" } }}
      host={{ ...h, online: false }}
      onSave={async () => {
        saves += 1;
      }}
    />,
  );
  const picker = screen.getByRole("combobox", {
    name: "切换 Codex 模式",
  }) as HTMLSelectElement;
  assert.equal(picker.disabled, true);
  assert.match(picker.parentElement?.title || "", /设备离线/);
  view.rerender(
    <ThreadServiceTierControl
      thread={thread}
      host={{ ...h, capabilities: ["runtimeSettings"] }}
      onSave={async () => {
        saves += 1;
      }}
    />,
  );
  await waitFor(() => assert.equal(picker.disabled, true));
  assert.match(picker.parentElement?.title || "", /更新节点后可切换模式/);
  assert.equal(saves, 0);
});
