import "./test-dom";
import { useState } from "react";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useAutomaticHistory } from "./useAutomaticHistory";
import type { Host, Thread } from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});
const thread: Thread = {
  id: "t1",
  host_id: "h1",
  native_id: "native-1",
  title: "local",
  cwd: "/work",
  status: "idle",
  imported: true,
  history_loaded: false,
  native_updated_at: 1,
  created_at: "",
  updated_at: "",
};
const host: Host = {
  id: "h1",
  name: "fake-node",
  platform: "linux",
  online: true,
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
function Harness({
  current,
  node = host,
  refresh,
}: {
  current: Thread;
  node?: Host;
  refresh: (id?: string) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const busy = useAutomaticHistory({
    selectionId: current.id,
    thread: current,
    host: node,
    refresh,
    onError: setError,
  });
  return (
    <div>
      <span>{busy ? "同步中" : "同步已结束"}</span>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
test("opening discovered history loads once per version and polls only bounded metadata", async () => {
  let requests = 0,
    refreshed = 0;
  const refresh = async () => {
    refreshed += 1;
  };
  globalThis.fetch = async (input) => {
    const path = String(input);
    if (path.endsWith("/history")) {
      requests += 1;
      return response(
        {
          thread,
          command: {
            id: "import-1",
            kind: "thread.import",
            status: "completed",
            result: {},
          },
        },
        202,
      );
    }
    assert.equal(path, "/api/threads/t1?item_limit=1");
    return response({
      thread: { ...thread, history_loaded: true },
      items: [],
      interactions: [],
    });
  };
  const view = render(<Harness current={thread} refresh={refresh} />);
  await waitFor(() => assert.equal(refreshed, 1));
  view.rerender(<Harness current={{ ...thread }} refresh={refresh} />);
  assert.equal(requests, 1);
  view.rerender(
    <Harness current={{ ...thread, native_updated_at: 2 }} refresh={refresh} />,
  );
  await waitFor(() => assert.equal(refreshed, 2));
  assert.equal(requests, 2);
});
test("409 unknown imports stop automatic retries even after unchanged catalog scans and route switches", async () => {
  let requests = 0;
  const refresh = async () => {};
  globalThis.fetch = async () => {
    requests += 1;
    return response(
      { error: "Import outcome unknown; choose manual retry" },
      409,
    );
  };
  const view = render(<Harness current={thread} refresh={refresh} />);
  await screen.findByRole("alert");
  view.rerender(
    <Harness
      current={thread}
      node={{ ...host, last_sync_at: "2026-10-21T01:00:00Z" }}
      refresh={refresh}
    />,
  );
  view.rerender(
    <Harness
      current={{ ...thread, id: "t2", status: "active" }}
      refresh={refresh}
    />,
  );
  view.rerender(
    <Harness
      current={thread}
      node={{ ...host, last_sync_at: "2026-10-21T02:00:00Z" }}
      refresh={refresh}
    />,
  );
  assert.equal(requests, 1);
});
test("switching away cancels old automatic loading without polling or refreshing the old task", async () => {
  let finishOld!: (response: Response) => void;
  const old = new Promise<Response>((resolve) => {
    finishOld = resolve;
  });
  const refreshed: string[] = [];
  const refresh = async (id?: string) => {
    refreshed.push(id || "");
  };
  globalThis.fetch = async (input) =>
    String(input) === "/api/threads/t1/history"
      ? old
      : response({ thread: { ...thread, id: "t2" }, command: null });
  const view = render(<Harness current={thread} refresh={refresh} />);
  view.rerender(
    <Harness current={{ ...thread, id: "t2" }} refresh={refresh} />,
  );
  await waitFor(() => assert.deepEqual(refreshed, ["t2"]));
  await act(async () => {
    finishOld(
      response({ thread, command: { id: "old", status: "completed" } }, 202),
    );
    await old;
  });
  assert.deepEqual(refreshed, ["t2"]);
  assert.equal(screen.queryByRole("alert") === null, true);
});
test("live, waiting and importing threads never trigger a second history request", async () => {
  let requests = 0;
  const refresh = async () => {};
  globalThis.fetch = async () => {
    requests += 1;
    return response({ thread, command: null });
  };
  const view = render(
    <Harness current={{ ...thread, status: "active" }} refresh={refresh} />,
  );
  for (const status of ["waiting", "starting", "importing"])
    view.rerender(
      <Harness current={{ ...thread, status }} refresh={refresh} />,
    );
  assert.equal(requests, 0);
});
