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
import { ProjectDialog } from "./Projects";
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
import type { Host, Project, Thread } from "./types";

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

test("sidebar renders recent device and project groups and keeps dated conversations navigable", () => {
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
  fireEvent.click(screen.getByText("项目", { selector: "summary" }));
  assert.deepEqual(
    Array.from(container.querySelectorAll(".project-host-group > h3 span")).map(
      (value) => value.textContent,
    ),
    ["Linux 工作电脑", "Windows 工作电脑"],
  );
  assert.deepEqual(
    Array.from(container.querySelectorAll(".project-host-group")).map((group) =>
      Array.from(group.querySelectorAll(".project-row strong")).map(
        (value) => value.textContent,
      ),
    ),
    [["最近项目", "更早项目"], ["较早项目"]],
  );
  assert.deepEqual(
    Array.from(container.querySelectorAll(".thread-date-label")).map(
      (value) => value.textContent,
    ),
    ["今天", "更早"],
  );
  const links = screen.getAllByRole("link");
  assert.deepEqual(
    links.map((link) => link.getAttribute("href")),
    ["/tasks/新会话", "/tasks/旧会话"],
  );
  fireEvent.click(links[1]);
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

test("new tasks use the chosen project ID and never send a directory allowlist", async () => {
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
  fireEvent.change(screen.getByRole("textbox", { name: "任务名称" }), {
    target: { value: "project task" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
  await waitFor(() => assert.ok(body));
  assert.deepEqual(body, {
    host_id: "h1",
    project_id: "p1",
    title: "project task",
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
