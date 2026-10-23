import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Composer from "./Composer";
import MessageQueue from "./MessageQueue";
import type {
  MessagePayload,
  MessageQueueState,
  QueuedMessage,
  Thread,
} from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  globalThis.fetch = originalFetch;
});
const queued = (id: string, text: string): QueuedMessage => ({
  id,
  text,
  revision: 1,
  assets: [],
  references: [],
  created_at: "now",
  updated_at: "now",
});
const state = (...messages: QueuedMessage[]): MessageQueueState => ({
  messages,
  paused: false,
});
const thread: Thread = {
  id: "t1",
  host_id: "h1",
  title: "task",
  cwd: "F:\\work",
  status: "active",
  active_turn_id: "running",
  created_at: "now",
  updated_at: "now",
};
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
function queueView(initial: MessageQueueState, hostOnline = true) {
  let queue = initial;
  let refreshes = 0;
  const refresh = async () => {
    refreshes++;
    view.rerender(content());
  };
  const content = () => (
    <MessageQueue
      threadId="t1"
      queue={queue}
      hostOnline={hostOnline}
      onChange={refresh}
      onNavigate={() => {}}
    />
  );
  const view = render(content());
  return {
    ...view,
    setQueue: (next: MessageQueueState) => {
      queue = next;
    },
    refreshes: () => refreshes,
  };
}

test("queue renders FIFO previews with images and references and survives remount", () => {
  const queue = state(
    {
      ...queued("first", "first queued task"),
      assets: [
        {
          id: "image",
          name: "diagram.png",
          mime: "image/png",
          size: 2,
          url: "/api/assets/image",
        },
      ],
      references: [{ kind: "task", thread_id: "source", title: "source task" }],
    },
    queued("second", "second queued task"),
  );
  const view = queueView(queue);
  assert.deepEqual(
    screen
      .getAllByRole("listitem")
      .map((item) => item.querySelector(".message-queue-text")?.textContent),
    ["first queued task", "second queued task"],
  );
  assert.ok(screen.getByText("图片 · diagram.png"));
  assert.ok(screen.getByRole("link", { name: "source task" }));
  view.unmount();
  queueView(queue);
  assert.ok(screen.getByText("first queued task"));
  assert.ok(screen.getByText("second queued task"));
});

test("editing updates only queued text with its revision and preserves composer draft, assets and references", async () => {
  const item: QueuedMessage = {
    ...queued("first", "old queue text"),
    assets: [
      {
        id: "image",
        name: "diagram.png",
        mime: "image/png",
        size: 2,
        url: "/api/assets/image",
      },
    ],
    references: [{ kind: "task", thread_id: "source", title: "source task" }],
  };
  let queue = state(item);
  let request: { path: string; method?: string; body: unknown } | undefined;
  globalThis.fetch = async (input, init) => {
    request = {
      path: String(input),
      method: init?.method,
      body: JSON.parse(String(init?.body)),
    };
    queue = state({ ...item, text: "edited queued text", revision: 2 });
    return response({ message: queue.messages[0] });
  };
  const props = {
    thread,
    hostOnline: true,
    onSend: async () => {},
    onInterrupt: async () => {},
    onNavigate: () => {},
  };
  const view = render(
    <Composer
      {...props}
      queue={queue}
      onQueueChange={() => {
        view.rerender(<Composer {...props} queue={queue} />);
      }}
    />,
  );
  const composer = screen.getByRole("textbox", {
    name: "排队消息",
  }) as HTMLTextAreaElement;
  fireEvent.change(composer, { target: { value: "independent unsent draft" } });
  fireEvent.click(screen.getByRole("button", { name: "编辑第 1 条排队消息" }));
  fireEvent.change(
    screen.getByRole("textbox", { name: "编辑第 1 条排队消息" }),
    { target: { value: "edited queued text" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("edited queued text");
  assert.deepEqual(request, {
    path: "/api/threads/t1/queue/first",
    method: "PATCH",
    body: { text: "edited queued text", revision: 1 },
  });
  assert.equal(composer.value, "independent unsent draft");
  assert.ok(screen.getByText("图片 · diagram.png"));
  assert.ok(screen.getByRole("link", { name: "source task" }));
});

test("failed edits retain text and a changed revision cannot overwrite a newer message", async () => {
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    return response({ error: "save failed" }, 503);
  };
  const view = queueView(state(queued("first", "original")));
  fireEvent.click(screen.getByRole("button", { name: "编辑第 1 条排队消息" }));
  const edit = screen.getByRole("textbox", {
    name: "编辑第 1 条排队消息",
  }) as HTMLTextAreaElement;
  fireEvent.change(edit, { target: { value: "keep unsaved edit" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("save failed");
  assert.equal(edit.value, "keep unsaved edit");
  view.rerender(
    <MessageQueue
      threadId="t1"
      queue={state({ ...queued("first", "external edit"), revision: 2 })}
      hostOnline
      onNavigate={() => {}}
    />,
  );
  assert.ok(
    (screen.getByRole("button", { name: "保存" }) as HTMLButtonElement)
      .disabled,
  );
  assert.equal(edit.value, "keep unsaved edit");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  assert.equal(requests, 1);
});

test("cancel removes only the selected queued message and failure leaves it available", async () => {
  const view = queueView(
    state(queued("first", "first task"), queued("second", "second task")),
  );
  let fail = true;
  let body: unknown;
  globalThis.fetch = async (input, init) => {
    assert.equal(String(input), "/api/threads/t1/queue/first");
    assert.equal(init?.method, "DELETE");
    body = JSON.parse(String(init.body));
    if (fail) return response({ error: "cancel failed" }, 503);
    view.setQueue(state(queued("second", "second task")));
    return response({ cancelled: true });
  };
  fireEvent.click(screen.getByRole("button", { name: "取消第 1 条排队消息" }));
  await screen.findByText("cancel failed");
  assert.ok(screen.getByText("first task"));
  fail = false;
  await waitFor(() =>
    assert.equal(
      (
        screen.getByRole("button", {
          name: "取消第 1 条排队消息",
        }) as HTMLButtonElement
      ).disabled,
      false,
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "取消第 1 条排队消息" }));
  await waitFor(() => assert.ok(!screen.queryByText("first task")));
  assert.ok(screen.getByText("second task"));
  assert.deepEqual(body, { revision: 1 });
});

test("dispatched messages lose edit actions and conflicting unsaved text remains copyable", async () => {
  const view = queueView(state(queued("first", "already queued")));
  globalThis.fetch = async () => {
    view.setQueue(state());
    return response({ error: "message already dispatched" }, 409);
  };
  fireEvent.click(screen.getByRole("button", { name: "编辑第 1 条排队消息" }));
  fireEvent.change(
    screen.getByRole("textbox", { name: "编辑第 1 条排队消息" }),
    { target: { value: "unsaved correction" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("message already dispatched");
  const saved = (await screen.findByRole("textbox", {
    name: "未保存的排队消息",
  })) as HTMLTextAreaElement;
  assert.equal(saved.value, "unsaved correction");
  assert.equal(saved.readOnly, true);
  assert.ok(!screen.queryByRole("button", { name: "保存" }));
  assert.ok(!screen.queryByRole("button", { name: /取消第/ }));
});

test("cancelling another queued message preserves the current unsaved inline edit", async () => {
  const view = queueView(
    state(queued("first", "first task"), queued("second", "second task")),
  );
  globalThis.fetch = async () => {
    view.setQueue(state(queued("first", "first task")));
    return response({ cancelled: true });
  };
  fireEvent.click(screen.getByRole("button", { name: "编辑第 1 条排队消息" }));
  fireEvent.change(
    screen.getByRole("textbox", { name: "编辑第 1 条排队消息" }),
    { target: { value: "keep this unsaved edit" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "取消第 2 条排队消息" }));
  await waitFor(() => assert.ok(!screen.queryByText("second task")));
  assert.equal(
    (
      screen.getByRole("textbox", {
        name: "编辑第 1 条排队消息",
      }) as HTMLTextAreaElement
    ).value,
    "keep this unsaved edit",
  );
});

test("offline queues remain editable and cancellable but cannot resume", () => {
  queueView(
    {
      ...state(queued("first", "offline queued task")),
      paused: true,
      reason: "上一轮已中断。",
    },
    false,
  );
  assert.equal(
    (
      screen.getByRole("button", {
        name: "编辑第 1 条排队消息",
      }) as HTMLButtonElement
    ).disabled,
    false,
  );
  assert.equal(
    (
      screen.getByRole("button", {
        name: "取消第 1 条排队消息",
      }) as HTMLButtonElement
    ).disabled,
    false,
  );
  assert.equal(
    (screen.getByRole("button", { name: "继续队列" }) as HTMLButtonElement)
      .disabled,
    true,
  );
});

test("paused queues resume explicitly and waiting approval queues have no resume action", async () => {
  const view = queueView({
    ...state(queued("first", "pending task")),
    paused: true,
    reason: "上一轮已中断。",
  });
  globalThis.fetch = async (input, init) => {
    assert.equal(String(input), "/api/threads/t1/queue/resume");
    assert.equal(init?.method, "POST");
    assert.equal(init?.body, "{}");
    view.setQueue(state(queued("first", "pending task")));
    return response({
      command: null,
      queue: state(queued("first", "pending task")),
    });
  };
  fireEvent.click(screen.getByRole("button", { name: "继续队列" }));
  await waitFor(() =>
    assert.ok(!screen.queryByRole("button", { name: "继续队列" })),
  );
  assert.equal(view.refreshes(), 1);
});

test("Enter queues active and approval-waiting messages without interrupting the current turn", async () => {
  const sends: { payload: MessagePayload; steer: boolean }[] = [];
  let interrupts = 0;
  const props = {
    hostOnline: true,
    onSend: async (payload: MessagePayload, steer: boolean) => {
      sends.push({ payload, steer });
    },
    onInterrupt: async () => {
      interrupts++;
    },
    onNavigate: () => {},
  };
  const view = render(
    <Composer
      {...props}
      thread={thread}
      queue={state(queued("first", "already waiting"))}
    />,
  );
  for (const status of ["active", "waiting"]) {
    view.rerender(
      <Composer
        {...props}
        thread={{ ...thread, status }}
        queue={state(queued("first", "already waiting"))}
      />,
    );
    const input = screen.getByRole("textbox", {
      name: "排队消息",
    }) as HTMLTextAreaElement;
    await waitFor(() => assert.equal(input.disabled, false));
    fireEvent.change(input, { target: { value: `queued while ${status}` } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => assert.equal(input.value, ""));
  }
  assert.equal(sends.length, 2);
  assert.deepEqual(
    sends.map((send) => send.steer),
    [false, false],
  );
  assert.equal(interrupts, 0);
  assert.ok(screen.getByText("already waiting"));
});

test("an idle paused queue explicitly labels new messages as queued without steering or interrupt actions", async () => {
  const sends: { payload: MessagePayload; steer: boolean }[] = [];
  render(
    <Composer
      thread={{ ...thread, status: "idle", active_turn_id: undefined }}
      queue={{
        ...state(queued("first", "paused task")),
        paused: true,
        reason: "上一轮已中断。",
      }}
      hostOnline
      onSend={async (payload, steer) => {
        sends.push({ payload, steer });
      }}
      onInterrupt={async () => {
        throw new Error("must not interrupt");
      }}
      onNavigate={() => {}}
    />,
  );
  const input = screen.getByRole("textbox", {
    name: "排队消息",
  }) as HTMLTextAreaElement;
  assert.match(input.placeholder, /继续队列后/);
  assert.ok(screen.getByRole("button", { name: "加入队列" }));
  assert.ok(!screen.queryByRole("button", { name: "立即补充" }));
  assert.ok(!screen.queryByRole("button", { name: "中断当前执行" }));
  fireEvent.change(input, { target: { value: "append to the paused queue" } });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => assert.equal(input.value, ""));
  assert.equal(sends.length, 1);
  assert.equal(sends[0].steer, false);
  assert.ok(screen.getByText("paused task"));
  assert.ok(screen.getByRole("button", { name: "继续队列" }));
});
