import "./test-dom";
import test, { afterEach, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import InteractionAttention from "./InteractionAttention";
import type {
  Command,
  Host,
  Interaction,
  InteractionPage,
  PendingInteraction,
} from "./types";

const originalFetch = globalThis.fetch;
const originalNotification = Object.getOwnPropertyDescriptor(
  globalThis,
  "Notification",
);
const originalHidden = Object.getOwnPropertyDescriptor(document, "hidden");
const originalHasFocus = document.hasFocus;
const originalFocus = window.focus;
const originalTitle = document.title;
const originalSetInterval = window.setInterval;
const originalClearInterval = window.clearInterval;

class FakeNotification {
  static permission: NotificationPermission = "default";
  static requests = 0;
  static instances: FakeNotification[] = [];
  static async requestPermission(): Promise<NotificationPermission> {
    FakeNotification.requests++;
    FakeNotification.permission = "granted";
    return "granted";
  }
  onclick: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(
    public title: string,
    public options: NotificationOptions = {},
  ) {
    FakeNotification.instances.push(this);
  }
  close() {
    this.closed = true;
    this.onclose?.();
  }
}

beforeEach(() => {
  FakeNotification.permission = "default";
  FakeNotification.requests = 0;
  FakeNotification.instances = [];
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    writable: true,
    value: FakeNotification,
  });
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: false,
  });
  document.hasFocus = () => true;
  window.focus = () => {};
  document.title = "Codex Anywhere";
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  if (originalNotification)
    Object.defineProperty(globalThis, "Notification", originalNotification);
  else Reflect.deleteProperty(globalThis, "Notification");
  if (originalHidden) Object.defineProperty(document, "hidden", originalHidden);
  else Reflect.deleteProperty(document, "hidden");
  document.hasFocus = originalHasFocus;
  window.focus = originalFocus;
  document.title = originalTitle;
  window.setInterval = originalSetInterval;
  window.clearInterval = originalClearInterval;
  sessionStorage.clear();
});

const host: Host = {
  id: "h1",
  name: "Workstation",
  platform: "windows",
  online: true,
};
const metadata = (
  id: string,
  overrides: Partial<PendingInteraction> = {},
): PendingInteraction => ({
  id,
  thread_id: `thread-${id}`,
  host_id: "h1",
  thread_title: `Conversation ${id}`,
  host_name: "Workstation",
  method: "item/tool/requestUserInput",
  created_at: "2026-10-26T12:00:00Z",
  question_count: 1,
  blocking: true,
  ...overrides,
});
const page = (
  interactions: PendingInteraction[],
  total = interactions.length,
  next: number | null = null,
): InteractionPage => ({
  interactions,
  total,
  next_offset: next,
  has_more: next !== null,
});
const interaction = (id: string, status = "pending"): Interaction => ({
  id,
  thread_id: `thread-${id}`,
  method: "item/tool/requestUserInput",
  status,
  params: {
    isBlocking: true,
    questions: [
      {
        id: "approach",
        header: "方案",
        question: `Question ${id}`,
        options: [
          { label: "Proceed", description: "Use the existing approach" },
          { label: "Revise", description: "Change the approach" },
        ],
      },
    ],
  },
});
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
const props = {
  refreshKey: 0,
  hosts: [host],
  onNavigate: (_id: string) => {},
  onCommand: (_command: Command) => {},
};
const bell = () => screen.getByRole("button", { name: "待处理交互" });
const open = () => fireEvent.click(bell());

test("unseen conversations update the bell and tab title without opening or stealing focus", async () => {
  document.title = "My workspace";
  const item = metadata("background", { host_name: "Other machine" });
  const paths: string[] = [];
  globalThis.fetch = async (input) => {
    paths.push(String(input));
    return response(page([item], 7));
  };
  const view = render(
    <>
      <input aria-label="Current draft" autoFocus />
      <InteractionAttention {...props} />
    </>,
  );
  const draft = screen.getByRole("textbox", { name: "Current draft" });
  await waitFor(() =>
    assert.equal(document.title, "[7 待处理] Codex Anywhere"),
  );
  assert.ok(bell().textContent?.includes("7"));
  assert.equal(document.activeElement, draft);
  assert.equal(screen.queryByRole("dialog"), null);
  assert.ok(screen.getByRole("status").textContent?.includes("7"));
  assert.deepEqual(paths, ["/api/interactions?limit=50&offset=0"]);
  assert.equal(FakeNotification.requests, 0);
  open();
  assert.ok(await screen.findByText("Conversation background"));
  assert.ok(screen.getByText("Other machine · 1 个问题"));
  view.unmount();
  assert.equal(document.title, "My workspace");
});

test("pagination remains expanded across refresh and pending metadata never loads full questions automatically", async () => {
  const paths: string[] = [];
  globalThis.fetch = async (input) => {
    const path = String(input);
    paths.push(path);
    return response(
      path.endsWith("offset=50")
        ? page([metadata("second")], 2)
        : page([metadata("first")], 2, 50),
    );
  };
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("2")));
  open();
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  assert.ok(await screen.findByText("Conversation second"));
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await waitFor(() => assert.equal(paths.length, 4));
  assert.ok(screen.getByText("Conversation first"));
  assert.ok(screen.getByText("Conversation second"));
  assert.ok(paths.every((path) => path.startsWith("/api/interactions?")));
});

test("selection loads the native form with no preselected answer and optional conversation navigation", async () => {
  let navigation = "";
  globalThis.fetch = async (input) =>
    response(
      String(input).includes("?")
        ? page([metadata("one")])
        : { interaction: interaction("one") },
    );
  render(
    <InteractionAttention
      {...props}
      onNavigate={(id) => {
        navigation = id;
      }}
    />,
  );
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /Conversation one/ }));
  assert.ok(await screen.findByText("Question one"));
  assert.ok(
    screen
      .getAllByRole("radio")
      .every((radio) => !(radio as HTMLInputElement).checked),
  );
  fireEvent.click(screen.getByRole("button", { name: "打开会话" }));
  assert.equal(navigation, "thread-one");
  assert.equal(screen.queryByRole("dialog"), null);
});

test("an unbound native request is answerable in the panel without a conversation link", async () => {
  globalThis.fetch = async (input) =>
    response(
      String(input).includes("?")
        ? page([
            metadata("unbound", {
              thread_id: null,
              thread_title: "未关联会话",
            }),
          ])
        : { interaction: { ...interaction("unbound"), thread_id: null } },
    );
  render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /未关联会话/ }));
  await screen.findByText("Question unbound");
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    false,
  );
  assert.equal(screen.queryByRole("button", { name: "打开会话" }), null);
});

test("a slow previous detail cannot replace a later selected question", async () => {
  let resolveFirst!: (value: Response) => void;
  let firstSignal: AbortSignal | null | undefined;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path.includes("?"))
      return response(page([metadata("first"), metadata("second")]));
    if (path.endsWith("/first")) {
      firstSignal = init?.signal;
      return new Promise((resolve) => {
        resolveFirst = resolve;
      });
    }
    return response({ interaction: interaction("second") });
  };
  render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("2")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /Conversation first/ }));
  await waitFor(() => assert.ok(resolveFirst));
  fireEvent.click(screen.getByRole("button", { name: "返回列表" }));
  fireEvent.click(screen.getByRole("button", { name: /Conversation second/ }));
  await screen.findByText("Question second");
  assert.equal(firstSignal?.aborted, true);
  await act(async () => {
    resolveFirst(response({ interaction: interaction("first") }));
  });
  assert.equal(screen.queryByText("Question first"), null);
  assert.ok(screen.getByText("Conversation second"));
});

test("stale list responses are ignored and unmount aborts outstanding refreshes", async () => {
  const pending: {
    resolve: (value: Response) => void;
    signal?: AbortSignal | null;
  }[] = [];
  globalThis.fetch = async (_input, init) =>
    new Promise((resolve) => {
      pending.push({ resolve, signal: init?.signal });
    });
  const view = render(<InteractionAttention {...props} />);
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  assert.equal(pending[0].signal?.aborted, true);
  await act(async () => {
    pending[1].resolve(response(page([metadata("new")], 3)));
  });
  assert.equal(document.title, "[3 待处理] Codex Anywhere");
  await act(async () => {
    pending[0].resolve(response(page([metadata("old")], 1)));
  });
  assert.equal(document.title, "[3 待处理] Codex Anywhere");
  view.rerender(<InteractionAttention {...props} refreshKey={2} />);
  view.unmount();
  assert.equal(pending[2].signal?.aborted, true);
  await act(async () => {
    pending[2].resolve(response(page([], 0)));
  });
  assert.equal(document.title, "Codex Anywhere");
});

test("transient refresh errors preserve pending entries and clear after recovery", async () => {
  let failed = false;
  globalThis.fetch = async () =>
    failed
      ? response({ error: "Temporary outage" }, 503)
      : response(page([metadata("kept")]));
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  failed = true;
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await screen.findByText("Temporary outage");
  assert.ok(bell().classList.contains("has-error"));
  assert.ok(bell().getAttribute("title")?.includes("无法更新"));
  assert.ok(screen.getByText("Conversation kept"));
  assert.equal(document.title, "[1 待处理] Codex Anywhere");
  failed = false;
  view.rerender(<InteractionAttention {...props} refreshKey={2} />);
  await waitFor(() => assert.ok(!screen.queryByText("Temporary outage")));
  assert.ok(!bell().classList.contains("has-error"));
});

test("a non-advancing expanded pagination response preserves the last good list", async () => {
  let broken = false;
  let requests = 0;
  globalThis.fetch = async (input) => {
    requests++;
    return response(
      String(input).endsWith("offset=50")
        ? broken
          ? page([], 2, 50)
          : page([metadata("second")], 2)
        : page([metadata("first")], 2, 50),
    );
  };
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("2")));
  open();
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  await screen.findByText("Conversation second");
  broken = true;
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await screen.findByText("待处理列表正在变化，请刷新后重试。");
  assert.equal(requests, 4);
  assert.ok(screen.getByText("Conversation first"));
  assert.ok(screen.getByText("Conversation second"));
});

test("a request missing from a complete reminder refresh is read-only until its detail resyncs", async () => {
  let disappeared = false;
  let resolveDetail!: (value: Response) => void;
  globalThis.fetch = async (input) => {
    if (String(input).includes("?"))
      return response(page(disappeared ? [] : [metadata("one")]));
    if (disappeared)
      return new Promise((resolve) => {
        resolveDetail = resolve;
      });
    return response({ interaction: interaction("one") });
  };
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /Conversation one/ }));
  await screen.findByText("Question one");
  disappeared = true;
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await screen.findByText("同步交互状态");
  assert.ok(!screen.queryByRole("button", { name: "提交回答" }));
  assert.ok(screen.getByText("Conversation one"));
  await act(async () => {
    resolveDetail(response({ interaction: interaction("one", "resolved") }));
  });
  await screen.findByText("这条请求已处理或已过期。");
});

test("answering and resolved detail resync preserves selected context and never resubmits", async () => {
  let current = "pending";
  let posted = 0;
  let commandCount = 0;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (init?.method === "POST") {
      posted++;
      assert.deepEqual(JSON.parse(String(init.body)), {
        response: { answers: { approach: { answers: ["Proceed"] } } },
      });
      current = "answering";
      return response({
        command: { id: "answer", kind: "interaction.answer", status: "queued" },
      });
    }
    return response(
      path.includes("?")
        ? page(current === "pending" ? [metadata("one")] : [])
        : { interaction: interaction("one", current) },
    );
  };
  const view = render(
    <InteractionAttention
      {...props}
      onCommand={() => {
        commandCount++;
      }}
    />,
  );
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /Conversation one/ }));
  fireEvent.click(await screen.findByRole("radio", { name: /Proceed/ }));
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  await waitFor(() => assert.equal(document.title, "Codex Anywhere"));
  assert.ok(screen.getByText("Conversation one"));
  assert.ok(screen.getByText("Workstation"));
  assert.equal(posted, 1);
  assert.equal(commandCount, 1);
  current = "resolved";
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await screen.findByText("这条请求已处理或已过期。");
  assert.equal(screen.queryByRole("button", { name: "提交回答" }), null);
  assert.equal(posted, 1);
});

test("offline devices disable submitting an answer while retaining question content", async () => {
  globalThis.fetch = async (input) =>
    response(
      String(input).includes("?")
        ? page([metadata("offline")])
        : { interaction: interaction("offline") },
    );
  render(
    <InteractionAttention {...props} hosts={[{ ...host, online: false }]} />,
  );
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  fireEvent.click(screen.getByRole("button", { name: /Conversation offline/ }));
  await screen.findByText("Question offline");
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.ok(screen.getByText("Workstation · 节点离线"));
});

test("focus, online, visibility and periodic fallback each refresh attention", async () => {
  let requests = 0;
  let timerCallback: (() => void) | undefined;
  let cleared = false;
  window.setInterval = ((callback: () => void, delay: number) => {
    assert.equal(delay, 30_000);
    timerCallback = callback;
    return 987;
  }) as typeof window.setInterval;
  window.clearInterval = ((id: number) => {
    assert.equal(id, 987);
    cleared = true;
  }) as typeof window.clearInterval;
  globalThis.fetch = async () => {
    requests++;
    return response(page([]));
  };
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.equal(requests, 1));
  for (const [target, name] of [
    [window, "focus"],
    [window, "online"],
    [document, "visibilitychange"],
  ] as const) {
    const previous = requests;
    act(() => {
      target.dispatchEvent(new Event(name));
    });
    await waitFor(() => assert.equal(requests, previous + 1));
  }
  act(() => {
    timerCallback?.();
  });
  await waitFor(() => assert.equal(requests, 5));
  view.unmount();
  assert.equal(cleared, true);
  window.dispatchEvent(new Event("online"));
  assert.equal(requests, 5);
});

test("desktop notification permission is requested only by explicit enable and messages stay generic", async () => {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  globalThis.fetch = async () =>
    response(
      page([
        metadata("sensitive", {
          thread_title: "PRIVATE SECRET TASK",
          host_name: "PRIVATE MACHINE",
        }),
      ]),
    );
  render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  assert.equal(FakeNotification.requests, 0);
  assert.equal(FakeNotification.instances.length, 0);
  open();
  fireEvent.click(screen.getByRole("button", { name: "启用桌面提醒" }));
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  assert.equal(FakeNotification.requests, 1);
  const notification = FakeNotification.instances[0];
  assert.equal(notification.title, "Codex Anywhere · 待处理交互");
  assert.equal(
    notification.options.body,
    "有新的问题或审批等待处理，点击打开页面查看。",
  );
  assert.ok(!JSON.stringify(notification.options).includes("PRIVATE"));
  assert.ok(screen.getByRole("button", { name: "桌面提醒已启用" }));
});

test("background desktop notifications are deduplicated across event replay and remount", async () => {
  FakeNotification.permission = "granted";
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  let items = [metadata("one")];
  globalThis.fetch = async () => response(page(items));
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await waitFor(() =>
    assert.equal(document.title, "[1 待处理] Codex Anywhere"),
  );
  assert.equal(FakeNotification.instances.length, 1);
  view.unmount();
  const remount = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  assert.equal(FakeNotification.instances.length, 1);
  items = [...items, metadata("two")];
  remount.rerender(<InteractionAttention {...props} refreshKey={2} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 2));
  assert.equal(FakeNotification.requests, 0);
});

test("pending count increases beyond the loaded page trigger one generic reminder and pagination does not notify again", async () => {
  FakeNotification.permission = "granted";
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  const firstPage = Array.from({ length: 50 }, (_, index) =>
    metadata(String(index)),
  );
  let total = 51;
  globalThis.fetch = async (input) =>
    response(
      String(input).endsWith("offset=50")
        ? page([metadata("50"), metadata("51")], total)
        : page(firstPage, total, 50),
    );
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  total = 52;
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 2));
  assert.equal(
    FakeNotification.instances[1].options.body,
    "有新的问题或审批等待处理，点击打开页面查看。",
  );
  view.rerender(<InteractionAttention {...props} refreshKey={2} />);
  await waitFor(() =>
    assert.equal(document.title, "[52 待处理] Codex Anywhere"),
  );
  open();
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  await screen.findByText("Conversation 51");
  assert.equal(FakeNotification.instances.length, 2);
});

test("foreground requests use in-page reminders, and notification clicks focus the existing page", async () => {
  FakeNotification.permission = "granted";
  let focused = 0;
  window.focus = () => {
    focused++;
  };
  globalThis.fetch = async () => response(page([metadata("one")]));
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  assert.equal(FakeNotification.instances.length, 0);
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  act(() => {
    FakeNotification.instances[0].onclick?.();
  });
  assert.equal(focused, 1);
  assert.ok(screen.getByRole("dialog"));
  assert.equal(screen.queryByRole("radio"), null);
  assert.equal(FakeNotification.instances[0].closed, true);
});

test("denied and unsupported desktop notifications retain usable in-page reminders", async () => {
  FakeNotification.permission = "denied";
  globalThis.fetch = async () => response(page([metadata("one")]));
  const denied = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  assert.ok(screen.getByText("桌面提醒已被浏览器关闭，可在站点设置中开启。"));
  assert.equal(
    (screen.getByRole("button", { name: "启用桌面提醒" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  denied.unmount();
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: undefined,
  });
  render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().textContent?.includes("1")));
  open();
  assert.ok(screen.getByText("此浏览器不支持桌面提醒，仍会显示页面内提示。"));
  assert.ok(screen.getByText("Conversation one"));
  assert.equal(FakeNotification.requests, 0);
});
