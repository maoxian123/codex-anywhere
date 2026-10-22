import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  act,
  cleanup,
  fireEvent,
  renderHook,
  waitFor,
} from "@testing-library/react";
import {
  ThreadActionIdentity,
  sendManualThreadRelease,
} from "./thread-presence";
import { useThreadOwnership } from "./useThreadOwnership";
import { setSession } from "./api";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});

function ownershipServer() {
  const requests: {
    path: string;
    init?: RequestInit;
    body: Record<string, unknown>;
  }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    requests.push({ path, init, body: JSON.parse(String(init?.body || "{}")) });
    const ownership = {
      state: "managed",
      owners: [],
      can_terminate: false,
      owner_token: null,
    };
    return new Response(
      JSON.stringify({
        command: {
          id: `action-${requests.length}`,
          status: "completed",
          result: path.endsWith("/ownership")
            ? ownership
            : { released: true, ownership },
        },
      }),
    );
  };
  return requests;
}

test("manual actions retain one client identity and strictly ordered timestamps even if the clock goes backwards", () => {
  let now = 1000000;
  const identity = new ThreadActionIdentity("tab-one", () => now);
  const first = identity.payload();
  const second = identity.payload();
  now -= 60000;
  const third = identity.payload();
  assert.equal(first.client_id, "tab-one");
  assert.equal(second.client_id, first.client_id);
  assert.ok(second.issued_at > first.issued_at);
  assert.ok(third.issued_at > second.issued_at);
});

test("idle time, input, scroll, visibility, restoration and switching only check ownership without browser leases", async (context) => {
  const requests = ownershipServer();
  const view = renderHook(
    ({ id, enabled }) => useThreadOwnership(id, enabled),
    {
      initialProps: { id: "first", enabled: true },
    },
  );
  await waitFor(() =>
    assert.equal(view.result.current.ownership?.state, "managed"),
  );
  context.mock.timers.enable({
    apis: ["setTimeout", "setInterval", "Date"],
    now: 1000000,
  });
  act(() => {
    context.mock.timers.tick(30 * 60 * 1000);
  });
  fireEvent.input(document);
  fireEvent.keyDown(document, { key: "ArrowDown" });
  fireEvent.pointerDown(document);
  fireEvent.pointerMove(document, { buttons: 1 });
  fireEvent.wheel(document);
  fireEvent.scroll(document);
  fireEvent.touchStart(document);
  fireEvent.touchMove(document);
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => {
    window.dispatchEvent(new Event("pagehide"));
    window.dispatchEvent(new Event("pageshow"));
  });
  view.rerender({ id: "second", enabled: true });
  await act(async () => {});
  view.rerender({ id: "second", enabled: false });
  view.unmount();
  assert.ok(
    requests.some((request) => request.path === "/api/threads/first/ownership"),
  );
  assert.ok(
    requests.some(
      (request) => request.path === "/api/threads/second/ownership",
    ),
  );
  assert.ok(requests.every((request) => request.path.endsWith("/ownership")));
});

test("manual release sends an authenticated explicit action without keepalive or browser presence", async () => {
  const requests = ownershipServer();
  setSession({ authenticated: true, csrf_token: "manual-csrf" });
  await sendManualThreadRelease("native/thread", {
    client_id: "tab-one",
    issued_at: 10,
  });
  assert.equal(requests[0].path, "/api/threads/native%2Fthread/release");
  assert.deepEqual(requests[0].body, {
    manual: true,
    client_id: "tab-one",
    issued_at: 10,
  });
  assert.equal(requests[0].init?.method, "POST");
  assert.equal(requests[0].init?.keepalive, undefined);
  assert.equal(requests[0].init?.credentials, "same-origin");
  assert.equal(
    new Headers(requests[0].init?.headers).get("X-CSRF-Token"),
    "manual-csrf",
  );
});
