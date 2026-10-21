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
import { waitForCommand, waitForImportedThread } from "./commands";
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
