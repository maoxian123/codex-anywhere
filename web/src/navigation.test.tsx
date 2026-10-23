import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import ProjectNavigation from "./ProjectNavigation";
import type { Host, Project, Thread } from "./types";

afterEach(cleanup);
const hosts: Host[] = [
  { id: "linux", name: "Linux 节点", platform: "linux", online: true },
  { id: "windows", name: "Windows 节点", platform: "windows", online: false },
  { id: "empty", name: "空节点", platform: "linux", online: false },
];
function thread(
  id: string,
  hostId = "linux",
  extra: Partial<Thread> = {},
): Thread {
  return {
    id,
    host_id: hostId,
    title: id,
    cwd: "/work",
    status: "idle",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...extra,
  };
}
function project(
  id: string,
  hostId = "linux",
  extra: Partial<Project> = {},
): Project {
  return {
    id,
    host_id: hostId,
    name: id,
    cwd: "/work",
    thread_count: 2,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...extra,
  };
}
const defaults = {
  hosts,
  projects: [] as Project[],
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

test("all sessions group by stable node order, include orphan nodes and show loaded counts independently", () => {
  const navigated: string[] = [];
  const { container } = render(
    <ProjectNavigation
      {...defaults}
      threads={[
        thread("Windows 会话", "windows"),
        thread("Linux 第一条"),
        thread("孤立会话", "missing"),
        thread("Linux 第二条"),
      ]}
      onNavigate={(id) => navigated.push(id)}
    />,
  );
  assert.deepEqual(
    Array.from(container.querySelectorAll(".host-thread-heading h3")).map(
      (node) => node.textContent,
    ),
    ["Linux 节点", "Windows 节点", "空节点", "missing"],
  );
  const linux = screen.getByRole("region", { name: "Linux 节点 会话" });
  assert.equal(
    within(linux).getByTitle("当前已加载 2 个会话").textContent,
    "2",
  );
  assert.ok(within(linux).getByRole("img", { name: "在线" }));
  assert.deepEqual(
    within(linux)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href")),
    ["/tasks/Linux 第一条", "/tasks/Linux 第二条"],
  );
  const windows = screen.getByRole("region", { name: "Windows 节点 会话" });
  assert.equal(
    within(windows).getByTitle("当前已加载 1 个会话").textContent,
    "1",
  );
  assert.ok(within(windows).getByRole("img", { name: "离线" }));
  const empty = screen.getByRole("region", { name: "空节点 会话" });
  assert.equal(within(empty).queryAllByRole("link").length, 0);
  assert.ok(within(empty).getByText("这里还没有会话"));
  assert.ok(
    within(screen.getByRole("region", { name: "missing 会话" })).getByRole(
      "link",
      { name: /孤立会话/ },
    ),
  );
  fireEvent.click(within(linux).getByRole("link", { name: /Linux 第二条/ }));
  assert.deepEqual(navigated, ["Linux 第二条"]);
});

test("each node paginates and disables its own button without blocking another node", () => {
  const requested: string[] = [];
  let legacyMore = 0;
  render(
    <ProjectNavigation
      {...defaults}
      threads={[thread("Linux 第一页"), thread("Windows 第一页", "windows")]}
      hasMore
      onMore={() => {
        legacyMore += 1;
      }}
      hostPages={{
        linux: { hasMore: true, loading: false },
        windows: { hasMore: true, loading: true },
        empty: { hasMore: false, loading: false },
      }}
      onHostMore={(id) => requested.push(id)}
    />,
  );
  const linuxMore = screen.getByRole("button", {
    name: "加载 Linux 节点 的更多会话",
  });
  const windowsMore = screen.getByRole("button", {
    name: "加载 Windows 节点 的更多会话",
  });
  assert.equal(linuxMore.textContent, "加载更多会话");
  assert.equal((linuxMore as HTMLButtonElement).disabled, false);
  assert.equal((windowsMore as HTMLButtonElement).disabled, true);
  fireEvent.click(windowsMore);
  fireEvent.click(linuxMore);
  assert.deepEqual(requested, ["linux"]);
  assert.equal(legacyMore, 0);
  assert.ok(
    within(screen.getByRole("region", { name: "Windows 节点 会话" })).getByRole(
      "link",
      { name: /Windows 第一页/ },
    ),
  );
});

test("large and small machines retain separate reachable conversation regions", () => {
  render(
    <ProjectNavigation
      {...defaults}
      hosts={[hosts[1], hosts[0]]}
      threads={[
        ...Array.from({ length: 50 }, (_, index) =>
          thread(`Windows 会话 ${index + 1}`, "windows"),
        ),
        thread("Linux 唯一会话"),
      ]}
    />,
  );
  const windows = screen.getByRole("region", {
    name: "Windows 节点 会话列表",
  });
  const linux = screen.getByRole("region", { name: "Linux 节点 会话列表" });
  assert.equal(within(windows).getAllByRole("link").length, 50);
  assert.equal(within(linux).getAllByRole("link").length, 1);
  assert.ok(within(linux).getByRole("link", { name: /Linux 唯一会话/ }));
  assert.equal(windows.tabIndex, 0);
  assert.equal(linux.tabIndex, 0);
  assert.equal(
    screen
      .getByRole("button", { name: "收起 Windows 节点 的会话" })
      .getAttribute("aria-expanded"),
    "true",
  );
  assert.equal(
    screen
      .getByRole("button", { name: "收起 Linux 节点 的会话" })
      .getAttribute("aria-expanded"),
    "true",
  );
});

test("background activity updates preserve row order, project order and independent machine scroll positions", () => {
  const projects = [project("原第一项目"), project("原第二项目")];
  const threads = [
    thread("原第一条", "linux", { project_id: "原第一项目" }),
    thread("原第二条", "linux", { project_id: "原第二项目" }),
    thread("Windows 会话", "windows"),
  ];
  const { rerender, container } = render(
    <ProjectNavigation {...defaults} projects={projects} threads={threads} />,
  );
  const linuxList = screen.getByRole("region", { name: "Linux 节点 会话列表" });
  const windowsList = screen.getByRole("region", {
    name: "Windows 节点 会话列表",
  });
  const sharedList = screen.getByRole("region", { name: "项目与会话列表" });
  linuxList.scrollTop = 120;
  windowsList.scrollTop = 45;
  fireEvent.scroll(linuxList);
  assert.equal(windowsList.scrollTop, 45);
  fireEvent.scroll(windowsList);
  assert.equal(linuxList.scrollTop, 120);
  rerender(
    <ProjectNavigation
      {...defaults}
      projects={projects.map((value, index) => ({
        ...value,
        last_activity_at:
          index === 1 ? new Date().toISOString() : value.updated_at,
      }))}
      threads={threads.map((value, index) => ({
        ...value,
        updated_at: index === 1 ? new Date().toISOString() : value.updated_at,
      }))}
    />,
  );
  assert.equal(
    screen.getByRole("region", { name: "Linux 节点 会话列表" }),
    linuxList,
  );
  assert.equal(
    screen.getByRole("region", { name: "Windows 节点 会话列表" }),
    windowsList,
  );
  assert.equal(
    screen.getByRole("region", { name: "项目与会话列表" }),
    sharedList,
  );
  assert.equal(linuxList.scrollTop, 120);
  assert.equal(windowsList.scrollTop, 45);
  assert.deepEqual(
    within(linuxList)
      .getAllByRole("link")
      .map((link) => link.querySelector("strong")?.textContent),
    ["原第一条", "原第二条"],
  );
  assert.deepEqual(
    Array.from(container.querySelectorAll(".project-row strong")).map(
      (node) => node.textContent,
    ),
    ["原第一项目", "原第二项目"],
  );
  assert.equal(linuxList.tabIndex, 0);
  assert.equal(windowsList.tabIndex, 0);
});

test("collapsing a machine preserves its scroll and project state without filtering or changing other machines", () => {
  const filters: (string | null)[] = [];
  render(
    <ProjectNavigation
      {...defaults}
      hosts={[hosts[0], hosts[1]]}
      projects={[project("Linux 项目"), project("Windows 项目", "windows")]}
      threads={[
        thread("Linux 会话", "linux", { project_id: "Linux 项目" }),
        thread("Windows 会话", "windows", { project_id: "Windows 项目" }),
      ]}
      onFilter={(id) => filters.push(id)}
    />,
  );
  const linuxList = screen.getByRole("region", { name: "Linux 节点 会话列表" });
  const windowsList = screen.getByRole("region", {
    name: "Windows 节点 会话列表",
  });
  const linuxProject = screen.getByRole("region", {
    name: "Linux 项目 项目会话",
  });
  linuxList.scrollTop = 140;
  windowsList.scrollTop = 75;
  fireEvent.click(
    within(linuxProject).getByRole("button", { name: "收起会话" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "收起 Linux 节点 的会话" }),
  );
  assert.ok(linuxList.hidden);
  assert.equal(
    screen.queryByRole("region", { name: "Linux 节点 会话列表" }),
    null,
  );
  assert.ok(within(windowsList).getByRole("link", { name: /Windows 会话/ }));
  assert.equal(windowsList.scrollTop, 75);
  assert.deepEqual(filters, []);
  fireEvent.click(
    screen.getByRole("button", { name: "展开 Linux 节点 的会话" }),
  );
  assert.equal(
    screen.getByRole("region", { name: "Linux 节点 会话列表" }),
    linuxList,
  );
  assert.equal(linuxList.scrollTop, 140);
  assert.equal(windowsList.scrollTop, 75);
  assert.equal(within(linuxProject).queryByRole("link"), null);
  assert.ok(within(linuxProject).getByRole("button", { name: "展开会话" }));
  assert.deepEqual(filters, []);
});

test("navigation into a collapsed machine opens it while background metadata respects manual collapse", () => {
  const threads = [
    thread("Linux 当前会话"),
    thread("Windows 目标会话", "windows"),
  ];
  const { rerender } = render(
    <ProjectNavigation
      {...defaults}
      threads={threads}
      currentThreadId="Linux 当前会话"
    />,
  );
  fireEvent.click(
    screen.getByRole("button", { name: "收起 Windows 节点 的会话" }),
  );
  const refreshedThreads = threads.map((value) => ({
    ...value,
    updated_at: "2026-10-23T12:00:00Z",
  }));
  rerender(
    <ProjectNavigation
      {...defaults}
      threads={refreshedThreads}
      currentThreadId="Linux 当前会话"
    />,
  );
  assert.ok(screen.getByRole("button", { name: "展开 Windows 节点 的会话" }));
  rerender(
    <ProjectNavigation
      {...defaults}
      threads={refreshedThreads}
      currentThreadId="Windows 目标会话"
    />,
  );
  assert.ok(screen.getByRole("button", { name: "收起 Windows 节点 的会话" }));
  assert.equal(
    screen
      .getByRole("link", { name: /Windows 目标会话/ })
      .getAttribute("aria-current"),
    "page",
  );
  fireEvent.click(
    screen.getByRole("button", { name: "收起 Windows 节点 的会话" }),
  );
  rerender(
    <ProjectNavigation
      {...defaults}
      threads={refreshedThreads.map((value) => ({
        ...value,
        status: "active",
      }))}
      currentThreadId="Windows 目标会话"
    />,
  );
  assert.ok(screen.getByRole("button", { name: "展开 Windows 节点 的会话" }));
});

test("project filters show only their node and retain project settings with legacy pagination", () => {
  let projectMore = 0;
  const hostMore: string[] = [];
  const settings: string[] = [];
  const projects = [project("Linux 项目"), project("Windows 项目", "windows")];
  render(
    <ProjectNavigation
      {...defaults}
      projects={projects}
      selectedProjectId="Windows 项目"
      threads={[thread("项目会话", "windows", { project_id: "Windows 项目" })]}
      hasMore
      onMore={() => {
        projectMore += 1;
      }}
      hostPages={{ windows: { hasMore: false, loading: false } }}
      onHostMore={(id) => hostMore.push(id)}
      onSettings={(value) => settings.push(value.id)}
    />,
  );
  assert.ok(screen.getByRole("region", { name: "Windows 节点 会话" }));
  assert.ok(!screen.queryByRole("region", { name: "Linux 节点 会话" }));
  fireEvent.click(screen.getByRole("button", { name: "加载更多会话" }));
  assert.equal(projectMore, 1);
  assert.deepEqual(hostMore, []);
  fireEvent.click(
    screen.getByRole("button", { name: "Windows 项目 项目默认设置" }),
  );
  assert.deepEqual(settings, ["Windows 项目"]);
});

test("a single legacy node keeps its existing load-more button working", () => {
  let more = 0;
  render(
    <ProjectNavigation
      {...defaults}
      hosts={[hosts[0]]}
      threads={[thread("单节点会话")]}
      hasMore
      onMore={() => {
        more += 1;
      }}
    />,
  );
  assert.equal(screen.getAllByRole("region", { name: / 会话列表$/ }).length, 1);
  fireEvent.click(screen.getByRole("button", { name: "加载更多会话" }));
  assert.equal(more, 1);
});

test("node lists contain project groups in supplied order, keep empty projects selectable and have no date sections", () => {
  const filtered: (string | null)[] = [];
  const projects = [
    project("第二项目"),
    project("第一项目"),
    project("空项目", "linux", { cwd: "/empty", thread_count: 0 }),
  ];
  const { container } = render(
    <ProjectNavigation
      {...defaults}
      hosts={[hosts[0]]}
      projects={projects}
      threads={[
        thread("第一项目会话", "linux", { project_id: "第一项目" }),
        thread("第二项目原会话", "linux", { project_id: "第二项目" }),
        thread("第二项目新会话", "linux", {
          project_id: "第二项目",
          updated_at: new Date().toISOString(),
        }),
      ]}
      onFilter={(id) => filtered.push(id)}
    />,
  );
  assert.deepEqual(
    Array.from(
      container.querySelectorAll(".machine-project-group .project-row strong"),
    ).map((node) => node.textContent),
    ["第二项目", "第一项目", "空项目"],
  );
  const second = screen.getByRole("region", { name: "第二项目 项目会话" });
  assert.deepEqual(
    within(second)
      .getAllByRole("link")
      .map((link) => link.querySelector("strong")?.textContent),
    ["第二项目原会话", "第二项目新会话"],
  );
  assert.equal(
    container.querySelectorAll(
      ".thread-date-label, .thread-date-group, .project-filter-tree",
    ).length,
    0,
  );
  assert.equal(within(second).queryByText("今天"), null);
  fireEvent.click(screen.getByRole("button", { name: /空项目/ }));
  assert.deepEqual(filtered, ["空项目"]);
  assert.equal(screen.queryByText("此项目还没有会话"), null);
});

test("threads without project IDs match their node's directory or retain separate directory and unassigned groups", () => {
  render(
    <ProjectNavigation
      {...defaults}
      projects={[
        project("Windows 项目", "windows", { cwd: "C:\\Work\\" }),
        project("Linux 项目", "linux", { cwd: "/work" }),
      ]}
      threads={[
        thread("Windows 未映射会话", "windows", { cwd: "c:/work" }),
        thread("Linux 已有目录会话", "linux", { cwd: "/work/" }),
        thread("Linux 独立目录会话", "linux", { cwd: "/Work" }),
        thread("没有目录会话", "linux", { cwd: "" }),
      ]}
    />,
  );
  assert.ok(
    within(
      screen.getByRole("region", { name: "Windows 项目 项目会话" }),
    ).getByRole("link", { name: /Windows 未映射会话/ }),
  );
  assert.ok(
    within(
      screen.getByRole("region", { name: "Linux 项目 项目会话" }),
    ).getByRole("link", { name: /Linux 已有目录会话/ }),
  );
  assert.ok(
    within(screen.getByRole("region", { name: "Work 项目会话" })).getByRole(
      "link",
      { name: /Linux 独立目录会话/ },
    ),
  );
  assert.ok(
    within(
      screen.getByRole("region", { name: "未归属项目 项目会话" }),
    ).getByRole("link", { name: /没有目录会话/ }),
  );
  assert.equal(screen.getAllByRole("link").length, 4);
});

test("empty projects on orphan nodes remain reachable and global navigation actions are retained", () => {
  const filters: (string | null)[] = [];
  let created = 0;
  let refreshed = 0;
  render(
    <ProjectNavigation
      {...defaults}
      hosts={[]}
      projects={[project("离线空项目", "orphan", { thread_count: 0 })]}
      onFilter={(id) => filters.push(id)}
      onNewProject={() => {
        created += 1;
      }}
      onRefresh={() => {
        refreshed += 1;
      }}
    />,
  );
  assert.ok(screen.getByRole("region", { name: "orphan 会话" }));
  fireEvent.click(screen.getByRole("button", { name: /离线空项目/ }));
  fireEvent.click(screen.getByRole("button", { name: /全部会话/ }));
  fireEvent.click(screen.getByRole("button", { name: "添加项目" }));
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  assert.deepEqual(filters, ["离线空项目", null]);
  assert.equal(created, 1);
  assert.equal(refreshed, 1);
});
