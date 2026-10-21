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
import SettingsPage from "./SettingsPage";
import { api, setSession } from "./api";
import type { Session } from "./types";

const originalFetch = globalThis.fetch;
const initialSettings = { administrator: { username: "admin" } };
const updatedSession: Session = {
  authenticated: true,
  username: "new-admin",
  csrf_token: "rotated-csrf",
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

function input(label: string) {
  return screen.getByLabelText(label) as HTMLInputElement;
}

function change(label: string, value: string) {
  fireEvent.change(input(label), { target: { value } });
}

function submit() {
  fireEvent.submit(
    screen.getByRole("button", { name: "保存更改" }).closest("form")!,
  );
}

beforeEach(() => {
  setSession({ authenticated: true, csrf_token: "original-csrf" });
});

afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});

test("settings load shows the account section without reading a saved password", async () => {
  let resolveLoad!: (value: Response) => void;
  globalThis.fetch = async (path, init) => {
    assert.equal(String(path), "/api/settings");
    assert.equal(new Headers(init?.headers).get("X-CSRF-Token"), null);
    return new Promise<Response>((resolve) => {
      resolveLoad = resolve;
    });
  };
  render(<SettingsPage onSession={() => {}} />);
  assert.ok(screen.getByText("正在加载配置"));
  assert.ok(screen.getByRole("heading", { name: "应用配置" }));
  assert.equal(
    screen.getByRole("link", { name: "账户" }).getAttribute("aria-current"),
    "page",
  );
  await act(async () => resolveLoad(json(initialSettings)));
  await screen.findByLabelText("管理员用户名");
  assert.equal(input("管理员用户名").value, "admin");
  assert.equal(input("管理员用户名").autocomplete, "username");
  assert.equal(input("管理员用户名").maxLength, 120);
  assert.equal(input("当前密码").value, "");
  assert.equal(input("当前密码").autocomplete, "current-password");
  assert.equal(input("当前密码").required, true);
  assert.equal(input("新密码").value, "");
  assert.equal(input("新密码").autocomplete, "new-password");
  assert.equal(input("新密码").maxLength, 1024);
  assert.equal(input("确认新密码").autocomplete, "new-password");
});

test("a failed settings load can be retried", async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    reads += 1;
    return reads === 1
      ? json({ error: "配置暂时不可用" }, 503)
      : json(initialSettings);
  };
  render(<SettingsPage onSession={() => {}} />);
  await screen.findByRole("alert");
  assert.ok(screen.getByText("配置暂时不可用"));
  assert.equal(screen.queryByLabelText("当前密码"), null);
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByLabelText("管理员用户名");
  assert.equal(reads, 2);
  assert.equal(screen.queryByRole("alert"), null);
});

test("leaving settings cancels its pending read", () => {
  let signal: AbortSignal | null | undefined;
  globalThis.fetch = async (_path, init) => {
    signal = init?.signal;
    return new Promise<Response>(() => {});
  };
  const view = render(<SettingsPage onSession={() => {}} />);
  assert.equal(signal?.aborted, false);
  view.unmount();
  assert.equal(signal?.aborted, true);
});

test("username-only saves omit the new password and rotate CSRF before updating the app session", async () => {
  const requests: { path: string; init?: RequestInit }[] = [];
  const sessions: Session[] = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init });
    if (String(path) === "/api/settings") return json(initialSettings);
    if (String(path) === "/api/follow-up") return json({ ok: true });
    return json({
      settings: { administrator: { username: "new-admin" } },
      session: updatedSession,
    });
  };
  render(
    <SettingsPage
      onSession={(session) => {
        sessions.push(session);
        void api("/follow-up", { method: "POST" });
      }}
    />,
  );
  await screen.findByLabelText("管理员用户名");
  change("管理员用户名", "  new-admin  ");
  change("当前密码", "  current password  ");
  submit();
  await screen.findByRole("status");
  assert.deepEqual(sessions, [updatedSession]);
  assert.equal(requests[1].path, "/api/settings/administrator");
  assert.equal(requests[1].init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), {
    username: "new-admin",
    current_password: "  current password  ",
  });
  assert.equal(
    new Headers(requests[1].init?.headers).get("X-CSRF-Token"),
    "original-csrf",
  );
  assert.equal(
    new Headers(requests[2].init?.headers).get("X-CSRF-Token"),
    "rotated-csrf",
  );
  assert.equal(input("管理员用户名").value, "new-admin");
  assert.equal(input("当前密码").value, "");
  assert.ok(screen.getByText("管理员账号已更新"));
  assert.ok(screen.getByText("其他登录会话将失效，当前浏览器保持登录。"));
});

test("password updates preserve whitespace and clear all password fields after saving", async () => {
  let payload: unknown;
  globalThis.fetch = async (path, init) => {
    if (String(path) === "/api/settings") return json(initialSettings);
    payload = JSON.parse(String(init?.body));
    return json({ settings: initialSettings, session: updatedSession });
  };
  render(<SettingsPage onSession={() => {}} />);
  await screen.findByLabelText("当前密码");
  change("当前密码", "old-password");
  change("新密码", "  new-password  ");
  change("确认新密码", "  new-password  ");
  submit();
  await screen.findByRole("status");
  assert.deepEqual(payload, {
    username: "admin",
    current_password: "old-password",
    new_password: "  new-password  ",
  });
  for (const label of ["当前密码", "新密码", "确认新密码"])
    assert.equal(input(label).value, "");
});

test("mismatched passwords are rejected locally and preserve the entered values", async () => {
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return json(initialSettings);
  };
  render(<SettingsPage onSession={() => {}} />);
  await screen.findByLabelText("当前密码");
  change("当前密码", "old-password");
  change("新密码", "new-password");
  change("确认新密码", "different-password");
  submit();
  await screen.findByText("两次输入的新密码不一致。");
  assert.equal(requests, 1);
  assert.equal(input("当前密码").value, "old-password");
  assert.equal(input("新密码").value, "new-password");
  assert.equal(input("确认新密码").value, "different-password");
});

test("an incorrect current password keeps the draft available for retry", async () => {
  let updates = 0;
  const sessions: Session[] = [];
  globalThis.fetch = async (path) => {
    if (String(path) === "/api/settings") return json(initialSettings);
    updates += 1;
    return updates === 1
      ? json({ error: "当前密码不正确" }, 400)
      : json({ settings: initialSettings, session: updatedSession });
  };
  render(<SettingsPage onSession={(session) => sessions.push(session)} />);
  await screen.findByLabelText("当前密码");
  change("管理员用户名", "draft-admin");
  change("当前密码", "wrong-password");
  change("新密码", "new-password");
  change("确认新密码", "new-password");
  submit();
  await screen.findByText("当前密码不正确");
  assert.equal(sessions.length, 0);
  assert.equal(input("管理员用户名").value, "draft-admin");
  assert.equal(input("当前密码").value, "wrong-password");
  assert.equal(input("新密码").value, "new-password");
  assert.equal(input("确认新密码").value, "new-password");
  change("当前密码", "right-password");
  submit();
  await screen.findByRole("status");
  assert.equal(updates, 2);
  assert.deepEqual(sessions, [updatedSession]);
  assert.equal(screen.queryByRole("alert"), null);
});

test("a pending save disables fields and prevents duplicate submissions", async () => {
  let updates = 0;
  let resolveUpdate!: (value: Response) => void;
  globalThis.fetch = async (path) => {
    if (String(path) === "/api/settings") return json(initialSettings);
    updates += 1;
    return new Promise<Response>((resolve) => {
      resolveUpdate = resolve;
    });
  };
  render(<SettingsPage onSession={() => {}} />);
  await screen.findByLabelText("当前密码");
  change("当前密码", "old-password");
  const form = screen
    .getByRole("button", { name: "保存更改" })
    .closest("form")!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  assert.equal(updates, 1);
  assert.equal(input("当前密码").closest("fieldset")?.disabled, true);
  assert.equal(
    (
      screen.getByRole("button", { name: "正在保存…" }) as HTMLButtonElement
    ).closest("fieldset")?.disabled,
    true,
  );
  await act(async () =>
    resolveUpdate(json({ settings: initialSettings, session: updatedSession })),
  );
  await waitFor(() =>
    assert.equal(input("当前密码").closest("fieldset")?.disabled, false),
  );
});
