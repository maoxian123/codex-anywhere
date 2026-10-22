import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import ProjectNavigation, {
  type NavigationView,
  type ThreadUpdate,
} from "./ProjectNavigation";
import SidebarMenu from "./SidebarMenu";
import type { Host, Project, Thread } from "./types";

afterEach(cleanup);
const host: Host = {
  id: "node",
  name: "工作电脑",
  platform: "windows",
  online: true,
};
const project: Project = {
  id: "project",
  host_id: host.id,
  name: "当前项目",
  cwd: "C:\\work",
  thread_count: 2,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};
function thread(
  id: string,
  changes: Partial<Thread> & ThreadUpdate = {},
): Thread & ThreadUpdate {
  return {
    id,
    host_id: host.id,
    project_id: project.id,
    title: id,
    cwd: project.cwd,
    status: "idle",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...changes,
  };
}
const defaults = {
  hosts: [host],
  projects: [project],
  threads: [] as Thread[],
  selectedProjectId: null as string | null,
  currentThreadId: "",
  loading: false,
  loadingMore: false,
  hasMore: false,
  onFilter: (_id: string | null) => {},
  onNavigate: (_id: string) => {},
  onNewProject: () => {},
  onRefresh: () => {},
  onMore: () => {},
};

test("project collapse, filtering and scoped new conversations are independent actions", () => {
  const filters: (string | null)[] = [];
  const created: string[] = [];
  const props = {
    ...defaults,
    threads: [thread("项目中的任务")],
    onFilter: (id: string | null) => filters.push(id),
    onNewConversation: (value: Project) => created.push(value.id),
  };
  const { rerender } = render(<ProjectNavigation {...props} />);
  const group = screen.getByRole("region", { name: "当前项目 项目会话" });
  fireEvent.click(within(group).getByRole("button", { name: "收起会话" }));
  assert.equal(within(group).queryByRole("link"), null);
  assert.deepEqual(filters, []);
  fireEvent.click(
    within(group).getByRole("button", { name: "在 当前项目 新建对话" }),
  );
  assert.deepEqual(created, [project.id]);
  fireEvent.click(within(group).getByRole("button", { name: /^当前项目C:/ }));
  assert.deepEqual(filters, [project.id]);
  rerender(<ProjectNavigation {...props} selectedProjectId={project.id} />);
  assert.ok(screen.getByText("正在筛选：当前项目"));
  fireEvent.click(screen.getByRole("button", { name: "返回全部会话" }));
  assert.deepEqual(filters, [project.id, null]);
  fireEvent.click(within(group).getByRole("button", { name: "展开会话" }));
  assert.ok(within(group).getByRole("link", { name: /项目中的任务/ }));
});

test("recent, active, waiting and archived views expose only matching conversations", () => {
  const views: NavigationView[] = [];
  render(
    <ProjectNavigation
      {...defaults}
      threads={[
        thread("普通会话"),
        thread("正在执行", { status: "active" }),
        thread("启动会话", { status: "starting" }),
        thread("需要审批", { status: "waiting" }),
        thread("已归档任务", { archived: true, status: "active" }),
      ]}
      onViewChange={(view) => views.push(view)}
    />,
  );
  assert.equal(screen.getAllByRole("link").length, 4);
  assert.equal(screen.queryByRole("link", { name: /已归档任务/ }), null);
  fireEvent.click(screen.getByRole("button", { name: "运行中" }));
  assert.deepEqual(
    screen
      .getAllByRole("link")
      .map((link) => link.querySelector("strong")?.textContent),
    ["正在执行", "启动会话"],
  );
  fireEvent.click(screen.getByRole("button", { name: "待处理" }));
  assert.equal(screen.getAllByRole("link").length, 1);
  assert.ok(screen.getByRole("link", { name: /需要审批/ }));
  assert.ok(screen.getByText("等待你处理"));
  fireEvent.click(screen.getByRole("button", { name: "已归档" }));
  assert.ok(screen.getByRole("link", { name: /已归档任务/ }));
  fireEvent.click(screen.getByRole("button", { name: "最近" }));
  assert.equal(screen.getAllByRole("link").length, 4);
  assert.deepEqual(views, ["active", "waiting", "archived", "all"]);
});

test("pinned conversations come first without mutating input and menu changes are persisted explicitly", async () => {
  const threads = [
    thread("普通任务"),
    thread("原始标题", { pinned: true, display_title: "我的任务" }),
  ];
  const updates: { id: string; changes: ThreadUpdate }[] = [];
  const { container, rerender } = render(
    <ProjectNavigation
      {...defaults}
      threads={threads}
      onThreadUpdate={async (value, changes) => {
        updates.push({ id: value.id, changes });
      }}
    />,
  );
  assert.deepEqual(
    screen
      .getAllByRole("link")
      .map((link) => link.querySelector("strong")?.textContent),
    ["我的任务", "普通任务"],
  );
  assert.equal(threads[0].id, "普通任务");
  fireEvent.click(screen.getByLabelText("我的任务 会话操作"));
  assert.equal(
    container.querySelector(".thread-menu-popover"),
    null,
    "menu floats outside the scrolling sidebar",
  );
  fireEvent.click(screen.getByRole("button", { name: "取消置顶" }));
  await waitFor(() =>
    assert.deepEqual(updates, [{ id: "原始标题", changes: { pinned: false } }]),
  );
  await waitFor(() =>
    assert.ok(!screen.queryByRole("button", { name: "取消置顶" })),
  );
  fireEvent.click(screen.getByLabelText("我的任务 会话操作"));
  fireEvent.click(screen.getByRole("button", { name: "重命名" }));
  const dialog = screen.getByRole("dialog", { name: "重命名会话" });
  const nameInput = within(dialog).getByRole("textbox", {
    name: "会话名称",
  }) as HTMLInputElement;
  assert.ok(document.activeElement === nameInput);
  assert.equal(nameInput.selectionStart, 0);
  assert.equal(nameInput.selectionEnd, nameInput.value.length);
  fireEvent.change(within(dialog).getByRole("textbox", { name: "会话名称" }), {
    target: { value: "  新名字  " },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
  await waitFor(() => assert.ok(!screen.queryByRole("dialog")));
  assert.deepEqual(updates[1], {
    id: "原始标题",
    changes: { display_title: "新名字" },
  });
  rerender(
    <ProjectNavigation
      {...defaults}
      view="archived"
      threads={[thread("归档任务", { archived: true })]}
      onThreadUpdate={async (value, changes) => {
        updates.push({ id: value.id, changes });
      }}
    />,
  );
  fireEvent.click(screen.getByLabelText("归档任务 会话操作"));
  fireEvent.click(screen.getByRole("button", { name: "恢复会话" }));
  await waitFor(() =>
    assert.deepEqual(updates[2], {
      id: "归档任务",
      changes: { archived: false },
    }),
  );
});

test("rename failures keep the dialog and draft available for retry", async () => {
  let calls = 0;
  render(
    <ProjectNavigation
      {...defaults}
      threads={[thread("重命名任务")]}
      onThreadUpdate={async () => {
        calls += 1;
        if (calls === 1) throw new Error("连接中断");
      }}
    />,
  );
  fireEvent.click(screen.getByLabelText("重命名任务 会话操作"));
  fireEvent.click(screen.getByRole("button", { name: "重命名" }));
  fireEvent.change(screen.getByRole("textbox", { name: "会话名称" }), {
    target: { value: "保留的名称" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() =>
    assert.equal(screen.getByRole("alert").textContent, "连接中断"),
  );
  assert.equal(
    (screen.getByRole("textbox", { name: "会话名称" }) as HTMLInputElement)
      .value,
    "保留的名称",
  );
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => assert.ok(!screen.queryByRole("dialog")));
  assert.equal(calls, 2);
});

test("archive explains that running work continues and requests only presentation changes", async () => {
  const updates: ThreadUpdate[] = [];
  render(
    <ProjectNavigation
      {...defaults}
      threads={[thread("运行任务", { status: "active" })]}
      onThreadUpdate={async (_thread, changes) => {
        updates.push(changes);
      }}
    />,
  );
  fireEvent.click(screen.getByLabelText("运行任务 会话操作"));
  assert.ok(screen.getByText("归档后，正在运行的任务会继续执行。"));
  fireEvent.click(screen.getByRole("button", { name: "归档会话" }));
  await waitFor(() => assert.deepEqual(updates, [{ archived: true }]));
});

test("footer exposes settings directly and retains device health and history actions", () => {
  let settings = 0;
  let history = 0;
  render(
    <SidebarMenu
      hosts={[{ ...host, sync_error: "未能同步" }]}
      onPair={() => {}}
      onHistory={() => {
        history += 1;
      }}
      onSettings={() => {
        settings += 1;
      }}
      onAbout={() => {}}
      onLogout={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  assert.equal(settings, 1);
  assert.equal(screen.queryByText("同步失败：未能同步"), null);
  fireEvent.click(screen.getByLabelText("工作设备"));
  assert.ok(screen.getByText("同步失败：未能同步"));
  assert.ok(screen.getByText("1 在线"));
  fireEvent.click(screen.getByRole("button", { name: "更多本机历史" }));
  assert.equal(history, 1);
  assert.equal(screen.queryByText("同步失败：未能同步"), null);
});

for (const [view, status] of [
  ["active", "active"],
  ["waiting", "waiting"],
  ["archived", "idle"],
] as const) {
  test(`${view} view omits unrelated projects and devices and counts only matching conversations`, () => {
    const otherHost = { ...host, id: "other-node", name: "另一台电脑" };
    const otherProject = {
      ...project,
      id: "other-project",
      name: "无关项目",
      host_id: otherHost.id,
    };
    const { container } = render(
      <ProjectNavigation
        {...defaults}
        view={view}
        hosts={[host, otherHost]}
        projects={[
          { ...project, thread_count: 99 },
          { ...project, id: "empty-project", name: "空项目", cwd: "C:\\empty" },
          otherProject,
        ]}
        threads={[
          thread("匹配任务", { status, archived: view === "archived" }),
          thread("无关任务", {
            host_id: otherHost.id,
            project_id: otherProject.id,
          }),
        ]}
      />,
    );
    assert.ok(screen.getByRole("region", { name: "当前项目 项目会话" }));
    assert.ok(!screen.queryByRole("region", { name: "空项目 项目会话" }));
    assert.ok(!screen.queryByRole("region", { name: "无关项目 项目会话" }));
    assert.ok(!screen.queryByRole("region", { name: "另一台电脑 会话" }));
    assert.equal(container.querySelectorAll(".host-thread-heading").length, 1);
    assert.equal(screen.getByTitle("此视图已加载 1 个会话").textContent, "1");
    assert.ok(!screen.queryByText("99"));
    assert.ok(!screen.queryByText("当前视图没有会话"));
  });
}

test("zero-match views show one empty state while preserving pagination without empty device headings", () => {
  const requested: string[] = [];
  const otherHost = { ...host, id: "other-node", name: "另一台电脑" };
  const { container, rerender } = render(
    <ProjectNavigation
      {...defaults}
      view="waiting"
      hosts={[host, otherHost]}
      threads={[thread("普通任务")]}
      hostPages={{
        [host.id]: { hasMore: true, loading: false },
        [otherHost.id]: { hasMore: false, loading: false },
      }}
      onHostMore={(id) => requested.push(id)}
    />,
  );
  assert.equal(
    screen.getByRole("status").textContent,
    "没有等待处理的会话当前已加载的会话中暂无匹配项，可继续加载更多。",
  );
  assert.equal(
    container.querySelectorAll(".machine-project-group, .host-thread-heading")
      .length,
    0,
  );
  assert.ok(!screen.queryByRole("region", { name: "另一台电脑 会话" }));
  fireEvent.click(
    screen.getByRole("button", { name: "加载 工作电脑 的更多会话" }),
  );
  assert.deepEqual(requested, [host.id]);
  rerender(
    <ProjectNavigation
      {...defaults}
      hosts={[]}
      projects={[]}
      view="archived"
    />,
  );
  assert.equal(screen.getAllByRole("status").length, 1);
  assert.equal(screen.getByRole("status").textContent, "没有已归档的会话");
  assert.ok(!screen.queryByText("这里还没有会话"));
});

test("a filtered project's zero-match view retains its legacy load-more action", () => {
  let more = 0;
  render(
    <ProjectNavigation
      {...defaults}
      selectedProjectId={project.id}
      view="active"
      hasMore
      onMore={() => {
        more += 1;
      }}
    />,
  );
  assert.ok(screen.getByText("没有正在运行的会话"));
  fireEvent.click(
    screen.getByRole("button", { name: "加载 工作电脑 的更多会话" }),
  );
  assert.equal(more, 1);
});
