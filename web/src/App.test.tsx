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
  const authentication = {
    authenticated: true,
    username: "test",
    csrf_token: "csrf",
  };
  const loginHeaders: Headers[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
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
      result = { threads: [thread] };
    } else if (path === "/api/threads/t1")
      result = { thread, items: [], interactions: [] };
    else throw new Error(`Unexpected test request: ${path}`);
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
    refreshes: () => refreshes,
    event: (method: string, params: Record<string, unknown>) => {
      const event: HubEvent = {
        type: "event",
        id: Date.now(),
        thread_id: "t1",
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
