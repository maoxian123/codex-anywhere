import test from "node:test";
import assert from "node:assert/strict";
import { latestHistory, olderHistory } from "./history";
import type { Item, ThreadDetail } from "./types";

const item = (id: string, position: number, text = id): Item => ({
  id,
  native_id: id,
  thread_id: "t1",
  type: "agentMessage",
  position,
  data: { text },
});
function page(items: Item[], version = "v1"): ThreadDetail {
  return {
    thread: {
      id: "t1",
      host_id: "h1",
      title: "test",
      cwd: "/work",
      status: "idle",
      created_at: "",
      updated_at: "",
      history_synced_at: version,
    },
    items,
    interactions: [],
    has_more_items: true,
    next_before_position: 100,
  };
}
test("latest pages update messages while keeping explicitly loaded old pages and their cursor", () => {
  const first = latestHistory(null, page([item("recent", 101)]));
  const expanded = olderHistory(first, {
    ...page([item("old", 1)]),
    has_more_items: false,
    next_before_position: null,
  })!;
  const next = latestHistory(
    expanded,
    page([item("recent", 101, "updated"), item("new", 102)]),
  );
  assert.deepEqual(
    next.items.map((value) => value.id),
    ["old", "recent", "new"],
  );
  assert.equal(next.items[1].data.text, "updated");
  assert.equal(next.hasMore, false);
  assert.equal(next.before, null);
});
test("a different imported snapshot discards old page caches and rejects late old responses", () => {
  const first = latestHistory(null, page([item("recent", 101)]));
  const next = latestHistory(first, page([item("replacement", 200)], "v2"));
  assert.deepEqual(
    next.items.map((value) => value.id),
    ["replacement"],
  );
  assert.equal(olderHistory(next, page([item("stale", 1)])), null);
});

test("native older-page projection advances sync time without discarding the same generation's pages", () => {
  const firstPage = page([item("recent", 101)]);
  firstPage.thread.history_generation = "generation-1";
  const cache = latestHistory(null, firstPage);
  const olderPage = page([item("old", 1)], "later-sync");
  olderPage.thread.history_generation = "generation-1";
  const expanded = olderHistory(cache, olderPage)!;
  assert.ok(expanded);
  const newest = page([item("recent", 101, "updated")], "latest-sync");
  newest.thread.history_generation = "generation-1";
  assert.deepEqual(
    latestHistory(expanded, newest).items.map((entry) => entry.id),
    ["old", "recent"],
  );
  newest.thread.history_generation = "generation-2";
  assert.deepEqual(
    latestHistory(expanded, newest).items.map((entry) => entry.id),
    ["recent"],
  );
});
