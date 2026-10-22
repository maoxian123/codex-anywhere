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
import HostSettings from "./HostSettings";
import { setSession } from "./api";

const originalFetch = globalThis.fetch;
const machine = {
  id: "host-one",
  name: "工作电脑",
  platform: "Windows",
  online: true,
  version: "0.1.0",
  data: {
    projects: 3,
    threads: 12,
    messages: 1024,
    attachments: 5,
    attachment_bytes: 2 * 1024 * 1024,
  },
};
const server = {
  ...machine,
  id: "linux-server",
  name: "Linux 服务器",
  platform: "Linux",
  online: false,
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

function confirmation() {
  return screen.getByLabelText("输入机器名称以确认删除") as HTMLInputElement;
}

function typeName(value = machine.name) {
  fireEvent.change(confirmation(), { target: { value } });
}

async function openDeletion(name = machine.name) {
  await screen.findByRole("article", { name });
  fireEvent.click(
    screen.getByRole("button", { name: `删除 ${name} 的机器与数据` }),
  );
  await screen.findByRole("heading", { name: "删除机器与管理中心数据" });
}

function submitDeletion() {
  fireEvent.submit(confirmation().closest("form")!);
}

beforeEach(() => {
  setSession({ authenticated: true, csrf_token: "host-settings-csrf" });
});

afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});

test("host settings load authenticated hub data and display status and statistics", async () => {
  let resolveLoad!: (response: Response) => void;
  globalThis.fetch = async (path, init) => {
    assert.equal(String(path), "/api/settings/hosts");
    assert.equal(new Headers(init?.headers).get("X-CSRF-Token"), null);
    assert.equal(init?.credentials, "same-origin");
    return new Promise<Response>((resolve) => {
      resolveLoad = resolve;
    });
  };
  render(<HostSettings />);
  assert.ok(screen.getByText("正在加载机器列表"));
  await act(async () => resolveLoad(json({ hosts: [machine, server] })));
  const windows = within(
    await screen.findByRole("article", { name: machine.name }),
  );
  assert.ok(windows.getByText("在线"));
  assert.ok(windows.getByText("Windows · 0.1.0"));
  const statistics = windows.getByLabelText("管理中心保存的数据");
  assert.deepEqual(
    [...statistics.querySelectorAll("dt")].map(
      (element) => element.textContent,
    ),
    ["项目", "会话", "消息", "附件", "附件大小"],
  );
  assert.deepEqual(
    [...statistics.querySelectorAll("dd")].map(
      (element) => element.textContent,
    ),
    ["3", "12", "1,024", "5", "2 MB"],
  );
  assert.ok(
    within(screen.getByRole("article", { name: server.name })).getByText(
      "离线",
    ),
  );
  assert.equal(screen.queryByText("正在加载机器列表"), null);
});

test("an empty machine list keeps refresh available", async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    reads += 1;
    return json({ hosts: [] });
  };
  render(<HostSettings />);
  await screen.findByText("还没有已配对的机器。");
  fireEvent.click(screen.getByRole("button", { name: "刷新机器列表" }));
  await waitFor(() => assert.equal(reads, 2));
  assert.equal(screen.queryByRole("article"), null);
});

test("a failed machine list can be retried without affecting account settings", async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    reads += 1;
    return reads === 1
      ? json({ error: "机器列表暂时不可用" }, 503)
      : json({ hosts: [machine] });
  };
  render(<HostSettings />);
  await screen.findByText("机器列表暂时不可用");
  fireEvent.click(screen.getByRole("button", { name: "重新加载机器列表" }));
  await screen.findByRole("article", { name: machine.name });
  assert.equal(reads, 2);
  assert.equal(screen.queryByRole("alert"), null);
});

test("deletion explains the complete scope and requires the exact typed machine name", async () => {
  let deletions = 0;
  globalThis.fetch = async (_path, init) => {
    if (init?.method === "DELETE") {
      deletions += 1;
      return json({ deleted: true, host_id: machine.id });
    }
    return json({ hosts: [machine] });
  };
  render(<HostSettings />);
  await openDeletion();
  assert.ok(
    screen.getByText(
      `将删除管理中心保存的“${machine.name}”机器记录、项目、会话、消息和附件，此操作无法撤销。`,
    ),
  );
  assert.ok(screen.getByText("该机器的连接将失效，再次连接需要重新配对。"));
  assert.ok(
    screen.getByText(
      "机器上的原始 Codex 会话和项目文件会保留，本地正在运行的任务不会因本操作终止。",
    ),
  );
  const button = screen.getByRole("button", {
    name: "确认删除机器与数据",
  }) as HTMLButtonElement;
  assert.equal(button.disabled, true);
  submitDeletion();
  typeName(` ${machine.name} `);
  submitDeletion();
  assert.equal(deletions, 0);
  assert.equal(button.disabled, true);
  typeName();
  assert.equal(button.disabled, false);
  assert.equal(deletions, 0);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  assert.equal(screen.queryByLabelText("输入机器名称以确认删除"), null);
  assert.ok(screen.getByRole("article", { name: machine.name }));
});

test("pending deletion uses the encoded endpoint and CSRF JSON once then removes the card and notifies the app", async () => {
  const host = { ...machine, id: "host/one#中文" };
  const deleted: string[] = [];
  const requests: { path: string; init?: RequestInit }[] = [];
  let resolveDelete!: (response: Response) => void;
  globalThis.fetch = async (path, init) => {
    if (init?.method === "DELETE") {
      requests.push({ path: String(path), init });
      return new Promise<Response>((resolve) => {
        resolveDelete = resolve;
      });
    }
    return json({ hosts: [host, server] });
  };
  render(<HostSettings onHostDeleted={(id) => deleted.push(id)} />);
  await openDeletion();
  typeName();
  const form = confirmation().closest("form")!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, `/api/hosts/${encodeURIComponent(host.id)}`);
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
    confirm_name: machine.name,
  });
  assert.equal(
    new Headers(requests[0].init?.headers).get("X-CSRF-Token"),
    "host-settings-csrf",
  );
  assert.equal(confirmation().closest("fieldset")?.disabled, true);
  assert.equal(
    (screen.getByRole("button", { name: "正在删除…" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  const cancel = new Event("cancel", { cancelable: true });
  fireEvent(screen.getByRole("dialog"), cancel);
  assert.equal(cancel.defaultPrevented, true);
  fireEvent.click(screen.getByRole("button", { name: "关闭窗口" }));
  assert.ok(screen.getByLabelText("输入机器名称以确认删除"));
  await act(async () =>
    resolveDelete(json({ deleted: true, host_id: host.id })),
  );
  await screen.findByText(
    `已删除“${machine.name}”在管理中心保存的机器记录与数据。`,
  );
  assert.equal(screen.queryByRole("article", { name: machine.name }), null);
  assert.ok(screen.getByRole("article", { name: server.name }));
  assert.equal(screen.queryByRole("dialog"), null);
  assert.deepEqual(deleted, [host.id]);
  assert.equal(requests.length, 1);
});

test("failed deletion preserves the card and confirmation for a deliberate retry", async () => {
  let deletions = 0;
  const callbacks: string[] = [];
  globalThis.fetch = async (_path, init) => {
    if (init?.method === "DELETE") {
      deletions += 1;
      return deletions === 1
        ? json({ error: "删除暂时失败，请重试" }, 503)
        : json({ deleted: true, host_id: machine.id });
    }
    return json({ hosts: [machine] });
  };
  render(<HostSettings onHostDeleted={(id) => callbacks.push(id)} />);
  await openDeletion();
  typeName();
  submitDeletion();
  await screen.findByText("删除暂时失败，请重试");
  assert.equal(confirmation().value, machine.name);
  assert.equal(confirmation().closest("fieldset")?.disabled, false);
  assert.ok(screen.getByRole("article", { name: machine.name }));
  assert.deepEqual(callbacks, []);
  submitDeletion();
  await screen.findByText(
    `已删除“${machine.name}”在管理中心保存的机器记录与数据。`,
  );
  assert.equal(deletions, 2);
  assert.deepEqual(callbacks, [machine.id]);
});

test("successful deletion surfaces cleanup warnings while still removing the deleted machine", async () => {
  const callbacks: string[] = [];
  globalThis.fetch = async (_path, init) =>
    init?.method === "DELETE"
      ? json({
          deleted: true,
          host_id: machine.id,
          warning: "部分附件文件清理失败，请稍后处理。",
          cleanup: { failed: 2, missing: 0, deleted: 3 },
          retained_shared_assets: 1,
        })
      : json({ hosts: [machine] });
  render(<HostSettings onHostDeleted={(id) => callbacks.push(id)} />);
  await openDeletion();
  typeName();
  submitDeletion();
  await screen.findByText(/部分附件文件清理失败，请稍后处理。/);
  assert.ok(screen.getByText(/已保留其他机器仍在使用的共享附件。/));
  assert.ok(screen.getByRole("status"));
  assert.equal(screen.queryByRole("article", { name: machine.name }), null);
  assert.deepEqual(callbacks, [machine.id]);
});

test("refresh errors preserve already displayed machines and a retry updates their metadata", async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    reads += 1;
    if (reads === 2) return json({ error: "刷新失败" }, 503);
    return json({
      hosts: [{ ...machine, name: reads === 3 ? "新机器名" : machine.name }],
    });
  };
  render(<HostSettings />);
  await screen.findByRole("article", { name: machine.name });
  fireEvent.click(screen.getByRole("button", { name: "刷新机器列表" }));
  await screen.findByText("刷新失败");
  assert.ok(screen.getByRole("article", { name: machine.name }));
  fireEvent.click(screen.getByRole("button", { name: "重新加载机器列表" }));
  await screen.findByRole("article", { name: "新机器名" });
  assert.equal(screen.queryByRole("article", { name: machine.name }), null);
});

test("leaving machine settings aborts the pending read", () => {
  let signal: AbortSignal | null | undefined;
  globalThis.fetch = async (_path, init) => {
    signal = init?.signal;
    return new Promise<Response>(() => {});
  };
  const view = render(<HostSettings />);
  assert.equal(signal?.aborted, false);
  view.unmount();
  assert.equal(signal?.aborted, true);
});
