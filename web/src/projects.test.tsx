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
import { desktopRegistrationMessage, ProjectDialog } from "./Projects";
import { ProjectSettingsDialog } from "./ExecutionControls";
import { Modal } from "./components";
import { ImportDialog } from "./Imports";
import { NewTaskDialog } from "./Dialogs";
import ProjectNavigation from "./ProjectNavigation";
import { waitForCommand, waitForImportedThread } from "./commands";
import {
  groupThreadsByDate,
  sortProjectsByActivity,
  threadActivityLabel,
  threadActivityTime,
} from "./sidebar-activity";
import type { DesktopRegistration, Host, Project, Thread } from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});
const hosts: Host[] = [
  { id: "h1", name: "Windows 工作电脑", platform: "windows", online: true },
  { id: "h2", name: "Linux 工作电脑", platform: "linux", online: true },
];
const project: Project = {
  id: "p1",
  host_id: "h1",
  name: "work",
  cwd: "C:\\work",
  thread_count: 0,
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
};
const listing = (path = "C:\\work") => ({
  path,
  parent: "C:\\",
  home: "C:\\Users\\tester",
  locations: [
    { name: "C:", path: "C:\\" },
    { name: "主目录", path: "C:\\Users\\tester" },
  ],
  directories: [{ name: "existing", path: "C:\\work\\existing" }],
  truncated: false,
});
const command = (result: unknown, id = "c1") =>
  new Response(
    JSON.stringify({
      command: { id, kind: "test", status: "completed", result },
    }),
    { status: 202 },
  );

const sidebarThread = (
  id: string,
  updatedAt: Date,
  extra: Partial<Thread> = {},
): Thread => ({
  id,
  host_id: "h1",
  project_id: "p1",
  title: id,
  cwd: "C:\\work",
  status: "idle",
  created_at: updatedAt.toISOString(),
  updated_at: updatedAt.toISOString(),
  ...extra,
});

test("sidebar groups conversations by local calendar date and native activity, without mutating input", () => {
  const now = new Date(2026, 9, 21, 0, 15);
  const yesterday = new Date(2026, 9, 20, 23, 58);
  const week = new Date(2026, 9, 14, 23, 0);
  const old = new Date(2026, 9, 13, 23, 0);
  const threads = [
    sidebarThread("imported-old", now, {
      imported: true,
      native_updated_at: old.getTime() / 1000,
    }),
    sidebarThread("yesterday", yesterday),
    sidebarThread("week", week),
    sidebarThread("today-first", new Date(2026, 9, 21, 0, 1)),
    sidebarThread("today-latest", now),
  ];
  const originalIds = threads.map((thread) => thread.id);
  assert.deepEqual(
    groupThreadsByDate(threads, now).map((group) => ({
      label: group.label,
      ids: group.threads.map((thread) => thread.id),
    })),
    [
      { label: "今天", ids: ["today-latest", "today-first"] },
      { label: "昨天", ids: ["yesterday"] },
      { label: "过去7天", ids: ["week"] },
      { label: "更早", ids: ["imported-old"] },
    ],
  );
  assert.equal(threadActivityLabel(threads[1], now), "昨天");
  assert.deepEqual(
    threads.map((thread) => thread.id),
    originalIds,
  );
});

test("sidebar prefers projected live activity and falls back safely when native metadata is missing", () => {
  const today = new Date(2026, 9, 21, 12);
  const native = new Date(2026, 9, 12, 12);
  const imported = sidebarThread("imported", today, {
    imported: true,
    native_updated_at: native.getTime() / 1000,
  });
  assert.equal(threadActivityTime(imported), native.getTime());
  assert.equal(
    threadActivityTime({ ...imported, last_activity_at: today.toISOString() }),
    today.getTime(),
  );
  assert.equal(
    threadActivityTime({ ...imported, native_updated_at: null }),
    today.getTime(),
  );
  assert.equal(
    threadActivityTime({ ...imported, native_updated_at: Number.NaN }),
    today.getTime(),
  );
});

test("sidebar preserves server project ranking across partial pages and promotes new loaded activity", () => {
  const projects: Project[] = [
    {
      ...project,
      id: "server-first",
      updated_at: "2026-10-01T00:00:00Z",
    },
    {
      ...project,
      id: "server-second",
      updated_at: "2026-10-05T00:00:00Z",
    },
  ];
  assert.deepEqual(
    sortProjectsByActivity(projects, []).map((value) => value.id),
    ["server-first", "server-second"],
  );
  const active = sidebarThread("active", new Date("2026-10-21T00:00:00Z"), {
    project_id: "server-second",
  });
  assert.deepEqual(
    sortProjectsByActivity(projects, [active]).map((value) => value.id),
    ["server-second", "server-first"],
  );
  assert.deepEqual(
    projects.map((value) => value.id),
    ["server-first", "server-second"],
  );
});

test("sidebar groups conversations by machine and project while preserving supplied ordering", () => {
  const now = new Date();
  const earlier = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() - 10,
  );
  const projects: Project[] = [
    { ...project, id: "latest", host_id: "h2", name: "最近项目" },
    { ...project, id: "older", host_id: "h1", name: "较早项目" },
    { ...project, id: "oldest", host_id: "h2", name: "更早项目" },
  ];
  const navigated: string[] = [];
  const { container } = render(
    <ProjectNavigation
      hosts={hosts}
      projects={projects}
      threads={[
        sidebarThread("旧会话", now, {
          imported: true,
          native_updated_at: earlier.getTime() / 1000,
        }),
        sidebarThread("新会话", now),
      ]}
      selectedProjectId={null}
      currentThreadId="新会话"
      loading={false}
      loadingMore={false}
      hasMore={false}
      onFilter={() => {}}
      onNavigate={(id) => navigated.push(id)}
      onNewProject={() => {}}
      onRefresh={() => {}}
      onMore={() => {}}
    />,
  );
  assert.deepEqual(
    Array.from(
      container.querySelectorAll(".host-thread-heading > h3 span"),
    ).map((value) => value.textContent),
    ["Windows 工作电脑", "Linux 工作电脑"],
  );
  assert.deepEqual(
    Array.from(container.querySelectorAll(".host-thread-group")).map((group) =>
      Array.from(group.querySelectorAll(".project-row strong")).map(
        (value) => value.textContent,
      ),
    ),
    [["较早项目"], ["最近项目", "更早项目"]],
  );
  assert.equal(container.querySelectorAll(".thread-date-label").length, 0);
  const links = screen.getAllByRole("link");
  assert.deepEqual(
    links.map((link) => link.getAttribute("href")),
    ["/tasks/旧会话", "/tasks/新会话"],
  );
  fireEvent.click(links[0]);
  assert.deepEqual(navigated, ["旧会话"]);
});

test("remote directory picker browses the selected node and creates a folder project only on request", async () => {
  const requests: { path: string; data: Record<string, unknown> }[] = [];
  let created: Project | undefined;
  globalThis.fetch = async (input, init) => {
    const path = String(input),
      data = JSON.parse(String(init?.body));
    requests.push({ path, data });
    if (path === "/api/hosts/h1/directories")
      return command(
        listing(typeof data.path === "string" ? data.path : undefined),
      );
    assert.equal(path, "/api/projects");
    return command({
      project: { ...project, cwd: data.cwd, name: "new-folder" },
    });
  };
  render(
    <ProjectDialog
      hosts={hosts}
      onClose={() => {}}
      onCreated={(value) => {
        created = value;
      }}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "existing" }));
  await waitFor(() =>
    assert.ok(
      requests.some((value) => value.data.path === "C:\\work\\existing"),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "新建文件夹" }));
  fireEvent.change(screen.getByRole("textbox", { name: "新文件夹名称" }), {
    target: { value: "new-folder" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建并用作项目" }));
  await waitFor(() => assert.ok(created));
  assert.equal(created!.cwd, "C:\\work\\existing\\new-folder");
  assert.deepEqual(
    requests.find((value) => value.path === "/api/projects")?.data,
    {
      host_id: "h1",
      cwd: "C:\\work\\existing\\new-folder",
      create_directory: true,
    },
  );
});

test("desktop registration messages describe outcomes without exposing node state details", () => {
  assert.equal(
    desktopRegistrationMessage({ status: "pending", reason: "state_changed" }),
    "已排队，节点会在项目列表稳定后自动完成登记。",
  );
  assert.equal(
    desktopRegistrationMessage({
      status: "unavailable",
      reason: "desktop_not_found",
    }),
    "该设备未检测到桌面项目列表，项目仍可在 Anywhere 使用。",
  );
  assert.equal(
    desktopRegistrationMessage({
      status: "unavailable",
      reason: "unsupported_state",
    }),
    "该设备的桌面版本暂不兼容，请更新 Codex 桌面版后重试。",
  );
  assert.match(
    desktopRegistrationMessage({
      status: "registered",
      refresh_required: true,
    }),
    /该设备的 Codex 项目列表.*重新打开桌面版/,
  );
  assert.match(
    desktopRegistrationMessage({ status: "already_registered" }),
    /已登记到该设备/,
  );
  assert.match(
    desktopRegistrationMessage({ status: "pending" }),
    /桌面版正在运行.*已排队.*退出桌面版后节点会自动完成登记/,
  );
  assert.match(
    desktopRegistrationMessage({
      status: "pending",
      reason: "desktop_state_unverified",
    }),
    /已排队.*确认桌面版退出后.*自动完成登记/,
  );
  assert.match(
    desktopRegistrationMessage({ status: "error", reason: "state_changed" }),
    /项目列表已变化.*手动同步/,
  );
  assert.match(
    desktopRegistrationMessage({ status: "error", reason: "state_invalid" }),
    /项目列表暂不可用.*手动重试/,
  );
  assert.match(
    desktopRegistrationMessage({ status: "error", reason: "outcome_unknown" }),
    /尚未确认.*手动同步/,
  );
  assert.match(
    desktopRegistrationMessage({
      status: "unavailable",
      reason: "native_unsupported",
    }),
    /更新 Codex/,
  );
  assert.match(
    desktopRegistrationMessage({
      status: "unavailable",
      reason: "runtime_unavailable",
    }),
    /暂不可用.*手动重试/,
  );
  assert.match(desktopRegistrationMessage(), /更新节点/);
  for (const status of ["error", "unavailable"] as const) {
    const message = desktopRegistrationMessage({
      status,
      reason: "state_write_failed: C:/private/node/state.json",
    });
    assert.doesNotMatch(message, /state_write_failed|private|state\.json/);
  }
});

for (const registration of [
  { status: "registered", refresh_required: true },
  { status: "already_registered" },
  { status: "pending" },
  { status: "unavailable", reason: "native_unsupported" },
  { status: "error", reason: "private_failure" },
  undefined,
] satisfies (DesktopRegistration | undefined)[]) {
  test(`project creation forwards ${registration?.status || "legacy node"} registration without failing project creation`, async () => {
    let created:
      { value: Project; registration?: DesktopRegistration } | undefined;
    let creates = 0;
    globalThis.fetch = async (input) => {
      if (String(input) === "/api/hosts/h1/directories")
        return command(listing());
      assert.equal(String(input), "/api/projects");
      creates++;
      return command({
        project,
        ...(registration ? { desktop_registration: registration } : {}),
      });
    };
    render(
      <ProjectDialog
        hosts={[{ ...hosts[0], capabilities: ["desktopProjects"] }]}
        onClose={() => {}}
        onCreated={(value, result) => {
          created = { value, registration: result };
        }}
      />,
    );
    await screen.findByRole("button", { name: "existing" });
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));
    await waitFor(() => assert.ok(created));
    assert.equal(created!.value.id, project.id);
    assert.deepEqual(created!.registration, registration);
    assert.equal(screen.queryByRole("alert"), null);
    assert.equal(creates, 1);
  });
}

test("existing project desktop sync is explicit, stays open and blocks duplicate clicks and closing while pending", async () => {
  let resolve!: (value: Response) => void;
  const requests: { path: string; method?: string; body: unknown }[] = [];
  let closed = 0;
  globalThis.fetch = async (input, init) => {
    requests.push({
      path: String(input),
      method: init?.method,
      body: JSON.parse(String(init?.body)),
    });
    return new Promise<Response>((accept) => {
      resolve = accept;
    });
  };
  render(
    <ProjectSettingsDialog
      project={project}
      host={{ ...hosts[0], capabilities: ["desktopProjects"] }}
      onClose={() => {
        closed++;
      }}
      onSaved={() => {
        throw new Error("must not save settings");
      }}
    />,
  );
  assert.equal(requests.length, 0);
  const sync = screen.getByRole("button", { name: "同步到桌面版" });
  fireEvent.click(sync);
  fireEvent.click(sync);
  assert.deepEqual(requests, [
    { path: "/api/projects/p1/desktop", method: "POST", body: {} },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "关闭窗口" }));
  assert.equal(closed, 0);
  const dialog = screen.getByRole("dialog") as HTMLDialogElement;
  const escape = new Event("cancel", { cancelable: true });
  fireEvent(dialog, escape);
  if (!escape.defaultPrevented) dialog.close();
  assert.equal(escape.defaultPrevented, true);
  assert.equal(dialog.hasAttribute("open"), true);
  assert.equal(closed, 0);
  assert.equal(
    (screen.getByRole("button", { name: "取消" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  await act(async () => {
    resolve(
      command({
        cwd: project.cwd,
        desktop_registration: {
          status: "already_registered",
          refresh_required: true,
        },
      }),
    );
  });
  await screen.findByText(/已登记到该设备.*重新打开桌面版/);
  assert.equal(closed, 0);
  assert.ok(screen.getByRole("dialog").hasAttribute("open"));
  assert.equal(requests.length, 1);
  const completedEscape = new Event("cancel", { cancelable: true });
  fireEvent(dialog, completedEscape);
  assert.equal(completedEscape.defaultPrevented, false);
  assert.equal(closed, 1);
});

test("ordinary dialogs retain native Escape close behavior without an explicit guard", () => {
  let closed = 0;
  render(
    <Modal
      title="普通弹窗"
      onClose={() => {
        closed++;
      }}
    >
      <p>内容</p>
    </Modal>,
  );
  const dialog = screen.getByRole("dialog") as HTMLDialogElement;
  const escape = new Event("cancel", { cancelable: true });
  fireEvent(dialog, escape);
  if (!escape.defaultPrevented) dialog.close();
  assert.equal(escape.defaultPrevented, false);
  assert.equal(closed, 1);
  assert.equal(dialog.hasAttribute("open"), false);
});

for (const owner of [
  { ...hosts[0], online: false, capabilities: ["desktopProjects"] },
  { ...hosts[0], capabilities: [] },
]) {
  test(`existing project desktop sync requires ${owner.online ? "node support" : "online device"}`, () => {
    let requests = 0;
    globalThis.fetch = async () => {
      requests++;
      throw new Error("must not send");
    };
    render(
      <ProjectSettingsDialog
        project={project}
        host={owner}
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );
    const sync = screen.getByRole("button", {
      name: "同步到桌面版",
    }) as HTMLButtonElement;
    assert.equal(sync.disabled, true);
    assert.ok(
      screen.getByText(
        owner.online
          ? "更新节点后支持同步到桌面版。"
          : "设备上线后可同步到桌面版。",
      ),
    );
    fireEvent.click(sync);
    assert.equal(requests, 0);
  });
}

test("desktop sync failures show safe inline feedback and retry only on another explicit click", async () => {
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    return command({
      cwd: project.cwd,
      desktop_registration:
        requests === 1
          ? {
              status: "error",
              reason: "state_write_failed: /private/state.json",
            }
          : { status: "registered" },
    });
  };
  const view = render(
    <ProjectSettingsDialog
      project={project}
      host={{ ...hosts[0], capabilities: ["desktopProjects"] }}
      onClose={() => {}}
      onSaved={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "同步到桌面版" }));
  await screen.findByText(/同步到桌面版失败/);
  assert.doesNotMatch(
    view.container.textContent || "",
    /private|state_write_failed|state\.json/,
  );
  assert.equal(requests, 1);
  fireEvent.click(screen.getByRole("button", { name: "同步到桌面版" }));
  await screen.findByText(/已登记到该设备/);
  assert.equal(requests, 2);
});

test("unknown desktop sync outcomes are not automatically retried or exposed as raw errors", async () => {
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    return new Response(
      JSON.stringify({
        command: {
          id: "uncertain-sync",
          kind: "desktop.project",
          status: "unknown",
          error: "C:/private/state.json",
        },
      }),
      { status: 202 },
    );
  };
  const view = render(
    <ProjectSettingsDialog
      project={project}
      host={{ ...hosts[0], capabilities: ["desktopProjects"] }}
      onClose={() => {}}
      onSaved={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "同步到桌面版" }));
  await screen.findByText(/登记结果尚未确认/);
  assert.equal(requests, 1);
  assert.doesNotMatch(view.container.textContent || "", /private|state\.json/);
});

test("changing nodes ignores a late directory response from the previous filesystem", async () => {
  let resolveOld!: (value: Response) => void;
  const old = new Promise<Response>((resolve) => {
    resolveOld = resolve;
  });
  globalThis.fetch = async (input) =>
    String(input) === "/api/hosts/h1/directories"
      ? old
      : command({
          ...listing("/home/linux"),
          home: "/home/linux",
          parent: "/home",
          directories: [{ name: "linux-project", path: "/home/linux/project" }],
        });
  render(
    <ProjectDialog hosts={hosts} onClose={() => {}} onCreated={() => {}} />,
  );
  fireEvent.change(screen.getByRole("combobox", { name: "工作设备" }), {
    target: { value: "h2" },
  });
  await screen.findByRole("button", { name: "linux-project" });
  await act(async () => {
    resolveOld(command(listing("C:\\old")));
    await old;
  });
  assert.equal(
    (
      screen.getByRole("textbox", {
        name: /设备上的绝对目录/,
      }) as HTMLInputElement
    ).value,
    "/home/linux",
  );
});

test("new tasks need no name and use the chosen project without sending a title or directory allowlist", async () => {
  let body: Record<string, unknown> | undefined;
  globalThis.fetch = async (_input, init) => {
    body = JSON.parse(String(init?.body));
    return new Response(
      JSON.stringify({
        thread: { id: "t1", project_id: "p1" },
        command: { id: "c1", status: "queued" },
      }),
      { status: 202 },
    );
  };
  render(
    <NewTaskDialog
      hosts={hosts}
      projects={[project]}
      initialProjectId="p1"
      onClose={() => {}}
      onCreated={() => {}}
      onNewProject={() => {}}
    />,
  );
  assert.equal(screen.queryByRole("textbox", { name: "任务名称" }), null);
  fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
  await waitFor(() => assert.ok(body));
  assert.deepEqual(body, {
    host_id: "h1",
    project_id: "p1",
  });
});

test("history catalog paginates metadata and imports only sessions selected across pages", async () => {
  const imported: string[] = [];
  const catalogRequests: Record<string, unknown>[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path.startsWith("/api/threads/hub-"))
      return new Response(
        JSON.stringify({
          thread: {
            id: path.split("?")[0].split("/").at(-1),
            status: "idle",
            history_loaded: true,
            imported: true,
          },
        }),
        { status: 200 },
      );
    const body = JSON.parse(String(init?.body));
    if (path.endsWith("/catalog")) {
      catalogRequests.push(body);
      return command(
        body.cursor
          ? {
              data: [
                { id: "native-b", name: "第二页会话", cwd: "C:\\second" },
                { id: "native-c", name: "不导入的会话", cwd: "C:\\third" },
              ],
              nextCursor: null,
            }
          : {
              data: [{ id: "native-a", name: "第一页会话", cwd: "C:\\first" }],
              nextCursor: "next-page",
            },
      );
    }
    assert.equal(path, "/api/hosts/h1/imports");
    imported.push(body.native_thread_id);
    return new Response(
      JSON.stringify({
        thread: {
          id: `hub-${body.native_thread_id}`,
          status: "importing",
          history_loaded: false,
          imported: true,
        },
        command: {
          id: `import-${body.native_thread_id}`,
          status: "completed",
          result: { itemCount: 1 },
        },
      }),
      { status: 202 },
    );
  };
  render(
    <ImportDialog
      hosts={hosts}
      onClose={() => {}}
      onImported={(_thread: Thread) => {}}
      onNavigate={() => {}}
    />,
  );
  await screen.findByText("第一页会话");
  assert.equal(imported.length, 0);
  fireEvent.click(screen.getByRole("checkbox", { name: "选择当前页" }));
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByText("第二页会话");
  fireEvent.click(screen.getByRole("checkbox", { name: /第二页会话/ }));
  fireEvent.click(screen.getByRole("button", { name: "导入所选（2）" }));
  await waitFor(() => assert.equal(imported.length, 2));
  assert.deepEqual(imported, ["native-a", "native-b"]);
  assert.equal(catalogRequests[1].cursor, "next-page");
});

test("unknown command outcomes are not polled or submitted again automatically", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    throw new Error("unexpected retry");
  };
  await assert.rejects(
    waitForCommand({
      id: "uncertain",
      kind: "thread.import",
      status: "unknown",
    }),
    /未知/,
  );
  assert.equal(calls, 0);
});

test("import completion waits for hub history projection and returns its latest thread", async () => {
  let reads = 0;
  globalThis.fetch = async (input) => {
    assert.equal(String(input), "/api/threads/imported-task?item_limit=1");
    reads += 1;
    return new Response(
      JSON.stringify({
        thread: {
          id: "imported-task",
          status: reads === 1 ? "importing" : "idle",
          history_loaded: reads > 1,
          title: reads === 1 ? "placeholder" : "真实原会话标题",
        },
      }),
      { status: 200 },
    );
  };
  const thread = await waitForImportedThread("imported-task");
  assert.equal(reads, 2);
  assert.equal(thread.history_loaded, true);
  assert.equal(thread.title, "真实原会话标题");
});

test("an expired import reports its error instead of offering to open a partial history", async () => {
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        thread: {
          id: "t1",
          status: "error",
          history_loaded: false,
          last_error: "History unavailable",
        },
      }),
      { status: 200 },
    );
  await assert.rejects(waitForImportedThread("t1"), /History unavailable/);
});
