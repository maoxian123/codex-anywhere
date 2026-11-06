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
  within,
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
    return (FakeNotification.permission = "granted");
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
  thread_id: "thread-one",
  host_id: "h1",
  thread_title: `Conversation ${id}`,
  host_name: "Workstation",
  method: "anywhere/message_questions",
  created_at: "2026-11-02T12:00:00Z",
  question_count: 1,
  blocking: false,
  ...overrides,
});
const interaction = (id: string, status = "pending"): Interaction => ({
  id,
  thread_id: "thread-one",
  method: "anywhere/message_questions",
  status,
  params: {
    isBlocking: false,
    questions: [
      {
        id: "approach",
        header: "方案",
        question: `Question ${id}`,
        isOther: true,
        options: [
          { label: "Proceed", description: "Use the existing approach" },
          { label: "Revise", description: "Change the approach" },
        ],
      },
    ],
  },
});
const page = (
  items: PendingInteraction[],
  total = items.length,
  next: number | null = null,
): InteractionPage => ({
  interactions: items,
  total,
  has_more: next !== null,
  next_offset: next,
});
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
const props = {
  activeThreadId: "thread-one",
  refreshKey: 0,
  hosts: [host],
  onNavigate: (_id: string) => {},
  onCommand: (_command: Command) => {},
};
const bell = () => screen.getByRole("button", { name: "待处理交互" });
const panel = () => screen.getByRole("region", { name: "当前轮次问题" });
const reminder = () => screen.getByRole("region", { name: "待处理提醒" });
function server(initial: PendingInteraction[] = [metadata("one")]) {
  const state = {
    items: initial,
    details: new Map(initial.map((item) => [item.id, interaction(item.id)])),
    paths: [] as string[],
    posts: [] as unknown[],
    listError: false,
    detailError: false,
  };
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    state.paths.push(path);
    if (path.includes("?"))
      return state.listError
        ? response({ error: "Temporary list outage" }, 503)
        : response(page(state.items));
    const id = path.split("/")[3];
    if (init?.method === "POST") {
      state.posts.push(JSON.parse(String(init.body)));
      state.items = state.items.filter((item) => item.id !== id);
      return response(
        {
          command: {
            id: `answer-${id}`,
            kind: "interaction.answer",
            status: "queued",
          },
        },
        202,
      );
    }
    if (state.detailError)
      return response({ error: "Temporary detail outage" }, 503);
    return response({ interaction: state.details.get(id) || interaction(id) });
  };
  return state;
}

test("current question and choices appear on arrival without opening a dialog, focusing or submitting", async () => {
  const state = server();
  render(
    <>
      <input aria-label="Current draft" defaultValue="keep draft" />
      <InteractionAttention {...props} />
    </>,
  );
  const draft = screen.getByRole("textbox", {
    name: "Current draft",
  }) as HTMLInputElement;
  draft.focus();
  await screen.findByRole("radio", { name: /Proceed/ });
  assert.ok(within(panel()).getByText("Question one"));
  assert.match(reminder().textContent || "", /进入对应会话回答/);
  assert.equal(document.activeElement, draft);
  assert.equal(draft.value, "keep draft");
  assert.equal(screen.queryByRole("dialog"), null);
  assert.ok(
    screen
      .getAllByRole("radio")
      .every((radio) => !(radio as HTMLInputElement).checked),
  );
  assert.deepEqual(state.posts, []);
  assert.equal(FakeNotification.requests, 0);
  assert.deepEqual(state.paths, [
    "/api/interactions?limit=50&offset=0",
    "/api/interactions/one",
  ]);
});

test("other conversations only show a reminder until the user opens the owning conversation", async () => {
  const state = server([metadata("other", { thread_id: "thread-other" })]);
  state.details.set("other", {
    ...interaction("other"),
    thread_id: "thread-other",
  });
  const navigations: string[] = [];
  const view = render(
    <InteractionAttention
      {...props}
      onNavigate={(id) => navigations.push(id)}
    />,
  );
  await screen.findByRole("region", { name: "待处理提醒" });
  assert.equal(screen.queryByRole("radio"), null);
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  assert.deepEqual(state.paths, ["/api/interactions?limit=50&offset=0"]);
  fireEvent.click(bell());
  fireEvent.click(
    await screen.findByRole("button", { name: /Conversation other/ }),
  );
  assert.deepEqual(navigations, ["thread-other"]);
  assert.equal(screen.queryByRole("dialog"), null);
  assert.equal(screen.queryByRole("radio"), null);
  view.rerender(
    <InteractionAttention {...props} activeThreadId="thread-other" />,
  );
  await screen.findByRole("radio", { name: /Proceed/ });
  view.rerender(<InteractionAttention {...props} activeThreadId={null} />);
  assert.equal(screen.queryByRole("radio"), null);
  assert.ok(reminder());
  assert.deepEqual(state.posts, []);
});

test("switching conversations hides their forms and restores the previous custom answer", async () => {
  const state = server([
    metadata("one"),
    metadata("two", { thread_id: "thread-two" }),
  ]);
  state.details.set("two", { ...interaction("two"), thread_id: "thread-two" });
  const view = render(<InteractionAttention {...props} />);
  fireEvent.click(await screen.findByRole("radio", { name: "自定义回答" }));
  fireEvent.change(within(panel()).getByLabelText("方案：自定义回答"), {
    target: { value: "keep my answer" },
  });
  view.rerender(
    <InteractionAttention {...props} activeThreadId="thread-two" />,
  );
  await within(panel()).findByText("Question two");
  assert.equal(within(panel()).queryByText("Question one"), null);
  assert.equal(
    screen
      .queryByRole("textbox", { name: "方案：自定义回答" })
      ?.getAttribute("value"),
    null,
  );
  view.rerender(<InteractionAttention {...props} />);
  await within(panel()).findByText("Question one");
  assert.equal(
    (within(panel()).getByLabelText("方案：自定义回答") as HTMLTextAreaElement)
      .value,
    "keep my answer",
  );
  assert.deepEqual(state.posts, []);
});

test("a realtime refresh key loads a newly current question without waiting for the periodic timer", async () => {
  const state = server([]);
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.equal(state.paths.length, 1));
  state.items = [metadata("new")];
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  assert.ok(await screen.findByText("Question new"));
  assert.ok(screen.getByRole("radio", { name: /Proceed/ }));
  assert.equal(screen.queryByRole("dialog"), null);
});

test("a newer current question replaces an untouched selection", async () => {
  const state = server();
  const view = render(<InteractionAttention {...props} />);
  await screen.findByText("Question one");
  state.items = [
    metadata("one"),
    metadata("new", { created_at: "2026-11-02T12:01:00Z" }),
  ];
  await act(async () => {
    view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  });
  assert.ok(within(panel()).getByText("Question new"));
  assert.equal(
    screen.getAllByRole("region", { name: "当前轮次问题" }).length,
    1,
  );
});

test("new arrivals do not overwrite an answered draft and switching live questions preserves both forms", async () => {
  const state = server();
  const view = render(<InteractionAttention {...props} />);
  fireEvent.click(await screen.findByRole("radio", { name: "自定义回答" }));
  const draft = within(panel()).getByLabelText(
    "方案：自定义回答",
  ) as HTMLInputElement;
  fireEvent.change(draft, { target: { value: "my live answer" } });
  state.items = [
    metadata("one"),
    metadata("new", { created_at: "2026-11-02T12:01:00Z" }),
  ];
  await act(async () => {
    view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  });
  assert.match(reminder().textContent || "", /2 个问题/);
  assert.equal(draft.value, "my live answer");
  assert.ok(within(panel()).getByText("Question one"));
  fireEvent.click(bell());
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /Conversation new/ }));
  });
  assert.ok(within(panel()).getByText("Question new"));
  assert.equal(screen.queryByRole("dialog"), null);
  fireEvent.click(bell());
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /Conversation one/ }));
  });
  assert.equal(within(panel()).getByDisplayValue("my live answer"), draft);
  assert.equal(screen.getAllByRole("radio").length, 3);
  assert.deepEqual(state.posts, []);
});

test("authoritative turn-end removal clears the panel and aborts a late detail without reviving old questions", async () => {
  let items = [metadata("expired")];
  let resolveDetail!: (result: Response) => void;
  let signal: AbortSignal | null | undefined;
  globalThis.fetch = async (input, init) =>
    String(input).includes("?")
      ? response(page(items))
      : new Promise((accept) => {
          resolveDetail = accept;
          signal = init?.signal;
        });
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(resolveDetail));
  items = [];
  await act(async () => {
    view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  assert.equal(signal?.aborted, true);
  await act(async () => {
    resolveDetail(response({ interaction: interaction("expired") }));
  });
  assert.equal(screen.queryByText("Question expired"), null);
  assert.equal(screen.queryByRole("region", { name: "待处理提醒" }), null);
});

test("a stale previous list response cannot restore an expired current panel", async () => {
  const pending: ((result: Response) => void)[] = [];
  globalThis.fetch = async () => new Promise((accept) => pending.push(accept));
  const view = render(<InteractionAttention {...props} />);
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await act(async () => {
    pending[1](response(page([])));
    pending[0](response(page([metadata("old")])));
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
});

test("resolved or missing detail removes its current reminder instead of retaining an old question inbox", async () => {
  const state = server();
  state.details.set("one", interaction("one", "expired"));
  await act(async () => {
    render(<InteractionAttention {...props} />);
  });
  assert.equal(state.paths.length, 2);
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  assert.equal(screen.queryByRole("radio"), null);
  assert.equal(screen.queryByText("这条请求已处理或已过期。"), null);
});

for (const failure of ["listError", "detailError"] as const) {
  test(`${failure} preserves a live draft but requires confirmation before submitting`, async () => {
    const state = server();
    const view = render(<InteractionAttention {...props} />);
    fireEvent.click(await screen.findByRole("radio", { name: "自定义回答" }));
    const draft = within(panel()).getByLabelText(
      "方案：自定义回答",
    ) as HTMLInputElement;
    fireEvent.change(draft, { target: { value: "do not discard" } });
    state[failure] = true;
    await act(async () => {
      view.rerender(<InteractionAttention {...props} refreshKey={1} />);
    });
    assert.ok(screen.getByRole("button", { name: "重新确认当前问题" }));
    assert.equal(draft.value, "do not discard");
    assert.equal(
      (
        within(panel()).getByRole("button", {
          name: "提交回答",
        }) as HTMLButtonElement
      ).disabled,
      true,
    );
    state[failure] = false;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新确认当前问题" }));
    });
    assert.equal(
      (
        within(panel()).getByRole("button", {
          name: "提交回答",
        }) as HTMLButtonElement
      ).disabled,
      false,
    );
    assert.equal(draft.value, "do not discard");
    assert.deepEqual(state.posts, []);
  });
}

test("periodic confirmation keeps current answer selections and removes them when the turn ends", async () => {
  let tick: (() => void) | undefined;
  window.setInterval = ((callback: () => void) => {
    tick = callback;
    return 987;
  }) as typeof window.setInterval;
  const state = server();
  render(<InteractionAttention {...props} />);
  const option = (await screen.findByRole("radio", {
    name: /Revise/,
  })) as HTMLInputElement;
  fireEvent.click(option);
  await act(async () => {
    tick?.();
  });
  assert.equal(option.checked, true);
  state.items = [];
  await act(async () => {
    tick?.();
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
});

test("offline current questions remain visible while answer submission is disabled", async () => {
  server();
  render(
    <InteractionAttention {...props} hosts={[{ ...host, online: false }]} />,
  );
  await screen.findByRole("radio", { name: /Proceed/ });
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.ok(screen.getByText("Workstation · 设备离线"));
});

test("unknown current answers stay read-only and disappear with authoritative current-turn expiry", async () => {
  const state = server();
  state.details.set("one", {
    ...interaction("one", "answer_unknown"),
    params: { ...interaction("one").params, answerCommandStatus: "unknown" },
  });
  const view = render(<InteractionAttention {...props} />);
  await screen.findByText("回答结果未知，请先检查对话记录，避免重复提交。");
  assert.equal(screen.queryByRole("button", { name: "提交回答" }), null);
  state.items = [];
  await act(async () => {
    view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  assert.deepEqual(state.posts, []);
});

test("an explicit inline answer sends once and the server-confirmed answered question disappears", async () => {
  const state = server();
  render(<InteractionAttention {...props} />);
  fireEvent.click(await screen.findByRole("radio", { name: /Proceed/ }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  assert.deepEqual(state.posts, [
    { response: { answers: { approach: { answers: ["Proceed"] } } } },
  ]);
});

test("native approvals retain explicit dialog review without showing an inline question", async () => {
  const approval = metadata("approval", {
    method: "item/commandExecution/requestApproval",
    question_count: 0,
    blocking: true,
  });
  const state = server([approval]);
  state.details.set("approval", {
    id: approval.id,
    thread_id: approval.thread_id,
    method: approval.method,
    status: "pending",
    params: { command: "echo hello" },
  });
  render(<InteractionAttention {...props} />);
  await screen.findByRole("region", { name: "待处理提醒" });
  assert.equal(state.paths.length, 1);
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
  fireEvent.click(screen.getByRole("button", { name: "查看待处理" }));
  fireEvent.click(await screen.findByRole("button", { name: "允许这一次" }));
  await waitFor(() => assert.equal(state.posts.length, 1));
  assert.deepEqual(state.posts[0], { response: { decision: "accept" } });
});

test("unmount aborts both metadata and current detail requests", async () => {
  let detailSignal: AbortSignal | null | undefined;
  let resolve!: (result: Response) => void;
  globalThis.fetch = async (input, init) =>
    String(input).includes("?")
      ? response(page([metadata("one")]))
      : new Promise((accept) => {
          resolve = accept;
          detailSignal = init?.signal;
        });
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(resolve));
  view.unmount();
  assert.equal(detailSignal?.aborted, true);
  await act(async () => {
    resolve(response({ interaction: interaction("one") }));
  });
  assert.equal(screen.queryByRole("region", { name: "当前轮次问题" }), null);
});

test("desktop notifications remain generic and permission is requested only explicitly", async () => {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  const state = server([
    metadata("private", {
      thread_title: "PRIVATE TASK",
      host_name: "PRIVATE MACHINE",
    }),
  ]);
  render(<InteractionAttention {...props} />);
  await screen.findByRole("radio", { name: /Proceed/ });
  assert.equal(FakeNotification.requests, 0);
  fireEvent.click(bell());
  fireEvent.click(screen.getByRole("button", { name: "启用桌面提醒" }));
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  const notice = FakeNotification.instances[0];
  assert.equal(
    notice.options.body,
    "有新的问题或审批等待处理，点击打开页面查看。",
  );
  assert.doesNotMatch(
    JSON.stringify(notice.options),
    /PRIVATE|Question private|Proceed/,
  );
  assert.deepEqual(state.posts, []);
});

test("background notifications are deduplicated across current refreshes and remounts", async () => {
  FakeNotification.permission = "granted";
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  server();
  const view = render(<InteractionAttention {...props} />);
  await waitFor(() => assert.equal(FakeNotification.instances.length, 1));
  view.rerender(<InteractionAttention {...props} refreshKey={1} />);
  await screen.findByRole("radio", { name: /Proceed/ });
  view.unmount();
  render(<InteractionAttention {...props} />);
  await screen.findByRole("radio", { name: /Proceed/ });
  assert.equal(FakeNotification.instances.length, 1);
});

test("initial list failures remain visible and do not claim there are no current questions", async () => {
  const state = server();
  state.listError = true;
  render(<InteractionAttention {...props} />);
  await waitFor(() => assert.ok(bell().classList.contains("has-error")));
  fireEvent.click(bell());
  assert.ok(screen.getByText("Temporary list outage"));
  assert.equal(screen.queryByText("当前没有待处理的交互。"), null);
  state.listError = false;
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByRole("button", { name: /Conversation one/ });
  fireEvent.click(screen.getByRole("button", { name: /Conversation one/ }));
  await screen.findByRole("radio", { name: /Proceed/ });
  assert.equal(screen.queryByRole("dialog"), null);
});
