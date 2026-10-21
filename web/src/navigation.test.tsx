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

test("background activity updates preserve row order, project order and each node's scroll element", () => {
  const projects = [project("原第一项目"), project("原第二项目")];
  const threads = [
    thread("原第一条", "linux", { project_id: "原第一项目" }),
    thread("原第二条", "linux", { project_id: "原第二项目" }),
    thread("Windows 会话", "windows"),
  ];
  const { rerender, container } = render(
    <ProjectNavigation {...defaults} projects={projects} threads={threads} />,
  );
  fireEvent.click(screen.getByText("项目", { selector: "summary" }));
  const linuxList = screen.getByRole("region", { name: "Linux 节点 会话列表" });
  const windowsList = screen.getByRole("region", {
    name: "Windows 节点 会话列表",
  });
  linuxList.scrollTop = 120;
  windowsList.scrollTop = 35;
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
  assert.equal(linuxList.scrollTop, 120);
  assert.equal(windowsList.scrollTop, 35);
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
