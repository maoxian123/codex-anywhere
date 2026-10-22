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
import NewConversation from "./NewConversation";
import { pendingConversation, startConversation } from "./conversation-start";
import { readComposerDraft } from "./composer-drafts";
import type { Host, MessagePayload, Project, Thread } from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  sessionStorage.clear();
});
const hosts: Host[] = [
  { id: "h1", name: "工作电脑", platform: "windows", online: true },
];
const project = (id: string): Project => ({
  id,
  host_id: "h1",
  name: id,
  cwd: `F:\\${id}`,
  thread_count: 0,
  created_at: "",
  updated_at: "",
});
const thread: Thread = {
  id: "created",
  host_id: "h1",
  project_id: "p2",
  native_id: "native-created",
  title: "修复问题",
  cwd: "F:\\p2",
  status: "idle",
  created_at: "",
  updated_at: "",
};
const command = (id = "start", status = "completed") => ({
  id,
  kind: id === "start" ? "thread.start" : "turn.start",
  status,
  result: {},
});
const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
const payload: MessagePayload = {
  text: "修复问题",
  asset_ids: [],
  references: [],
  client_request_id: "first-message-request",
};

test("new conversation opens an editor in the selected project and creates nothing until send", async () => {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) : {} });
    if (path === "/api/threads")
      return json({
        thread: { ...thread, native_id: undefined, status: "starting" },
        command: command(),
      });
    if (path.includes("?item_limit=1"))
      return json({ thread, items: [], interactions: [] });
    if (path.endsWith("/messages"))
      return json({ command: command("message") });
    throw new Error(path);
  };
  let created: Thread | undefined;
  render(
    <NewConversation
      hosts={hosts}
      projects={[project("p1"), project("p2")]}
      initialProjectId="p2"
      loading={false}
      draftScope="new-flow"
      onNewProject={() => {}}
      onPair={() => {}}
      onNavigate={() => {}}
      onCreated={(value) => {
        created = value;
      }}
    />,
  );
  assert.equal(
    (screen.getByRole("combobox", { name: "工作项目" }) as HTMLSelectElement)
      .value,
    "p2",
  );
  assert.equal(screen.queryByRole("textbox", { name: "任务名称" }), null);
  const input = screen.getByRole("textbox", { name: "发送消息给 Codex" });
  assert.equal(calls.length, 0);
  fireEvent.change(input, { target: { value: "修复问题\n保留详细说明" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(created?.id, "created"));
  assert.deepEqual(
    calls.map((entry) => entry.path),
    [
      "/api/threads",
      "/api/threads/created?item_limit=1",
      "/api/threads/created/messages",
    ],
  );
  assert.equal(calls[0].body.project_id, "p2");
  assert.equal(calls[0].body.title, "修复问题");
  assert.equal(calls[2].body.text, "修复问题\n保留详细说明");
  assert.equal(readComposerDraft("new:p2", "new-flow").text, "");
});

test("project changes remain selectable and drafts stay with their own project", () => {
  render(
    <NewConversation
      hosts={hosts}
      projects={[project("a"), project("b")]}
      initialProjectId="b"
      loading={false}
      draftScope="project-drafts"
      onNewProject={() => {}}
      onPair={() => {}}
      onNavigate={() => {}}
      onCreated={() => {}}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "B 项目的草稿" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "工作项目" }), {
    target: { value: "a" },
  });
  assert.equal(
    (screen.getByRole("combobox", { name: "工作项目" }) as HTMLSelectElement)
      .value,
    "a",
  );
  assert.equal(
    (
      screen.getByRole("textbox", {
        name: "发送消息给 Codex",
      }) as HTMLTextAreaElement
    ).value,
    "",
  );
  fireEvent.change(screen.getByRole("combobox", { name: "工作项目" }), {
    target: { value: "b" },
  });
  assert.equal(
    (
      screen.getByRole("textbox", {
        name: "发送消息给 Codex",
      }) as HTMLTextAreaElement
    ).value,
    "B 项目的草稿",
  );
});

test("lost creation response reuses the original creation key even when a later payload changes", async () => {
  const requests: Record<string, unknown>[] = [];
  let first = true;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path === "/api/threads") {
      requests.push(JSON.parse(String(init?.body)));
      if (first) {
        first = false;
        throw new Error("response lost");
      }
      return json({ thread, command: command() });
    }
    if (path.includes("?item_limit=1"))
      return json({ thread, items: [], interactions: [] });
    if (path.endsWith("/messages"))
      return json({ command: command("message") });
    throw new Error(path);
  };
  await assert.rejects(
    startConversation(project("lost"), {}, payload, "lost-response", () => {}),
  );
  assert.ok(pendingConversation("lost", "lost-response"));
  await startConversation(
    project("lost"),
    { effort: "high" },
    { ...payload, client_request_id: "edited-message", text: "调整后的要求" },
    "lost-response",
    () => {},
  );
  assert.deepEqual(requests[1], requests[0]);
  assert.equal(pendingConversation("lost", "lost-response"), null);
});

test("an unknown creation is never repeated or followed by a message", async () => {
  let creations = 0,
    messages = 0;
  globalThis.fetch = async (input) => {
    const path = String(input);
    if (path === "/api/threads") {
      creations++;
      return json({ thread, command: command("start", "unknown") });
    }
    if (path.endsWith("/messages")) messages++;
    throw new Error(path);
  };
  for (let attempt = 0; attempt < 2; attempt++)
    await assert.rejects(
      startConversation(
        project("unknown"),
        {},
        payload,
        "unknown-command",
        () => {},
      ),
    );
  assert.equal(creations, 1);
  assert.equal(messages, 0);
});

test("a completed send from an unmounted new page does not erase a newer draft", async () => {
  let finishMessage!: (response: Response) => void;
  const message = new Promise<Response>((resolve) => {
    finishMessage = resolve;
  });
  let submitted = false;
  globalThis.fetch = async (input) => {
    const path = String(input);
    if (path === "/api/threads") return json({ thread, command: command() });
    if (path.includes("?item_limit=1"))
      return json({ thread, items: [], interactions: [] });
    if (path.endsWith("/messages")) {
      submitted = true;
      return message;
    }
    throw new Error(path);
  };
  const props = {
    hosts,
    projects: [project("late")],
    loading: false,
    draftScope: "late-completion",
    onNewProject() {},
    onPair() {},
    onNavigate() {},
    onCreated() {},
  };
  const first = render(<NewConversation {...props} />);
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "第一条需求" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(submitted, true));
  first.unmount();
  render(<NewConversation {...props} />);
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "返回后新写的草稿" },
  });
  await act(async () => {
    finishMessage(json({ command: command("message") }));
    await message;
  });
  assert.equal(
    (
      screen.getByRole("textbox", {
        name: "发送消息给 Codex",
      }) as HTMLTextAreaElement
    ).value,
    "返回后新写的草稿",
  );
});

test("a known failed creation can be explicitly abandoned without discarding the draft", async () => {
  let creations = 0;
  globalThis.fetch = async (input) => {
    const path = String(input);
    if (path === "/api/threads") {
      creations++;
      return json({ thread, command: command("start", "failed") });
    }
    throw new Error(path);
  };
  render(
    <NewConversation
      hosts={hosts}
      projects={[project("failed")]}
      loading={false}
      draftScope="failed-creation"
      onNewProject={() => {}}
      onPair={() => {}}
      onNavigate={() => {}}
      onCreated={() => {}}
    />,
  );
  const input = screen.getByRole("textbox", { name: "发送消息给 Codex" });
  fireEvent.change(input, { target: { value: "保留这条需求" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "开始另一条新对话" }),
  );
  assert.equal(pendingConversation("failed", "failed-creation"), null);
  assert.equal((input as HTMLTextAreaElement).value, "保留这条需求");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(creations, 2));
  await screen.findByRole("button", { name: "开始另一条新对话" });
});
