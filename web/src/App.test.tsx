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
import type { HubEvent, Thread } from "./types";

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

function setup(threadOverride: Partial<Thread> = {}) {
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
  const threads = [thread];
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
    else if (path === "/api/hosts")
      result = {
        hosts: [
          {
            id: "h1",
            name: "测试设备",
            platform: "windows",
            online: true,
            roots: ["C:\\work"],
          },
        ],
      };
    else if (path.startsWith("/api/threads?")) {
      refreshes += 1;
      result = { threads };
    } else if (path === "/api/threads/t1")
      result = { thread, items: [], interactions: [] };
    else if (path.startsWith("/api/threads/")) {
      const found = threads.find(
        (entry) => path === `/api/threads/${entry.id}`,
      );
      if (!found) throw new Error(`Unexpected test request: ${path}`);
      result = { thread: found, items: [], interactions: [] };
    } else throw new Error(`Unexpected test request: ${path}`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  window.history.replaceState({}, "", "/tasks/t1");
  render(<App />);
  return {
    thread,
    authentication,
    loginHeaders,
    threads,
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

test("continuous streaming still paints snapshots when requests take longer than the refresh interval", async () => {
  const server = setup();
  await screen.findByRole("textbox", { name: "发送消息给 Codex" });
  let inFlight = 0;
  let peak = 0;
  server.intercept((path) => {
    if (path !== "/api/threads/t1") return undefined;
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
          roots: ["C:\\work"],
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
            roots: ["C:\\work"],
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
    if (path === "/api/threads/t1") {
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
