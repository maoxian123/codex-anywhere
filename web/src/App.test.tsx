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
import App from "./App";
import type { Host, HubEvent, Project, Thread, ThreadOwnership } from "./types";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => this.emit({ type: "ready" }));
  }
  send() {}
  close() {}
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
  }
}

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  FakeWebSocket.instances.length = 0;
});

function setup(
  threadOverride: Partial<Thread> = {},
  pathname = "/tasks/t1",
  capabilities: string[] = [],
  catalog?: { threads?: Thread[]; projects?: Project[]; hosts?: Host[] },
) {
  const thread: Thread = {
    id: "t1",
    host_id: "h1",
    title: "集成测试任务",
    cwd: "C:\\work",
    status: "idle",
    created_at: "2026-10-20T00:00:00Z",
    updated_at: "2026-10-20T00:00:00Z",
    ...threadOverride,
  };
  Object.defineProperty(globalThis, "WebSocket", {
    configurable: true,
    writable: true,
    value: FakeWebSocket,
  });
  let refreshes = 0;
  let sequence = 0;
  const threads = catalog?.threads || [thread];
  const hosts = catalog?.hosts || [
    {
      id: "h1",
      name: "测试设备",
      platform: "windows",
      online: true,
      capabilities,
    },
  ];
  const projects: Project[] = catalog?.projects || [
    {
      id: "p1",
      host_id: "h1",
      name: "work",
      cwd: "C:\\work",
      thread_count: 1,
      created_at: thread.created_at,
      updated_at: thread.updated_at,
    },
  ];
  let intercept:
    | ((
        path: string,
        init?: RequestInit,
      ) => Response | Promise<Response> | undefined)
    | undefined;
  const authentication = {
    authenticated: true,
    username: "test",
    csrf_token: "csrf",
  };
  const loginHeaders: Headers[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    const pathname = path.split("?")[0];
    const overridden = intercept?.(path, init);
    if (overridden) return overridden;
    let result: unknown;
    if (path === "/api/auth/me") result = authentication;
    else if (path === "/api/auth/login") {
      loginHeaders.push(new Headers(init?.headers));
      authentication.authenticated = true;
      result = authentication;
    } else if (!authentication.authenticated)
      return new Response(
        JSON.stringify({ error: "Authentication required" }),
        { status: 401 },
      );
    else if (path === "/api/settings")
      result = { administrator: { username: authentication.username } };
    else if (path === "/api/settings/hosts") result = { hosts: [] };
    else if (path === "/api/projects") result = { projects };
    else if (path === "/api/hosts") result = { hosts };
    else if (path.startsWith("/api/threads?")) {
      refreshes += 1;
      const query = new URLSearchParams(path.split("?")[1]);
      const offset = Number(query.get("offset") || 0);
      const limit = Number(query.get("limit") || 50);
      const hostId = query.get("host_id");
      const projectId = query.get("project_id");
      const filtered = threads.filter(
        (entry) =>
          (!hostId || entry.host_id === hostId) &&
          (!projectId || entry.project_id === projectId),
      );
      result = {
        threads: filtered.slice(offset, offset + limit),
        has_more: offset + limit < filtered.length,
        next_offset: offset + limit < filtered.length ? offset + limit : null,
      };
    } else if (pathname === "/api/threads/t1")
      result = { thread, items: [], interactions: [] };
    else if (path.startsWith("/api/threads/")) {
      const found = threads.find(
        (entry) => pathname === `/api/threads/${entry.id}`,
      );
      if (!found) throw new Error(`Unexpected test request: ${path}`);
      result = { thread: found, items: [], interactions: [] };
    } else throw new Error(`Unexpected test request: ${path}`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  window.history.replaceState({}, "", pathname);
  render(<App />);
  return {
    thread,
    authentication,
    loginHeaders,
    threads,
    hosts,
    projects,
    intercept: (handler: typeof intercept) => {
      intercept = handler;
    },
    refreshes: () => refreshes,
    event: (
      method: string,
      params: Record<string, unknown>,
      threadId = "t1",
    ) => {
      const event: HubEvent = {
        type: "event",
        id: ++sequence,
        thread_id: threadId,
        method,
        params,
      };
      act(() => FakeWebSocket.instances.at(-1)!.emit(event));
    },
  };
}

function ownershipServer(
  initial: ThreadOwnership,
  releaseResult?: Record<string, unknown>,
) {
  const server = setup({ native_id: "native-t1" }, "/tasks/t1", [
    "threadOwnership",
  ]);
  let state = initial;
  const requests: {
    path: string;
    data: Record<string, unknown>;
    init?: RequestInit;
  }[] = [];
  server.intercept((path, init) => {
    if (
      !/^\/api\/threads\/t1\/(ownership|presence|release|takeover|messages)$/.test(
        path,
      )
    )
      return;
    const data = JSON.parse(String(init?.body || "{}")) as Record<
      string,
      unknown
    >;
    requests.push({ path, data, init });
    let result: unknown = {};
    if (path.endsWith("/ownership")) result = state;
    if (path.endsWith("/takeover")) {
      state = {
        state: "managed",
        owners: [],
        can_terminate: false,
        owner_token: null,
      };
      result = { ownership: state };
    }
    if (path.endsWith("/release")) {
      if (releaseResult) result = releaseResult;
      else {
        state = {
          state: "available",
          owners: [],
          can_terminate: false,
          owner_token: null,
        };
        result = { released: true, deferred: false, ownership: state };
      }
    }
    if (path.endsWith("/messages")) {
      state = {
        state: "external",
        owners: [{ pid: 512, name: "codex" }],
        can_terminate: true,
        owner_token: "changed",
      };
      return new Response(
        JSON.stringify({
          command: {
            id: "failed-message",
            kind: "turn.start",
            status: "failed",
            error: "thread native-t1 already has an active writer",
          },
        }),
      );
    }
    return new Response(
      JSON.stringify({
        command: {
          id: `cmd-${requests.length}`,
          kind: path.split("/").at(-1),
          status: "completed",
          result,
        },
      }),
    );
  });
  return {
    ...server,
    requests,
    setOwnership: (value: ThreadOwnership) => {
      state = value;
    },
  };
}

test("external writer takeover requires PID confirmation and never sends the draft automatically", async () => {
  const server = ownershipServer({
    state: "external",
    owners: [{ pid: 512, name: "codex", started_at: 1750000000 }],
    owner_kind: "candidate",
    can_terminate: true,
    owner_token: "identity",
  });
  const input = await screen.findByRole("textbox", {
    name: "发送消息给 Codex",
  });
  fireEvent.change(input, { target: { value: "保留我的草稿" } });
  fireEvent.click(await screen.findByRole("button", { name: /^会话托管：/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: "结束进程并接管" }),
  );
  await screen.findByRole("heading", { name: "结束占用进程并接管" });
  assert.match(screen.getByRole("dialog").textContent || "", /PID 512/);
  assert.match(screen.getByRole("dialog").textContent || "", /其他会话/);
  assert.ok(
    screen
      .getByRole("dialog")
      .textContent?.includes(new Date(1750000000 * 1000).toLocaleString()),
  );
  assert.equal(
    server.requests.filter((entry) => entry.path.endsWith("/takeover")).length,
    0,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认结束并接管" }));
  await screen.findByText("已托管");
  const takeover = server.requests.find((entry) =>
    entry.path.endsWith("/takeover"),
  )!;
  assert.equal(takeover.data.confirmed, true);
  assert.equal(takeover.data.owner_token, "identity");
  assert.equal(typeof takeover.data.client_id, "string");
  assert.equal(typeof takeover.data.issued_at, "number");
  assert.equal((input as HTMLTextAreaElement).value, "保留我的草稿");
  assert.equal(
    server.requests.filter((entry) => entry.path.endsWith("/messages")).length,
    0,
  );
});

test("a writer conflict checks ownership and preserves the failed message draft", async () => {
  const server = ownershipServer({
    state: "available",
    owners: [],
    can_terminate: false,
    owner_token: null,
  });
  const input = await screen.findByRole("textbox", {
    name: "发送消息给 Codex",
  });
  await screen.findByText("可接管");
  fireEvent.change(input, { target: { value: "继续之前的任务" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByRole("button", { name: /^会话托管：其他进程占用/ });
  assert.equal(screen.queryByRole("button", { name: "结束进程并接管" }), null);
  fireEvent.click(screen.getByRole("button", { name: /^会话托管：/ }));
  await screen.findByRole("button", { name: "结束进程并接管" });
  assert.equal((input as HTMLTextAreaElement).value, "继续之前的任务");
  assert.equal(
    server.requests.filter((entry) => entry.path.endsWith("/messages")).length,
    1,
  );
  assert.ok(
    server.requests.filter((entry) => entry.path.endsWith("/ownership"))
      .length >= 2,
  );
});

test("leaving the conversation releases this tab's lease with a later timestamp", async () => {
  const server = ownershipServer({
    state: "managed",
    owners: [],
    can_terminate: false,
    owner_token: null,
  });
  await screen.findByText("已托管");
  fireEvent.click(screen.getByLabelText("设备与设置"));
  fireEvent.click(await screen.findByRole("button", { name: "设置" }));
  await screen.findByRole("textbox", { name: "管理员用户名" });
  assert.equal(screen.queryByRole("button", { name: /^会话托管：/ }), null);
  await waitFor(() =>
    assert.ok(server.requests.some((entry) => entry.path.endsWith("/release"))),
  );
  const lease = server.requests.filter((entry) =>
    /\/(presence|release)$/.test(entry.path),
  );
  const release = lease.at(-1)!;
  assert.equal(release.path, "/api/threads/t1/release");
  assert.equal(release.init?.keepalive, true);
  assert.equal(release.data.client_id, lease[0].data.client_id);
  assert.ok(Number(release.data.issued_at) > Number(lease[0].data.issued_at));
});

test("manual release and native ownership events update the displayed management state", async () => {
  const server = ownershipServer({
    state: "managed",
    owners: [],
    can_terminate: false,
    owner_token: null,
  });
  fireEvent.click(await screen.findByRole("button", { name: /^会话托管：/ }));
  fireEvent.click(await screen.findByRole("button", { name: "释放托管" }));
  await screen.findByText("可接管");
  const count = server.requests.filter((entry) =>
    entry.path.endsWith("/release"),
  ).length;
  assert.equal(count, 1);
  server.event("anywhere/ownership", {
    ownership: {
      state: "managed",
      owners: [],
      can_terminate: false,
      owner_token: null,
    },
  });
  await screen.findByText("已托管");
  server.setOwnership({
    state: "unknown",
    owners: [],
    can_terminate: false,
    owner_token: null,
    reason: "没有足够权限确认占用进程",
  });
  server.event("thread/status/changed", { status: { type: "notLoaded" } });
  await screen.findByText("状态待确认");
  assert.ok(screen.getByText("没有足够权限确认占用进程"));
  assert.ok(!screen.queryByRole("button", { name: "结束进程并接管" }));
});

test("pagehide releases with keepalive and pageshow restores only browser presence", async () => {
  const server = ownershipServer({
    state: "managed",
    owners: [],
    can_terminate: false,
    owner_token: null,
  });
  await screen.findByText("已托管");
  act(() => window.dispatchEvent(new Event("pagehide")));
  await waitFor(() =>
    assert.ok(server.requests.some((entry) => entry.path.endsWith("/release"))),
  );
  assert.equal(
    server.requests.find((entry) => entry.path.endsWith("/release"))?.init
      ?.keepalive,
    true,
  );
  act(() => window.dispatchEvent(new Event("pageshow")));
  await waitFor(() =>
    assert.equal(
      server.requests.filter((entry) => entry.path.endsWith("/presence"))
        .length,
      2,
    ),
  );
  assert.equal(
    server.requests.filter((entry) => /\/(takeover|messages)$/.test(entry.path))
      .length,
    0,
  );
});

for (const result of [
  { released: false, error: "无法释放：当前节点不支持立即卸载" },
  { released: false, deferred: false },
]) {
  test(`release failures never display a success notice: ${JSON.stringify(result)}`, async () => {
    ownershipServer(
      { state: "managed", owners: [], can_terminate: false, owner_token: null },
      result,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^会话托管：/ }));
    fireEvent.click(await screen.findByRole("button", { name: "释放托管" }));
    await screen.findByText(
      result.error || "释放托管失败，请重新检查占用状态。",
    );
    assert.ok(!screen.queryByText(/已释放托管/));
    assert.ok(screen.getByText("已托管"));
  });
}

test("deferred manual release remains paused while browsing, and sending explicitly renews presence", async () => {
  const server = ownershipServer(
    { state: "managed", owners: [], can_terminate: false, owner_token: null },
    {
      released: false,
      deferred: true,
      reason: "当前任务执行结束后释放托管",
    },
  );
  fireEvent.click(await screen.findByRole("button", { name: /^会话托管：/ }));
  fireEvent.click(await screen.findByRole("button", { name: "释放托管" }));
  await screen.findByText("当前任务执行结束后释放托管");
  fireEvent.pointerDown(document);
  fireEvent.wheel(document);
  fireEvent.keyDown(document, { key: "ArrowDown" });
  fireEvent.scroll(document);
  assert.equal(
    server.requests.filter((entry) => entry.path.endsWith("/presence")).length,
    1,
  );
  const input = screen.getByRole("textbox", { name: "发送消息给 Codex" });
  fireEvent.change(input, { target: { value: "继续执行" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() =>
    assert.equal(
      server.requests.filter((entry) => entry.path.endsWith("/presence"))
        .length,
      2,
    ),
  );
});

test("retained leases show the other page explanation and native release errors remain visible", async () => {
  const server = ownershipServer(
    { state: "managed", owners: [], can_terminate: false, owner_token: null },
    { released: false, retained: true },
  );
  fireEvent.click(await screen.findByRole("button", { name: /^会话托管：/ }));
  fireEvent.click(await screen.findByRole("button", { name: "释放托管" }));
  await screen.findByText("其他页面仍在使用此会话，当前托管将继续保留。");
  server.event("anywhere/ownership", {
    ownership: {
      state: "managed",
      owners: [],
      can_terminate: false,
      owner_token: null,
    },
    release_error: "释放失败：有未保存的临时会话",
  });
  await screen.findByText("释放失败：有未保存的临时会话");
});

test("application settings open from the sidebar and conversations remain reachable", async () => {
  setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  fireEvent.click(screen.getByLabelText("设备与设置"));
  fireEvent.click(await screen.findByRole("button", { name: "设置" }));
  const username = await screen.findByRole("textbox", { name: "管理员用户名" });
  assert.equal((username as HTMLInputElement).value, "test");
  assert.equal(window.location.pathname, "/settings");
  assert.ok(screen.getByRole("heading", { level: 1, name: "设置" }));
  assert.ok(!screen.queryByRole("textbox", { name: "发送消息给 Codex" }));
  fireEvent.click(screen.getByRole("button", { name: /全部会话/ }));
  await screen.findByText("选择一个会话继续");
  assert.equal(window.location.pathname, "/");
  fireEvent.click(screen.getByRole("link", { name: /集成测试任务/ }));
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  assert.equal(window.location.pathname, "/tasks/t1");
});

test("settings deep links retain the updated session and use the rotated CSRF token", async () => {
  const server = setup({}, "/settings");
  const tokens: (string | null)[] = [];
  server.intercept((path, init) => {
    if (path !== "/api/settings/administrator") return undefined;
    assert.equal(init?.method, "PATCH");
    tokens.push(new Headers(init?.headers).get("X-CSRF-Token"));
    const data = JSON.parse(String(init?.body)) as { username: string };
    const session = {
      authenticated: true,
      username: data.username,
      csrf_token: "updated-account-csrf",
    };
    Object.assign(server.authentication, session);
    return new Response(
      JSON.stringify({
        settings: { administrator: { username: data.username } },
        session,
      }),
    );
  });
  const username = await screen.findByRole("textbox", { name: "管理员用户名" });
  fireEvent.change(username, { target: { value: "updated-admin" } });
  fireEvent.change(screen.getByLabelText("当前密码"), {
    target: { value: "current-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
  await screen.findByText("管理员账号已更新");
  assert.equal(window.location.pathname, "/settings");
  assert.equal((username as HTMLInputElement).value, "updated-admin");
  assert.ok(!screen.queryByRole("button", { name: "进入工作台" }));
  fireEvent.change(screen.getByLabelText("当前密码"), {
    target: { value: "current-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
  await waitFor(() => assert.equal(tokens.length, 2));
  assert.deepEqual(tokens, ["csrf", "updated-account-csrf"]);
});

test("native execution errors survive successful background list refreshes", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  server.event("error", {
    error: {
      message: "Unsupported model",
      additionalDetails: "Choose a valid model.",
    },
  });
  await screen.findByText(/Unsupported model/);
  const before = server.refreshes();
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await waitFor(() => assert.ok(server.refreshes() > before));
  assert.match(
    screen.getByRole("alert").textContent || "",
    /Unsupported model.*Choose a valid model\./s,
  );
});

test("persisted thread error is visible on reload and clears with the next active turn", async () => {
  const server = setup({
    status: "error",
    last_error: "Authentication expired on node",
  });
  await screen.findByText("Authentication expired on node");
  server.thread.last_error = null;
  server.thread.status = "active";
  server.thread.active_turn_id = "turn-next";
  server.event("turn/started", { turn: { id: "turn-next" } });
  await waitFor(() =>
    assert.equal(
      screen.queryByText("Authentication expired on node") === null,
      true,
    ),
  );
  assert.ok(screen.getByRole("textbox", { name: "补充任务要求" }));
});

test("an expired session returns to login and the next login uses the refreshed CSRF token", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  server.authentication.authenticated = false;
  server.authentication.csrf_token = "csrf-after-expiry";
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  const username = await screen.findByRole("textbox", { name: "用户名" });
  fireEvent.change(username, { target: { value: "test" } });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "disposable-test-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  assert.equal(
    server.loginHeaders.at(-1)?.get("X-CSRF-Token"),
    "csrf-after-expiry",
  );
});

test("WebSocket reconnection resubscribes and returns to live status", async () => {
  setup();
  await screen.findByText("实时连接");
  const previous = FakeWebSocket.instances.length;
  act(() => FakeWebSocket.instances.at(-1)!.onclose?.());
  await screen.findByText("正在重连");
  await waitFor(() => assert.ok(FakeWebSocket.instances.length > previous), {
    timeout: 2000,
  });
  await screen.findByText("实时连接");
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function catalogThread(
  id: string,
  title: string,
  projectId: string,
  activity: string,
  hostId = "h1",
): Thread {
  return {
    id,
    title,
    host_id: hostId,
    project_id: projectId,
    cwd: `C:\\${projectId}`,
    status: "idle",
    created_at: activity,
    updated_at: activity,
    last_activity_at: activity,
  };
}

function catalogProject(
  id: string,
  name: string,
  activity: string,
  hostId = "h1",
): Project {
  return {
    id,
    name,
    host_id: hostId,
    cwd: `C:\\${id}`,
    thread_count: 1,
    created_at: activity,
    updated_at: activity,
    last_activity_at: activity,
  };
}

function conversationIds(root: ParentNode = document): string[] {
  return Array.from(
    root.querySelectorAll<HTMLAnchorElement>('a[href^="/tasks/"]'),
  ).map((link) => link.getAttribute("href")!.split("/").at(-1)!);
}

function projectNames(names: string[]): string[] {
  return screen
    .getAllByRole("button")
    .map((button) => names.find((name) => button.textContent?.includes(name)))
    .filter((name): name is string => name !== undefined);
}

function recentCatalog() {
  const recent = "2026-10-21T10:00:00Z",
    old = "2026-10-01T10:00:00Z";
  const older = catalogThread("t1", "较早会话", "p1", old);
  const latest = catalogThread("t2", "近期会话", "p2", recent);
  const projects = [
    catalogProject("p2", "近期项目", recent),
    catalogProject("p1", "较早项目", old),
  ];
  return { older, latest, projects, threads: [latest, older] };
}

test("deleting a host clears its active conversation and rejects late snapshots without hiding other hosts", async () => {
  const first = catalogThread(
    "t1",
    "待删除机器会话",
    "p1",
    "2026-10-21T10:00:00Z",
  );
  const second = catalogThread(
    "t2",
    "保留机器会话",
    "p2",
    "2026-10-21T09:00:00Z",
    "h2",
  );
  const server = setup(first, "/tasks/t1", [], {
    threads: [first, second],
    hosts: [
      { id: "h1", name: "删除机器", platform: "windows", online: true },
      { id: "h2", name: "保留机器", platform: "linux", online: true },
    ],
    projects: [
      catalogProject("p1", "删除项目", first.created_at),
      catalogProject("p2", "保留项目", second.created_at, "h2"),
    ],
  });
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  await screen.findByRole("link", { name: /保留机器会话/ });
  const stale = deferred<Response>();
  let requested = false;
  server.intercept((path) => {
    if (
      path.startsWith("/api/threads?") &&
      new URLSearchParams(path.split("?")[1]).get("host_id") === "h1"
    ) {
      requested = true;
      return stale.promise;
    }
    return undefined;
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await waitFor(() => assert.equal(requested, true));
  server.event("anywhere/host_deleted", { host_id: "h1" }, "");
  await screen.findByRole("heading", { name: "选择一个会话继续" });
  assert.equal(window.location.pathname, "/");
  assert.equal(
    screen.queryByRole("textbox", { name: "发送消息给 Codex" }),
    null,
  );
  await act(async () => {
    stale.resolve(
      response({ threads: [first], has_more: false, next_offset: null }),
    );
    await stale.promise;
  });
  await screen.findByRole("link", { name: /保留机器会话/ });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await waitFor(() => assert.deepEqual(conversationIds(), ["t2"]));
  assert.equal(screen.queryByRole("region", { name: "删除机器 会话" }), null);
  assert.ok(screen.getByRole("region", { name: "保留机器 会话" }));
});

test("deleting a machine in settings removes its sidebar rows without leaving the settings page", async () => {
  const server = setup({}, "/settings");
  server.intercept((path, init) => {
    if (path === "/api/settings/hosts")
      return response({
        hosts: [
          {
            ...server.hosts[0],
            data: {
              projects: 1,
              threads: 1,
              messages: 0,
              attachments: 0,
              attachment_bytes: 0,
            },
          },
        ],
      });
    if (path === "/api/hosts/h1" && init?.method === "DELETE") {
      assert.deepEqual(JSON.parse(String(init.body)), {
        confirm_name: "测试设备",
      });
      return response({ deleted: true, host_id: "h1" });
    }
    return undefined;
  });
  await screen.findByRole("link", { name: /集成测试任务/ });
  fireEvent.click(
    await screen.findByRole("button", { name: "删除 测试设备 的机器与数据" }),
  );
  fireEvent.change(screen.getByLabelText("输入机器名称以确认删除"), {
    target: { value: "测试设备" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认删除机器与数据" }));
  await screen.findByText("已删除“测试设备”在管理中心保存的机器记录与数据。");
  await waitFor(() =>
    assert.equal(screen.queryByRole("link", { name: /集成测试任务/ }), null),
  );
  assert.equal(window.location.pathname, "/settings");
  assert.ok(screen.getByRole("heading", { name: "机器与数据" }));
});

test("initial navigation follows recent activity without promoting the selected older conversation", async () => {
  const catalog = recentCatalog();
  setup(catalog.older, "/tasks/t1", [], catalog);
  await screen.findByRole("link", { name: /近期会话/ });
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  assert.deepEqual(conversationIds(), ["t2", "t1"]);
  assert.deepEqual(projectNames(["近期项目", "较早项目"]), [
    "近期项目",
    "较早项目",
  ]);
});

test("background metadata updates titles and status while inverted activity preserves conversation and project order", async () => {
  const catalog = recentCatalog();
  const server = setup(catalog.older, "/tasks/t1", [], catalog);
  await screen.findByRole("link", { name: /近期会话/ });
  server.threads.splice(
    0,
    2,
    {
      ...catalog.older,
      title: "较早会话更新",
      last_activity_at: "2030-01-01T00:00:00Z",
      updated_at: "2030-01-01T00:00:00Z",
    },
    {
      ...catalog.latest,
      title: "近期会话更新",
      status: "active",
      last_activity_at: "2000-01-01T00:00:00Z",
    },
  );
  server.projects.splice(
    0,
    2,
    {
      ...catalog.projects[1],
      name: "较早项目更新",
      last_activity_at: "2030-01-01T00:00:00Z",
    },
    {
      ...catalog.projects[0],
      name: "近期项目更新",
      last_activity_at: "2000-01-01T00:00:00Z",
    },
  );
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  const updated = await screen.findByRole("link", { name: /近期会话更新/ });
  assert.ok(updated.querySelector(".thread-dot.status-active"));
  assert.ok(screen.getByRole("link", { name: /较早会话更新/ }));
  assert.deepEqual(conversationIds(), ["t2", "t1"]);
  assert.deepEqual(projectNames(["近期项目更新", "较早项目更新"]), [
    "近期项目更新",
    "较早项目更新",
  ]);
});

test("an accepted message immediately promotes its conversation and project while command polling and an older list are pending", async () => {
  const catalog = recentCatalog();
  const server = setup(catalog.older, "/tasks/t1", [], catalog);
  const input = await screen.findByRole("textbox", {
    name: "发送消息给 Codex",
  });
  await screen.findByRole("link", { name: /近期会话/ });
  const staleThreads = deferred<Response>();
  const completion = deferred<Response>();
  let listStarted = false,
    polled = false,
    holdLists = true;
  server.intercept((path) => {
    if (path.startsWith("/api/threads?") && holdLists) {
      listStarted = true;
      return staleThreads.promise;
    }
    if (path === "/api/projects")
      return response({ projects: catalog.projects });
    if (path === "/api/threads/t1/messages")
      return response(
        {
          command: {
            id: "promote-command",
            kind: "turn.start",
            status: "queued",
          },
        },
        202,
      );
    if (path === "/api/commands/promote-command") {
      polled = true;
      return completion.promise;
    }
    return undefined;
  });
  try {
    fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
    await waitFor(() => assert.equal(listStarted, true));
    fireEvent.change(input, { target: { value: "继续较早会话" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => assert.equal(polled, true));
    assert.deepEqual(conversationIds(), ["t1", "t2"]);
    assert.deepEqual(projectNames(["近期项目", "较早项目"]), [
      "较早项目",
      "近期项目",
    ]);
    holdLists = false;
    await act(async () => {
      staleThreads.resolve(
        response({
          threads: [catalog.latest, catalog.older],
          has_more: false,
          next_offset: null,
        }),
      );
      await staleThreads.promise;
    });
    assert.deepEqual(conversationIds(), ["t1", "t2"]);
    assert.deepEqual(projectNames(["近期项目", "较早项目"]), [
      "较早项目",
      "近期项目",
    ]);
  } finally {
    holdLists = false;
    await act(async () => {
      staleThreads.resolve(
        response({
          threads: catalog.threads,
          has_more: false,
          next_offset: null,
        }),
      );
      completion.resolve(
        response({
          command: {
            id: "promote-command",
            kind: "turn.start",
            status: "completed",
            result: {},
          },
        }),
      );
      await Promise.all([staleThreads.promise, completion.promise]);
    });
  }
});

test("each node loads its own first page and advances only its own native offset", async () => {
  const first = catalogThread(
    "t1",
    "Windows 最近会话",
    "p1",
    "2026-10-21T10:00:00Z",
  );
  const second = catalogThread(
    "t2",
    "Linux 最近会话",
    "p2",
    "2026-10-21T09:00:00Z",
    "h2",
  );
  const server = setup(first, "/tasks/t1", [], {
    threads: [first, second],
    hosts: [
      { id: "h1", name: "Windows 节点", platform: "windows", online: true },
      { id: "h2", name: "Linux 节点", platform: "linux", online: true },
    ],
    projects: [
      catalogProject("p1", "Windows 项目", first.created_at),
      catalogProject("p2", "Linux 项目", second.created_at, "h2"),
    ],
  });
  const queries: URLSearchParams[] = [];
  server.intercept((path) => {
    if (!path.startsWith("/api/threads?")) return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    queries.push(query);
    const host = query.get("host_id"),
      offset = Number(query.get("offset"));
    assert.ok(
      host === "h1" || host === "h2",
      "unfiltered global pages must not starve another node",
    );
    assert.equal(query.get("project_id"), null);
    const initial = host === "h1" ? first : second;
    return response({
      threads: [
        offset === 0
          ? initial
          : { ...initial, id: `${host}-older`, title: `${host} 较早会话` },
      ],
      has_more: offset === 0,
      next_offset: offset === 0 ? (host === "h1" ? 17 : 31) : null,
    });
  });
  await screen.findByRole("link", { name: /Windows 最近会话/ });
  await screen.findByRole("link", { name: /Linux 最近会话/ });
  const windows = screen.getByRole("region", { name: "Windows 节点 会话" });
  const linux = screen.getByRole("region", { name: "Linux 节点 会话" });
  assert.deepEqual(conversationIds(windows), ["t1"]);
  assert.deepEqual(conversationIds(linux), ["t2"]);
  fireEvent.click(
    screen.getByRole("button", { name: "加载 Windows 节点 的更多会话" }),
  );
  await screen.findByRole("link", { name: /h1 较早会话/ });
  assert.deepEqual(conversationIds(windows), ["t1", "h1-older"]);
  assert.deepEqual(conversationIds(linux), ["t2"]);
  assert.ok(
    queries.some(
      (query) => query.get("host_id") === "h1" && query.get("offset") === "0",
    ),
  );
  assert.ok(
    queries.some(
      (query) => query.get("host_id") === "h2" && query.get("offset") === "0",
    ),
  );
  assert.deepEqual(
    queries
      .filter(
        (query) => query.get("host_id") === "h1" && query.get("offset") !== "0",
      )
      .map((query) => Number(query.get("offset"))),
    [17],
  );
  assert.deepEqual(
    queries.filter(
      (query) => query.get("host_id") === "h2" && query.get("offset") !== "0",
    ),
    [],
  );
  fireEvent.click(
    screen.getByRole("button", { name: "加载 Linux 节点 的更多会话" }),
  );
  await screen.findByRole("link", { name: /h2 较早会话/ });
  assert.deepEqual(conversationIds(linux), ["t2", "h2-older"]);
  assert.deepEqual(conversationIds(windows), ["t1", "h1-older"]);
  assert.deepEqual(
    queries
      .filter(
        (query) => query.get("host_id") === "h2" && query.get("offset") !== "0",
      )
      .map((query) => Number(query.get("offset"))),
    [31],
  );
});

test("a late first-page snapshot cannot discard conversations already loaded from an older page", async () => {
  const server = setup();
  let pendingRefresh = false,
    refreshStarted = false;
  const stale = deferred<Response>();
  server.intercept((path) => {
    if (!path.startsWith("/api/threads?")) return undefined;
    const offset = Number(
      new URLSearchParams(path.split("?")[1]).get("offset"),
    );
    if (offset === 0 && pendingRefresh) {
      refreshStarted = true;
      return stale.promise;
    }
    return response({
      threads: [
        offset === 0
          ? server.thread
          : { ...server.thread, id: "older-loaded", title: "已加载的旧会话" },
      ],
      has_more: offset === 0,
      next_offset: offset === 0 ? 17 : null,
    });
  });
  await screen.findByRole("button", { name: /加载.*更多会话/ });
  fireEvent.click(screen.getByRole("button", { name: /加载.*更多会话/ }));
  await screen.findByRole("link", { name: /已加载的旧会话/ });
  pendingRefresh = true;
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await waitFor(() => assert.equal(refreshStarted, true));
  await act(async () => {
    stale.resolve(
      response({ threads: [server.thread], has_more: true, next_offset: 17 }),
    );
    await stale.promise;
  });
  assert.deepEqual(conversationIds(), ["t1", "older-loaded"]);
});

test("continuous streaming still paints snapshots when requests take longer than the refresh interval", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  let inFlight = 0;
  let peak = 0;
  server.intercept((path) => {
    if (path.split("?")[0] !== "/api/threads/t1") return undefined;
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    return new Promise<Response>((resolve) =>
      setTimeout(() => {
        inFlight -= 1;
        resolve(
          response({
            thread: server.thread,
            interactions: [],
            items: [
              {
                id: "i1",
                native_id: "i1",
                thread_id: "t1",
                type: "agentMessage",
                data: { text: "持续流的中间结果" },
              },
            ],
          }),
        );
      }, 450),
    );
  });
  const emit = () =>
    server.event("item/agentMessage/delta", { itemId: "i1", delta: "chunk" });
  emit();
  const timer = setInterval(emit, 180);
  try {
    await screen.findByText("持续流的中间结果", {}, { timeout: 1400 });
    assert.equal(
      peak,
      1,
      "a selected thread must have at most one snapshot request in flight",
    );
  } finally {
    clearInterval(timer);
  }
});

test("out-of-order list responses cannot replace a newer host online state", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  const old = deferred<Response>();
  let calls = 0;
  server.intercept((path) => {
    if (path !== "/api/hosts") return undefined;
    calls += 1;
    if (calls === 1) return old.promise;
    return response({
      hosts: [
        {
          id: "h1",
          name: "恢复后的设备",
          platform: "windows",
          online: true,
        },
      ],
    });
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await waitFor(() => assert.equal(calls, 1));
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await screen.findAllByText("恢复后的设备");
  await act(async () => {
    old.resolve(
      response({
        hosts: [
          {
            id: "h1",
            name: "过期离线设备",
            platform: "windows",
            online: false,
          },
        ],
      }),
    );
    await old.promise;
  });
  assert.equal(screen.queryByText("过期离线设备") === null, true);
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "device is online" },
  });
  assert.equal(
    (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
      .disabled,
    false,
  );
});

test("late commands and errors from a previous task cannot clear or overwrite the current task error", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  server.threads.push({ ...server.thread, id: "t2", title: "第二任务" });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await screen.findByRole("link", { name: /第二任务/ });
  const send = deferred<Response>();
  let polled = false;
  server.intercept((path) => {
    if (path === "/api/threads/t1/messages") return send.promise;
    if (path === "/api/commands/old-command") {
      polled = true;
      return response({
        command: {
          id: "old-command",
          kind: "turn.start",
          status: "failed",
          error: "Old task command failed",
        },
      });
    }
    return undefined;
  });
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "old task input" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  fireEvent.click(screen.getByRole("link", { name: /第二任务/ }));
  await screen.findByRole("heading", { name: "第二任务" });
  server.event("error", { error: { message: "Current task error" } }, "t2");
  await screen.findByText("Current task error");
  await act(async () => {
    send.resolve(
      response(
        {
          command: { id: "old-command", kind: "turn.start", status: "queued" },
        },
        202,
      ),
    );
    await send.promise;
  });
  await waitFor(() => assert.equal(polled, true));
  server.event(
    "anywhere/command_completed",
    { status: "failed", error: "Old task event failed" },
    "t1",
  );
  assert.ok(screen.getByText("Current task error"));
  assert.equal(screen.queryByText("Old task command failed") === null, true);
  assert.equal(screen.queryByText("Old task event failed") === null, true);
});

test("switching tasks does not wait for or display an older pending snapshot", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  server.threads.push({ ...server.thread, id: "t2", title: "新选择的任务" });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await screen.findByRole("link", { name: /新选择的任务/ });
  const old = deferred<Response>();
  let started = false;
  server.intercept((path) => {
    if (path.split("?")[0] === "/api/threads/t1") {
      started = true;
      return old.promise;
    }
    return undefined;
  });
  server.event("item/agentMessage/delta", { itemId: "old-item", delta: "old" });
  await waitFor(() => assert.equal(started, true));
  fireEvent.click(screen.getByRole("link", { name: /新选择的任务/ }));
  await screen.findByRole("heading", { name: "新选择的任务" });
  await act(async () => {
    old.resolve(
      response({
        thread: server.thread,
        interactions: [],
        items: [
          {
            id: "old-item",
            native_id: "old-item",
            thread_id: "t1",
            type: "agentMessage",
            data: { text: "过期的旧任务内容" },
          },
        ],
      }),
    );
    await old.promise;
  });
  assert.ok(screen.getByRole("heading", { name: "新选择的任务" }));
  assert.equal(screen.queryByText("过期的旧任务内容") === null, true);
});

test("all sessions remain reachable when the catalog contains more than 200 tasks", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  server.threads.splice(
    0,
    server.threads.length,
    ...Array.from({ length: 251 }, (_, index) => ({
      ...server.thread,
      id: `paged-${index + 1}`,
      title: `分页会话 ${index + 1}`,
    })),
  );
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await screen.findByText("分页会话 50");
  for (const boundary of [100, 150, 200, 250, 251]) {
    fireEvent.click(screen.getByRole("button", { name: /加载.*更多会话/ }));
    await screen.findByText(`分页会话 ${boundary}`, {}, { timeout: 3000 });
  }
  assert.equal(
    screen.queryByRole("button", { name: /加载.*更多会话/ }) === null,
    true,
  );
});

test("load more follows the server next_offset rather than assuming a full first page", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  const offsets: number[] = [];
  server.intercept((path) => {
    if (!path.startsWith("/api/threads?")) return undefined;
    const offset = Number(
      new URLSearchParams(path.split("?")[1]).get("offset"),
    );
    offsets.push(offset);
    return response(
      offset === 0
        ? {
            threads: [{ ...server.thread, id: "first", title: "第一页元数据" }],
            has_more: true,
            next_offset: 17,
          }
        : {
            threads: [
              { ...server.thread, id: "second", title: "后续页元数据" },
            ],
            has_more: false,
            next_offset: null,
          },
    );
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新任务列表" }));
  await screen.findByText("第一页元数据");
  fireEvent.click(screen.getByRole("button", { name: /加载.*更多会话/ }));
  await screen.findByText("后续页元数据");
  assert.ok(offsets.includes(17));
  assert.equal(offsets.includes(50), false);
});

test("bounded latest-page refreshes preserve loaded old messages until the history snapshot changes", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  let version = "snapshot-1",
    latestText = "最新消息",
    olderReads = 0;
  server.intercept((path) => {
    if (path.split("?")[0] !== "/api/threads/t1") return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("item_limit"), "100");
    const older = query.has("before_position");
    if (older) olderReads += 1;
    return response({
      thread: {
        ...server.thread,
        history_loaded: true,
        history_synced_at: version,
      },
      interactions: [],
      items: [
        {
          id: older ? "old-item" : "new-item",
          native_id: older ? "old-item" : "new-item",
          thread_id: "t1",
          type: "agentMessage",
          position: older ? 1 : 101,
          data: { text: older ? "更早的消息" : latestText },
        },
      ],
      has_more_items: !older,
      next_before_position: older ? null : 101,
    });
  });
  server.event("item/completed", {});
  await screen.findByText("最新消息");
  fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  await screen.findByText("更早的消息");
  latestText = "实时更新后的消息";
  server.event("item/agentMessage/delta", {
    itemId: "new-item",
    delta: "chunk",
  });
  await screen.findByText("实时更新后的消息");
  assert.ok(screen.getByText("更早的消息"));
  assert.equal(olderReads, 1);
  version = "snapshot-2";
  latestText = "新快照中的消息";
  server.event("item/completed", {});
  await screen.findByText("新快照中的消息");
  assert.equal(screen.queryByText("更早的消息") === null, true);
});

test("a reference to an older item searches history by bounded pages", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  let olderReads = 0;
  server.intercept((path) => {
    if (path.split("?")[0] !== "/api/threads/t1") return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("item_limit"), "100");
    const older = query.has("before_position");
    if (older) olderReads += 1;
    return response({
      thread: { ...server.thread, history_loaded: true },
      interactions: [],
      items: [
        {
          id: older ? "source-old" : "recent",
          native_id: older ? "source-old" : "recent",
          thread_id: "t1",
          type: "agentMessage",
          position: older ? 1 : 101,
          data: { text: older ? "引用定位到的原始消息" : "最新页" },
        },
      ],
      has_more_items: !older,
      next_before_position: older ? null : 101,
    });
  });
  act(() => {
    window.history.pushState({}, "", "/tasks/t1?item=source-old");
    window.dispatchEvent(new Event("popstate"));
  });
  server.event("item/completed", {});
  await screen.findByText("引用定位到的原始消息");
  assert.equal(olderReads, 1);
});

test("compact sidebar prioritizes sessions and keeps manual history inside the device menu", async () => {
  setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  assert.ok(screen.getByRole("button", { name: "新对话" }));
  assert.equal(screen.queryByText("我的工作空间") === null, true);
  assert.equal(
    screen.queryByRole("button", { name: "更多本机历史" }) === null,
    true,
  );
  fireEvent.click(screen.getByLabelText("设备与设置"));
  await screen.findByRole("button", { name: "更多本机历史" });
});

test("older native history waits for cursor projection and preserves the loaded tail", async () => {
  const server = setup({
    history_loaded: true,
    history_complete: false,
    history_cursor: "cursor-1",
    history_generation: "generation-1",
    history_synced_at: "first-sync",
  });
  let requests = 0,
    projected = false,
    metadataReads = 0;
  server.intercept((path, init) => {
    if (path === "/api/threads/t1/history") {
      requests += 1;
      assert.equal(init?.method, "POST");
      assert.deepEqual(JSON.parse(String(init.body)), {
        cursor: "cursor-1",
        limit: 50,
      });
      return response({
        thread: server.thread,
        command: {
          id: "older-command",
          kind: "thread.import",
          status: "completed",
          result: { itemCount: 1 },
        },
      });
    }
    if (path.split("?")[0] !== "/api/threads/t1") return undefined;
    const query = new URLSearchParams(path.split("?")[1]);
    const limit = query.get("item_limit");
    assert.ok(limit === "100" || limit === "1");
    if (limit === "1") {
      metadataReads += 1;
      if (metadataReads >= 2) projected = true;
    }
    const thread = {
      ...server.thread,
      ...(projected
        ? {
            history_cursor: null,
            history_complete: true,
            history_synced_at: "older-sync",
          }
        : {}),
    };
    return response({
      thread,
      interactions: [],
      items:
        limit === "1"
          ? []
          : [
              {
                id: query.has("before_position") ? "native-old" : "tail",
                native_id: query.has("before_position") ? "native-old" : "tail",
                thread_id: "t1",
                type: "agentMessage",
                position: query.has("before_position") ? 50 : 100,
                data: {
                  text: query.has("before_position")
                    ? "原生较早页"
                    : "保留的尾页",
                },
              },
            ],
      has_more_items: false,
      next_before_position: null,
    });
  });
  await screen.findByText("保留的尾页");
  assert.equal(requests, 0);
  fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  await screen.findByText("原生较早页");
  assert.ok(screen.getByText("保留的尾页"));
  assert.equal(requests, 1);
  assert.equal(metadataReads, 2);
  assert.equal(
    screen.queryByRole("button", { name: "加载更早消息" }) === null,
    true,
  );
});
