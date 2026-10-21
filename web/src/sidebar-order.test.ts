import test from "node:test";
import assert from "node:assert/strict";
import {
  groupThreadsByDate,
  mergeSidebarProjects,
  mergeSidebarThreads,
  promoteSidebarThread,
  threadActivityTime,
} from "./sidebar-activity.ts";
import type { Project, Thread } from "./types.ts";

function thread(
  id: string,
  activity: string,
  patch: Partial<Thread> = {},
): Thread {
  return {
    id,
    host_id: "host",
    title: id,
    cwd: "/project",
    status: "idle",
    created_at: "2026-10-01T12:00:00Z",
    updated_at: activity,
    last_activity_at: activity,
    ...patch,
  };
}

function project(id: string, patch: Partial<Project> = {}): Project {
  return {
    id,
    host_id: "host",
    name: id,
    cwd: `/${id}`,
    thread_count: 1,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-01T12:00:00Z",
    ...patch,
  };
}

const older = "2026-10-19T12:00:00Z";
const recent = "2026-10-20T12:00:00Z";
const latest = "2026-10-21T12:00:00Z";
const ids = (threads: Thread[]) => threads.map((value) => value.id);

test("a new sidebar scope ranks once by activity and removes overlapping IDs", () => {
  const incoming = [
    thread("old", older),
    thread("new", latest),
    thread("middle", recent),
    thread("old", older),
  ];
  const before = structuredClone(incoming);
  assert.deepEqual(ids(mergeSidebarThreads([], incoming)), [
    "new",
    "middle",
    "old",
  ]);
  assert.deepEqual(
    ids(
      mergeSidebarThreads([thread("previous-scope", latest)], incoming, true),
    ),
    ["new", "middle", "old"],
  );
  assert.deepEqual(incoming, before);
});

test("background rankings update metadata without moving existing sidebar rows or dates", () => {
  const current = [thread("first", recent), thread("second", older)];
  const incoming = [
    thread("second", latest, { title: "Updated title", status: "active" }),
    thread("first", older),
  ];
  const currentBefore = structuredClone(current);
  const incomingBefore = structuredClone(incoming);
  const merged = mergeSidebarThreads(current, incoming);
  assert.deepEqual(ids(merged), ["first", "second"]);
  assert.equal(merged[1].title, "Updated title");
  assert.equal(merged[1].status, "active");
  assert.equal(merged[1].last_activity_at, new Date(older).toISOString());
  assert.equal(threadActivityTime(merged[0]), threadActivityTime(current[0]));
  assert.equal(threadActivityTime(merged[1]), threadActivityTime(current[1]));
  assert.deepEqual(current, currentBefore);
  assert.deepEqual(incoming, incomingBefore);
});

test("an old snapshot cannot undo an explicit local promotion or activity timestamp", () => {
  const current = [
    thread("server-first", recent),
    thread("locally-used", older),
  ];
  const promoted = promoteSidebarThread(current, current[1], new Date(latest));
  const snapshot = mergeSidebarThreads(promoted, current);
  assert.deepEqual(ids(snapshot), ["locally-used", "server-first"]);
  assert.equal(snapshot[0].last_activity_at, new Date(latest).toISOString());
  assert.deepEqual(ids(current), ["server-first", "locally-used"]);
  assert.equal(current[1].last_activity_at, older);
});

test("page overlap retains loaded rows and local promotions while appending unseen IDs", () => {
  const current = [
    thread("local", latest),
    thread("first-page", recent),
    thread("overlap", older),
  ];
  const incoming = [
    thread("overlap", latest, { title: "Refreshed overlap" }),
    thread("next", recent),
    thread("last", older),
    thread("next", recent),
  ];
  const merged = mergeSidebarThreads(current, incoming);
  assert.deepEqual(ids(merged), [
    "local",
    "first-page",
    "overlap",
    "next",
    "last",
  ]);
  assert.equal(merged[2].title, "Refreshed overlap");
  assert.equal(merged[2].last_activity_at, new Date(older).toISOString());
  assert.deepEqual(ids(mergeSidebarThreads(merged, [])), ids(merged));
});

test("native activity fallback is frozen when background metadata changes", () => {
  const current = thread("native", recent, {
    imported: true,
    native_updated_at: Date.parse(older) / 1000,
    last_activity_at: undefined,
  });
  const incoming = { ...current, native_updated_at: Date.parse(latest) / 1000 };
  const merged = mergeSidebarThreads([current], [incoming]);
  assert.equal(merged[0].native_updated_at, incoming.native_updated_at);
  assert.equal(merged[0].last_activity_at, new Date(older).toISOString());
  assert.equal(threadActivityTime(merged[0]), Date.parse(older));
});

test("merging and promoting preserve newer desired and effective settings", () => {
  const current = thread("settings", recent, {
    execution_settings: { model: "current" },
    settings_revision: 8,
    effective_settings: { model: "applied" },
    effective_settings_revision: 6,
  });
  const stale = thread("settings", older, {
    execution_settings: { model: "old" },
    settings_revision: 2,
    effective_settings: { model: "old-applied" },
    effective_settings_revision: 1,
  });
  for (const value of [
    mergeSidebarThreads([current], [stale])[0],
    promoteSidebarThread([current], stale, new Date(latest))[0],
  ]) {
    assert.equal(value.settings_revision, 8);
    assert.deepEqual(value.execution_settings, { model: "current" });
    assert.equal(value.effective_settings_revision, 6);
    assert.deepEqual(value.effective_settings, { model: "applied" });
  }
});

test("promotion removes target duplicates, retains other order, and also inserts a new thread", () => {
  const first = thread("first", older);
  const target = thread("target", recent);
  const last = thread("last", older);
  const promoted = promoteSidebarThread(
    [first, target, last, target],
    { ...target, title: "Local title" },
    new Date(latest),
  );
  assert.deepEqual(ids(promoted), ["target", "first", "last"]);
  assert.equal(promoted[0].title, "Local title");
  assert.equal(promoted[1], first);
  assert.equal(promoted[2], last);
  assert.deepEqual(
    ids(promoteSidebarThread([first, last], target, new Date(latest))),
    ["target", "first", "last"],
  );
});

test("preserved date grouping only combines adjacent buckets and keeps repeated headings", () => {
  const now = new Date(2026, 9, 21, 12);
  const day = (id: string, offset: number) =>
    thread(id, new Date(2026, 9, 21 - offset, 12).toISOString());
  const input = [
    day("today-first", 0),
    day("yesterday", 1),
    day("today-again", 0),
    day("week", 3),
    day("week-adjacent", 4),
    day("old", 30),
  ];
  const sections = groupThreadsByDate(input, now, true);
  assert.deepEqual(
    sections.map((section) => section.label),
    ["今天", "昨天", "今天", "过去7天", "更早"],
  );
  assert.deepEqual(
    sections.map((section) => section.threads[0].id),
    ["today-first", "yesterday", "today-again", "week", "old"],
  );
  assert.deepEqual(
    sections.flatMap((section) => ids(section.threads)),
    ids(input),
  );
  assert.deepEqual(
    groupThreadsByDate(input, now).map((section) => section.label),
    ["今天", "昨天", "过去7天", "更早"],
  );
});

test("project snapshots keep server initial ranking then retain rank and remove missing IDs", () => {
  const current = [project("first"), project("second"), project("deleted")];
  const incoming = [
    project("second", { name: "Renamed", thread_count: 8 }),
    project("new"),
    project("first"),
  ];
  assert.deepEqual(
    mergeSidebarProjects([], incoming).map((value) => value.id),
    ["second", "new", "first"],
  );
  const before = structuredClone(current);
  const merged = mergeSidebarProjects(current, incoming);
  assert.deepEqual(
    merged.map((value) => value.id),
    ["first", "second", "new"],
  );
  assert.equal(merged[1].name, "Renamed");
  assert.equal(merged[1].thread_count, 8);
  assert.deepEqual(mergeSidebarProjects(current, []), []);
  assert.deepEqual(current, before);
});
