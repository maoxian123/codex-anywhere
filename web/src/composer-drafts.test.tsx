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
import Composer from "./Composer";
import {
  emptyComposerDraft,
  readComposerDraft,
  taskMentionAtCaret,
  writeComposerDraft,
} from "./composer-drafts";
import type { MessagePayload, Thread } from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  globalThis.fetch = originalFetch;
});

const thread = (id = "draft-a"): Thread => ({
  id,
  project_id: `project-${id}`,
  host_id: "host-draft",
  title: id,
  cwd: `F:\\${id}`,
  status: "idle",
  created_at: "now",
  updated_at: "now",
});
const props = {
  hostOnline: true,
  draftScope: "alice",
  onSend: async () => {},
  onInterrupt: async () => {},
  onNavigate: () => {},
};
const input = () =>
  screen.getByRole("textbox", {
    name: "发送消息给 Codex",
  }) as HTMLTextAreaElement;
const type = (value: string) =>
  fireEvent.change(input(), {
    target: { value, selectionStart: value.length },
  });

test("drafts restore text, uploaded assets, references and retry IDs within their user and project", () => {
  const draft = {
    text: "continue this work",
    assets: [
      {
        id: "asset-a",
        name: "image.png",
        mime: "image/png",
        size: 23,
        url: "/assets/a",
      },
    ],
    references: [
      { kind: "task" as const, thread_id: "source", item_id: "message" },
    ],
    retryRequestId: "unchanged-request",
  };
  writeComposerDraft("new:project-a", draft, "alice");
  assert.deepEqual(readComposerDraft("new:project-a", "alice"), draft);
  assert.deepEqual(
    readComposerDraft("new:project-b", "alice"),
    emptyComposerDraft(),
  );
  assert.deepEqual(
    readComposerDraft("new:project-a", "bob"),
    emptyComposerDraft(),
  );
  render(<Composer {...props} thread={thread("new:project-a")} />);
  assert.equal(input().value, draft.text);
  assert.ok(screen.getByRole("img", { name: "image.png" }));
  assert.ok(screen.getByRole("button", { name: /^移除引用/ }));
});

test("switching conversations, visiting settings and refreshing preserve independent drafts", () => {
  const view = render(<Composer {...props} thread={thread()} />);
  type("first draft");
  view.rerender(<Composer {...props} thread={thread("draft-b")} />);
  assert.equal(input().value, "");
  type("second draft");
  view.rerender(<Composer {...props} thread={thread()} />);
  assert.equal(input().value, "first draft");
  view.unmount();
  const restored = render(<Composer {...props} thread={thread()} />);
  assert.equal(input().value, "first draft");
  restored.unmount();
  render(<Composer {...props} thread={thread("draft-b")} />);
  assert.equal(input().value, "second draft");
});

test("failed sends retain the draft and retry ID across refresh and clear only after success", async () => {
  const requests: MessagePayload[] = [];
  const failed = render(
    <Composer
      {...props}
      thread={thread()}
      onSend={async (payload) => {
        requests.push(payload);
        throw new Error("connection interrupted");
      }}
    />,
  );
  type("keep this text");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("connection interrupted");
  assert.equal(input().value, "keep this text");
  const requestId = requests[0].client_request_id;
  assert.equal(readComposerDraft("draft-a", "alice").retryRequestId, requestId);
  failed.unmount();
  const retried = render(
    <Composer
      {...props}
      thread={thread()}
      onSend={async (payload) => {
        requests.push(payload);
      }}
    />,
  );
  assert.equal(input().value, "keep this text");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(input().value, ""));
  assert.equal(requests[1].client_request_id, requestId);
  retried.unmount();
  render(<Composer {...props} thread={thread()} />);
  assert.equal(input().value, "");
  assert.deepEqual(readComposerDraft("draft-a", "alice"), emptyComposerDraft());
});

test("a send completing after remount clears the visible sent draft without reviving it", async () => {
  let complete!: () => void;
  const sent = new Promise<void>((resolve) => {
    complete = resolve;
  });
  const first = render(
    <Composer {...props} thread={thread()} onSend={() => sent} />,
  );
  type("in flight");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  first.unmount();
  const second = render(<Composer {...props} thread={thread()} />);
  assert.equal(input().value, "in flight");
  await act(async () => {
    complete();
    await sent;
  });
  assert.equal(input().value, "");
  second.unmount();
  render(<Composer {...props} thread={thread()} />);
  assert.equal(input().value, "");
});

test("an old send completion preserves a newer draft entered after navigating back", async () => {
  let complete!: () => void;
  const sent = new Promise<void>((resolve) => {
    complete = resolve;
  });
  const first = render(
    <Composer {...props} thread={thread()} onSend={() => sent} />,
  );
  type("sent text");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  first.unmount();
  render(<Composer {...props} thread={thread()} />);
  type("new follow-up");
  await act(async () => {
    complete();
    await sent;
  });
  assert.equal(input().value, "new follow-up");
  assert.equal(readComposerDraft("draft-a", "alice").text, "new follow-up");
});

test("emails and code keep literal @ input without opening a modal or candidates", () => {
  render(<Composer {...props} thread={thread()} />);
  assert.equal(fireEvent.keyDown(input(), { key: "@" }), true);
  for (const value of [
    "name@example.com",
    "const email = 'a@b.com'",
    "`@decorator",
    "```python\n@decorator",
  ]) {
    type(value);
    assert.equal(input().value, value);
    assert.equal(screen.queryByRole("dialog"), null);
    assert.equal(screen.queryByRole("listbox"), null);
  }
  assert.deepEqual(taskMentionAtCaret("please @task", 12), {
    start: 7,
    end: 12,
    query: "task",
  });
  assert.equal(taskMentionAtCaret("foo@task", 8), null);
});

test("a whitespace @query opens nonmodal candidates, Escape preserves the text, and Enter selects", async () => {
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ threads: [thread("source-a"), thread("source-b")] }),
    );
  render(<Composer {...props} thread={thread()} />);
  type("please @source");
  await screen.findByRole("option", { name: /source-a/ });
  assert.equal(screen.queryByRole("dialog"), null);
  fireEvent.keyDown(input(), { key: "Escape" });
  assert.equal(screen.queryByRole("listbox"), null);
  assert.equal(input().value, "please @source");
  fireEvent.select(input());
  assert.equal(screen.queryByRole("listbox"), null);
  type("please @source-b");
  await screen.findByRole("option", { name: /source-b/ });
  fireEvent.keyDown(input(), { key: "ArrowDown" });
  fireEvent.keyDown(input(), { key: "Enter" });
  assert.equal(input().value, "please ");
  assert.equal(screen.queryByRole("listbox"), null);
  assert.equal(
    readComposerDraft("draft-a", "alice").references[0].thread_id,
    "source-b",
  );
  fireEvent.click(screen.getByRole("button", { name: "引用任务" }));
  assert.ok(screen.getByRole("dialog"));
  assert.ok(screen.getByRole("heading", { name: "引用其他任务" }));
});

test("model, effort and current versus next permissions remain visible without opening settings", () => {
  render(
    <Composer
      {...props}
      thread={{
        ...thread(),
        effective_settings: {
          model: "model-now",
          effort: "medium",
          sandbox_mode: "read-only",
          approval_policy: "on-request",
        },
        execution_settings: {
          model: "model-next",
          effort: "high",
          sandbox_mode: "workspace-write",
          approval_policy: "on-request",
        },
        settings_revision: 2,
        effective_settings_revision: 1,
      }}
    />,
  );
  assert.ok(screen.getByText("下次：model-next"));
  assert.ok(screen.getByText("high", { selector: ".composer-effort" }));
  assert.ok(screen.getByText(/当前权限：只读/));
  assert.ok(screen.getByText(/下次执行：model-next · high · 工作区写入/));
});
