import test from "node:test";
import assert from "node:assert/strict";
import {
  CONVERSATION_IDLE,
  PRESENCE_INTERVAL,
  ThreadPresence,
  sendThreadLease,
  type LeasePayload,
} from "./thread-presence";
import { setSession } from "./api";

test("presence renews only from recent user activity and releases once after five idle minutes", async () => {
  let now = 1_000_000;
  const calls: { action: string; payload: LeasePayload; keepalive: boolean }[] =
    [];
  const presence = new ThreadPresence(
    "tab-one",
    async (action, payload, keepalive) => {
      calls.push({ action, payload, keepalive });
    },
    () => now,
  );
  await presence.ensure();
  now += PRESENCE_INTERVAL;
  presence.heartbeat();
  assert.deepEqual(
    calls.map((call) => call.action),
    ["presence", "presence"],
  );
  now += CONVERSATION_IDLE;
  presence.heartbeat();
  presence.heartbeat();
  assert.deepEqual(
    calls.map((call) => call.action),
    ["presence", "presence", "release"],
  );
  assert.equal(presence.nextCheck(), PRESENCE_INTERVAL);
  presence.activity();
  assert.equal(calls.at(-1)?.action, "presence");
  assert.ok(calls.every((call) => call.payload.client_id === "tab-one"));
  assert.ok(
    calls.every(
      (call, index) =>
        index === 0 ||
        call.payload.issued_at > calls[index - 1].payload.issued_at,
    ),
  );
});

test("pagehide releases with keepalive and prevents background renewal until restored", async () => {
  let now = 1000;
  const actions: string[] = [];
  const presence = new ThreadPresence(
    "tab-two",
    async (action, _payload, keepalive) => {
      actions.push(`${action}:${keepalive}`);
    },
    () => now,
  );
  await presence.ensure();
  await presence.pagehide();
  now += PRESENCE_INTERVAL;
  presence.heartbeat();
  presence.activity();
  assert.deepEqual(actions, ["presence:false", "release:true"]);
  await presence.pageshow();
  assert.equal(actions.at(-1), "presence:false");
});

test("manual release stays paused through browsing and restoration until an explicit send or takeover", async () => {
  let now = 1000;
  const actions: string[] = [];
  const presence = new ThreadPresence(
    "tab-manual",
    async (action) => {
      actions.push(action);
    },
    () => now,
  );
  await presence.ensure();
  await presence.release(true, true);
  presence.activity();
  now += PRESENCE_INTERVAL;
  presence.heartbeat();
  await presence.pagehide();
  await presence.pageshow();
  presence.activity();
  assert.deepEqual(actions, ["presence", "release", "release"]);
  await presence.ensure();
  assert.equal(actions.at(-1), "presence");
});

test("release uses authenticated POST with CSRF and keepalive", async () => {
  const original = globalThis.fetch;
  const requests: { path: string; init?: RequestInit }[] = [];
  setSession({ authenticated: true, csrf_token: "lease-csrf" });
  globalThis.fetch = async (input, init) => {
    requests.push({ path: String(input), init });
    return new Response(
      JSON.stringify({ command: { id: "release", status: "queued" } }),
    );
  };
  try {
    await sendThreadLease(
      "native/thread",
      "release",
      { client_id: "tab-one", issued_at: 10 },
      true,
    );
    assert.equal(requests[0].path, "/api/threads/native%2Fthread/release");
    assert.equal(requests[0].init?.method, "POST");
    assert.equal(requests[0].init?.keepalive, true);
    assert.equal(requests[0].init?.credentials, "same-origin");
    assert.equal(
      new Headers(requests[0].init?.headers).get("X-CSRF-Token"),
      "lease-csrf",
    );
  } finally {
    globalThis.fetch = original;
  }
});
