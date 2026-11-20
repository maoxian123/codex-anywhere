import "./test-dom";
import { useState } from "react";
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
  const { busy, retry } = useAutomaticHistory({
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
      <button onClick={retry} disabled={busy}>
        重试历史读取
      </button>
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

test("explicit history retry bypasses the blocked version and sends retry:true", async () => {
  const bodies: unknown[] = [];
  let refreshed = 0;
  const refresh = async () => {
    refreshed += 1;
  };
  globalThis.fetch = async (_input, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    return bodies.length === 1
      ? response({ error: "previous history failed" }, 409)
      : response({ thread, command: null });
  };
  const view = render(<Harness current={thread} refresh={refresh} />);
  await screen.findByRole("alert");
  view.rerender(
    <Harness current={{ ...thread, status: "error" }} refresh={refresh} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "重试历史读取" }));
  await waitFor(() => assert.equal(refreshed, 1));
  assert.deepEqual(bodies, [{}, { retry: true }]);
  assert.equal(screen.queryByRole("alert"), null);
});

test("refreshing cached native history waits for the new snapshot projection", async () => {
  const cached: Thread = {
    ...thread,
    history_loaded: true,
    history_stale: false,
    history_generation: "old-generation",
  };
  const bodies: unknown[] = [];
  let metadataReads = 0,
    refreshed = 0;
  let finishProjection!: (value: Response) => void;
  const projected = new Promise<Response>((resolve) => {
    finishProjection = resolve;
  });
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path === "/api/threads/t1/history") {
      bodies.push(JSON.parse(String(init?.body)));
      return response({
        thread: cached,
        command: {
          id: "latest-page",
          kind: "thread.history",
          status: "completed",
          result: { itemCount: 1 },
        },
      });
    }
    assert.equal(path, "/api/threads/t1?item_limit=1");
    metadataReads += 1;
    return metadataReads === 1
      ? response({ thread: cached, items: [], interactions: [] })
      : projected;
  };
  const refresh = async () => {
    refreshed += 1;
  };
  render(<Harness current={cached} refresh={refresh} />);
  assert.equal(bodies.length, 0);
  fireEvent.click(screen.getByRole("button", { name: "重试历史读取" }));
  await waitFor(() => assert.equal(metadataReads, 2));
  assert.deepEqual(bodies, [{ retry: true }]);
  assert.equal(refreshed, 0);
  assert.ok(screen.getByText("同步中"));
  assert.equal(
    (screen.getByRole("button", { name: "重试历史读取" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  await act(async () => {
    finishProjection(
      response({
        thread: { ...cached, history_generation: "new-generation" },
        items: [],
        interactions: [],
      }),
    );
    await projected;
  });
  await waitFor(() => assert.equal(refreshed, 1));
  assert.ok(screen.getByText("同步已结束"));
  assert.equal(metadataReads, 2);
});

test("a reused older-page command offers another explicit refresh instead of waiting for a new generation", async () => {
  const cached = {
    ...thread,
    history_loaded: true,
    history_generation: "old-generation",
  };
  const bodies: unknown[] = [];
  let refreshed = 0;
  globalThis.fetch = async (input, init) => {
    if (String(input).endsWith("/history")) {
      bodies.push(JSON.parse(String(init?.body)));
      return response({
        thread: cached,
        command: {
          id: `page-${bodies.length}`,
          kind: "thread.history",
          status: "completed",
          result: {
            generation:
              bodies.length === 1 ? "old-generation" : "new-generation",
          },
        },
      });
    }
    assert.equal(String(input), "/api/threads/t1?item_limit=1");
    return response({
      thread: { ...cached, history_generation: "new-generation" },
      items: [],
      interactions: [],
    });
  };
  render(
    <Harness
      current={cached}
      refresh={async () => {
        refreshed += 1;
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "重试历史读取" }));
  await screen.findByText("更早历史正在由其他页面同步，请稍后重试历史读取。");
  assert.deepEqual(bodies, [{ retry: true }]);
  assert.equal(refreshed, 0);
  fireEvent.click(screen.getByRole("button", { name: "重试历史读取" }));
  await waitFor(() => assert.equal(refreshed, 1));
  assert.deepEqual(bodies, [{ retry: true }, { retry: true }]);
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
