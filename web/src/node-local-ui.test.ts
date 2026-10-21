import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

const directory = new URL(
  "../../codex_anywhere/node/local_ui/",
  import.meta.url,
);
const html = await readFile(new URL("index.html", directory), "utf8");
const source = await readFile(new URL("app.js", directory), "utf8");
type Status = {
  paired: boolean;
  running: boolean;
  connected: boolean;
  status: string;
  server: string;
  name: string;
  host_id: string;
  codex_bin: string;
  allow_insecure: boolean;
  last_error: string | null;
  platform: string;
};
const base: Status = {
  paired: false,
  running: false,
  connected: false,
  status: "unpaired",
  server: "",
  name: "测试电脑",
  host_id: "",
  codex_bin: "codex",
  allow_insecure: false,
  last_error: null,
  platform: "Windows",
};
const paired = {
  ...base,
  paired: true,
  status: "stopped",
  server: "https://hub.example",
  host_id: "host-one",
};
const browsers: JSDOM[] = [];
afterEach(() => {
  browsers.splice(0).forEach((browser) => browser.window.close());
});
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

async function setup(initial: Status = base) {
  const dom = new JSDOM(html, {
    url: "http://127.0.0.1:5780/",
    runScripts: "outside-only",
  });
  browsers.push(dom);
  const { window } = dom;
  let state = { ...initial };
  let csrf = "local-csrf";
  const intervals: { callback: () => void; delay: number }[] = [];
  window.setInterval = ((callback: () => void, delay: number) => {
    intervals.push({ callback, delay });
    return intervals.length;
  }) as typeof window.setInterval;
  const requests: {
    path: string;
    init: RequestInit;
    data: Record<string, unknown>;
  }[] = [];
  const replies = new Map<
    string,
    (data: Record<string, unknown>) => Response | Promise<Response>
  >();
  window.fetch = async (input, init = {}) => {
    const path = String(input);
    const data = JSON.parse(String(init.body || "{}")) as Record<
      string,
      unknown
    >;
    requests.push({ path, init, data });
    const reply = replies.get(path);
    if (reply) return reply(data);
    if (path === "/api/status")
      return json({ status: state, csrf_token: csrf });
    if (path === "/api/pair") state = { ...state, status: "pairing" };
    else if (path === "/api/start")
      state = { ...state, status: "starting", running: true };
    else if (path === "/api/stop") state = { ...state, status: "stopping" };
    else throw new Error(`Unexpected local API: ${path}`);
    return json({ status: state }, 202);
  };
  window.eval(source);
  await settle();
  const element = <T extends HTMLElement = HTMLElement>(id: string) =>
    window.document.getElementById(id) as T;
  return {
    window,
    requests,
    replies,
    intervals,
    element,
    setStatus(value: Partial<Status>) {
      state = { ...state, ...value };
    },
    setCsrf(value: string) {
      csrf = value;
    },
    input(id: string, value: string) {
      element<HTMLInputElement>(id).value = value;
    },
    async click(id: string) {
      element(id).click();
      await settle();
    },
    async submit() {
      element("pair-form").dispatchEvent(
        new window.Event("submit", { bubbles: true, cancelable: true }),
      );
      await settle();
    },
    async poll() {
      intervals[0].callback();
      await settle();
    },
    mutations(path: string) {
      return requests.filter(
        (entry) => entry.path === path && entry.init.method === "POST",
      );
    },
  };
}

test("local page polls status every two seconds and mutations use current CSRF with same-origin credentials", async () => {
  const ui = await setup(paired);
  assert.equal(ui.intervals[0].delay, 2000);
  assert.equal(ui.element("status-label").textContent, "已停止");
  assert.equal(ui.requests[0].path, "/api/status");
  assert.equal(ui.requests[0].init.credentials, "same-origin");
  assert.equal(
    new Headers(ui.requests[0].init.headers).has("X-CSRF-Token"),
    false,
  );
  ui.setCsrf("rotated-local-csrf");
  await ui.poll();
  await ui.click("start-node");
  const request = ui.mutations("/api/start")[0];
  assert.equal(
    new Headers(request.init.headers).get("X-CSRF-Token"),
    "rotated-local-csrf",
  );
  assert.equal(request.init.credentials, "same-origin");
  assert.equal(ui.element("status-label").textContent, "正在启动");
});

test("accepted pairing clears only its code and polling preserves the user's server, name and executable drafts", async () => {
  const ui = await setup();
  ui.input("server", "https://chosen.example/");
  ui.input("pair-code", "PAIR-SECRET");
  ui.input("device-name", "我的 Linux 主机");
  ui.input("codex-bin", "/opt/codex/bin/codex");
  ui.setStatus({
    server: "https://old.example",
    name: "旧设备名称",
    codex_bin: "old-codex",
  });
  await ui.poll();
  assert.equal(
    ui.element<HTMLInputElement>("server").value,
    "https://chosen.example/",
  );
  assert.equal(
    ui.element<HTMLInputElement>("device-name").value,
    "我的 Linux 主机",
  );
  assert.equal(
    ui.element<HTMLInputElement>("codex-bin").value,
    "/opt/codex/bin/codex",
  );
  await ui.submit();
  assert.deepEqual(ui.mutations("/api/pair")[0].data, {
    server: "https://chosen.example",
    name: "我的 Linux 主机",
    code: "PAIR-SECRET",
    codex_bin: "/opt/codex/bin/codex",
    allow_insecure: false,
  });
  assert.equal(ui.element<HTMLInputElement>("pair-code").value, "");
  assert.equal(
    ui.element<HTMLInputElement>("server").value,
    "https://chosen.example/",
  );
  assert.equal(
    ui.element<HTMLInputElement>("device-name").value,
    "我的 Linux 主机",
  );
  ui.setStatus({ status: "error", last_error: "配对码已过期" });
  await ui.poll();
  assert.equal(ui.element("error-message").textContent, "配对码已过期");
  assert.equal(
    ui.element<HTMLInputElement>("codex-bin").value,
    "/opt/codex/bin/codex",
  );
  assert.equal(ui.window.localStorage.length, 0);
  assert.equal(ui.window.sessionStorage.length, 0);
  assert.equal(
    ui.mutations("/api/start").length,
    0,
    "the local backend owns automatic startup after pairing",
  );
});

test("a rejected pair request preserves the entered code and fields for correction", async () => {
  const ui = await setup();
  ui.input("server", "https://chosen.example");
  ui.input("pair-code", "RETRY-CODE");
  ui.input("device-name", "工作电脑");
  ui.input("codex-bin", "C:\\Tools\\codex.exe");
  ui.replies.set("/api/pair", () => json({ error: "管理中心暂时不可用" }, 503));
  await ui.submit();
  assert.equal(ui.element("error-message").textContent, "管理中心暂时不可用");
  assert.equal(ui.element<HTMLInputElement>("pair-code").value, "RETRY-CODE");
  assert.equal(ui.element<HTMLInputElement>("device-name").value, "工作电脑");
  assert.equal(
    ui.element<HTMLInputElement>("codex-bin").value,
    "C:\\Tools\\codex.exe",
  );
});

test("HTTP requires an explicit advanced setting and is never enabled implicitly", async () => {
  const ui = await setup();
  ui.input("server", "http://192.168.1.2:5000");
  ui.input("pair-code", "CODE");
  assert.equal(ui.element<HTMLInputElement>("allow-insecure").checked, false);
  await ui.submit();
  assert.equal(ui.mutations("/api/pair").length, 0);
  assert.match(ui.element("error-message").textContent || "", /明确开启/);
  assert.equal(ui.element<HTMLDetailsElement>("advanced-settings").open, true);
  ui.element<HTMLInputElement>("allow-insecure").checked = true;
  await ui.submit();
  assert.equal(ui.mutations("/api/pair")[0].data.allow_insecure, true);
});

test("running nodes cannot be re-paired and stopping requires a separate explicit confirmation", async () => {
  const ui = await setup({
    ...paired,
    status: "connected",
    running: true,
    connected: true,
  });
  assert.equal(ui.element<HTMLButtonElement>("edit-pairing").disabled, true);
  await ui.click("edit-pairing");
  assert.equal(ui.element("pair-fields").hidden, true);
  await ui.submit();
  assert.equal(ui.mutations("/api/pair").length, 0);
  await ui.click("request-stop");
  assert.equal(ui.element("stop-confirmation").hidden, false);
  assert.match(
    ui.element("stop-confirmation").textContent || "",
    /中断本节点正在执行的任务/,
  );
  assert.equal(ui.mutations("/api/stop").length, 0);
  await ui.click("cancel-stop");
  assert.equal(ui.element("stop-confirmation").hidden, true);
  assert.equal(ui.mutations("/api/stop").length, 0);
  await ui.click("request-stop");
  await ui.click("confirm-stop");
  assert.equal(ui.mutations("/api/stop").length, 1);
  assert.equal(ui.element("status-label").textContent, "正在停止");
});

test("re-pairing a stopped node requires acknowledgement and sends replace_pairing without an implicit stop", async () => {
  const ui = await setup(paired);
  await ui.click("edit-pairing");
  assert.equal(ui.element("replace-confirmation").hidden, false);
  assert.equal(ui.element<HTMLButtonElement>("pair-submit").disabled, true);
  assert.match(
    ui.element("replace-confirmation").textContent || "",
    /会话资料/,
  );
  ui.input("server", "https://replacement.example");
  ui.input("pair-code", "NEW-CODE");
  await ui.submit();
  assert.equal(ui.mutations("/api/pair").length, 0);
  ui.element<HTMLInputElement>("replace-pairing").checked = true;
  ui.element("replace-pairing").dispatchEvent(new ui.window.Event("change"));
  assert.equal(ui.element<HTMLButtonElement>("pair-submit").disabled, false);
  await ui.submit();
  assert.equal(ui.mutations("/api/pair")[0].data.replace_pairing, true);
  assert.equal(ui.mutations("/api/stop").length, 0);
  assert.equal(ui.element<HTMLInputElement>("pair-code").value, "");
});

test("start errors stay visible, retain executable settings and allow explicit retry through startup to connection", async () => {
  const ui = await setup(paired);
  ui.input("codex-bin", "/missing/codex");
  ui.replies.set("/api/start", () =>
    json({ error: "找不到 Codex 可执行文件" }, 400),
  );
  await ui.click("start-node");
  assert.equal(
    ui.element("error-message").textContent,
    "找不到 Codex 可执行文件",
  );
  assert.equal(
    ui.element<HTMLInputElement>("codex-bin").value,
    "/missing/codex",
  );
  assert.equal(ui.element<HTMLButtonElement>("start-node").disabled, false);
  ui.replies.delete("/api/start");
  ui.input("codex-bin", "/usr/local/bin/codex");
  await ui.click("start-node");
  assert.equal(ui.element("status-label").textContent, "正在启动");
  assert.equal(ui.element("error-message").hidden, true);
  assert.equal(
    ui.mutations("/api/start").at(-1)?.data.codex_bin,
    "/usr/local/bin/codex",
  );
  ui.setStatus({
    status: "error",
    running: false,
    last_error: "Codex 启动失败，请检查路径",
  });
  await ui.poll();
  assert.equal(ui.element("start-node").textContent, "重试连接");
  assert.equal(
    ui.element("error-message").textContent,
    "Codex 启动失败，请检查路径",
  );
  ui.replies.set("/api/start", () =>
    json(
      {
        status: {
          ...paired,
          running: true,
          connected: true,
          status: "connected",
          last_error: null,
        },
      },
      202,
    ),
  );
  await ui.click("start-node");
  assert.equal(ui.element("status-label").textContent, "已连接");
  assert.equal(ui.element("error-message").hidden, true);
  assert.equal(ui.mutations("/api/stop").length, 0);
});

test("failed status polling can be retried and cannot inject markup into errors", async () => {
  const ui = await setup(paired);
  ui.replies.set("/api/status", () =>
    json({ error: "<img src=x onerror=alert(1)>读取失败" }, 500),
  );
  await ui.poll();
  assert.equal(ui.element("refresh-status").hidden, false);
  assert.equal(ui.element("error-message").querySelector("img"), null);
  assert.match(ui.element("error-message").textContent || "", /读取失败/);
  ui.replies.delete("/api/status");
  await ui.click("refresh-status");
  assert.equal(ui.element("error-message").hidden, true);
  assert.equal(ui.element("refresh-status").hidden, true);
});

test("a status response started before a mutation cannot roll back its newer startup state", async () => {
  const ui = await setup(paired);
  let resolveStatus: (response: Response) => void = () => {};
  ui.replies.set(
    "/api/status",
    () =>
      new Promise<Response>((resolve) => {
        resolveStatus = resolve;
      }),
  );
  await ui.poll();
  await ui.click("start-node");
  assert.equal(ui.element("status-label").textContent, "正在启动");
  resolveStatus(json({ status: paired, csrf_token: "outdated" }));
  await settle();
  assert.equal(ui.element("status-label").textContent, "正在启动");
  assert.equal(ui.element<HTMLButtonElement>("start-node").disabled, true);
});
