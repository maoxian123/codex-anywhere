import test from "node:test";
import assert from "node:assert/strict";
import { api, ApiError, post, setSession } from "./api.ts";

test("JSON mutations carry CSRF and same-origin credentials; GET never sends CSRF", async () => {
  const original = globalThis.fetch;
  const calls: RequestInit[] = [];
  globalThis.fetch = async (_input, init) => {
    calls.push(init || {});
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    setSession({ authenticated: true, csrf_token: "test-csrf-token" });
    await post("/threads", { title: "test" });
    await api("/hosts");
    assert.equal(
      new Headers(calls[0].headers).get("X-CSRF-Token"),
      "test-csrf-token",
    );
    assert.equal(
      new Headers(calls[0].headers).get("Content-Type"),
      "application/json",
    );
    assert.equal(calls[0].credentials, "same-origin");
    assert.equal(new Headers(calls[1].headers).get("X-CSRF-Token"), null);
  } finally {
    globalThis.fetch = original;
  }
});

test("multipart upload does not overwrite the browser-generated boundary", async () => {
  const original = globalThis.fetch;
  let headers = new Headers();
  globalThis.fetch = async (_input, init) => {
    headers = new Headers(init?.headers);
    return new Response("{}", { status: 200 });
  };
  try {
    const form = new FormData();
    form.append("file", new Blob(["test"], { type: "image/png" }), "test.png");
    await api("/assets", { method: "POST", body: form });
    assert.equal(headers.get("Content-Type"), null);
    assert.equal(headers.get("X-CSRF-Token"), "test-csrf-token");
  } finally {
    globalThis.fetch = original;
  }
});

test("backend error messages preserve status and server explanation", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ error: "设备离线" }), { status: 409 });
  try {
    await assert.rejects(
      api("/threads"),
      (error) =>
        error instanceof ApiError &&
        error.status === 409 &&
        error.message === "设备离线",
    );
  } finally {
    globalThis.fetch = original;
  }
});
