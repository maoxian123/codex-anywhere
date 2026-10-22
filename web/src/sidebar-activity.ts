import { preserveNewerSettings } from "./execution-settings";
import type { Project, Thread } from "./types";

function dateTime(value: string) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : 0;
}

export function threadActivityTime(thread: Thread) {
  const projected =
    thread.last_activity_at && dateTime(thread.last_activity_at);
  if (projected) return projected;
  if (
    thread.imported &&
    typeof thread.native_updated_at === "number" &&
    Number.isFinite(thread.native_updated_at) &&
    thread.native_updated_at > 0
  ) {
    return thread.native_updated_at * 1000;
  }
  return dateTime(thread.updated_at) || dateTime(thread.created_at);
}

export function sortProjectsByActivity(projects: Project[], threads: Thread[]) {
  const activity = new Map(
    projects.map((project) => [
      project.id,
      dateTime(project.last_activity_at || project.updated_at) ||
        dateTime(project.created_at),
    ]),
  );
  let hasNewActivity = false;
  for (const thread of threads) {
    if (!thread.project_id || !activity.has(thread.project_id)) continue;
    const time = threadActivityTime(thread);
    if (time > activity.get(thread.project_id)!) {
      activity.set(thread.project_id, time);
      hasNewActivity = true;
    }
  }
  // The server considers every thread, including those outside the loaded page.
  if (!hasNewActivity) return [...projects];
  return [...projects].sort(
    (left, right) => activity.get(right.id)! - activity.get(left.id)!,
  );
}

/** Rank a new scope once; subsequent snapshots only refresh row metadata. */
export function mergeSidebarThreads(
  current: Thread[],
  incoming: Thread[],
  reset = false,
): Thread[] {
  if (reset || current.length === 0) {
    const seen = new Set<string>();
    return [...incoming]
      .sort(
        (left, right) => threadActivityTime(right) - threadActivityTime(left),
      )
      .filter((thread) => {
        if (seen.has(thread.id)) return false;
        seen.add(thread.id);
        return true;
      });
  }
  const updates = new Map(incoming.map((thread) => [thread.id, thread]));
  const seen = new Set<string>();
  const result: Thread[] = [];
  for (const thread of current) {
    if (seen.has(thread.id)) continue;
    seen.add(thread.id);
    const update = updates.get(thread.id);
    result.push(
      update
        ? preserveNewerSettings(
            {
              ...thread,
              ...update,
              last_activity_at: new Date(
                threadActivityTime(thread),
              ).toISOString(),
            },
            thread,
          )
        : thread,
    );
  }
  for (const thread of incoming) {
    if (seen.has(thread.id)) continue;
    seen.add(thread.id);
    result.push(updates.get(thread.id)!);
  }
  return result;
}

/** Explicit local activity is the only action that promotes an existing row. */
export function promoteSidebarThread(
  current: Thread[],
  thread: Thread,
  now = new Date(),
): Thread[] {
  const previous = current.find((value) => value.id === thread.id);
  const promoted = preserveNewerSettings(
    { ...previous, ...thread, last_activity_at: now.toISOString() },
    previous,
  );
  return [promoted, ...current.filter((value) => value.id !== thread.id)];
}

/** Rank projects with conversations first, then retain rank across snapshots. */
export function mergeSidebarProjects(
  current: Project[],
  incoming: Project[],
): Project[] {
  // Inventory-only directories use their hub creation time as activity. An old
  // directory discovered today must not bury projects with recent conversations.
  // Apply this partition only on first load so background sync does not move
  // rows, and an explicitly added empty project can still stay at the top.
  if (current.length === 0) {
    incoming = [
      ...incoming.filter((project) => project.thread_count > 0),
      ...incoming.filter((project) => !(project.thread_count > 0)),
    ];
  }
  const updates = new Map(incoming.map((project) => [project.id, project]));
  const seen = new Set<string>();
  const result: Project[] = [];
  for (const project of current) {
    const update = updates.get(project.id);
    if (!update || seen.has(project.id)) continue;
    seen.add(project.id);
    result.push({ ...project, ...update });
  }
  for (const project of incoming) {
    if (seen.has(project.id)) continue;
    seen.add(project.id);
    result.push(updates.get(project.id)!);
  }
  return result;
}

function calendarDay(time: number) {
  const date = new Date(time);
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
}

function threadAgeDays(thread: Thread, now: Date) {
  return (
    (calendarDay(now.getTime()) - calendarDay(threadActivityTime(thread))) /
    86400000
  );
}

export function threadActivityLabel(thread: Thread, now = new Date()) {
  const days = threadAgeDays(thread, now);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  return new Date(threadActivityTime(thread)).toLocaleDateString("zh-CN", {
    month: "numeric",
    day: "numeric",
  });
}

export function groupThreadsByDate(
  threads: Thread[],
  now = new Date(),
  preserveOrder = false,
) {
  const groups = [
    { label: "今天", threads: [] as Thread[] },
    { label: "昨天", threads: [] as Thread[] },
    { label: "过去7天", threads: [] as Thread[] },
    { label: "更早", threads: [] as Thread[] },
  ];
  const ordered = preserveOrder
    ? threads
    : [...threads].sort(
        (left, right) => threadActivityTime(right) - threadActivityTime(left),
      );
  const sections: { label: string; threads: Thread[] }[] = [];
  for (const thread of ordered) {
    const days = threadAgeDays(thread, now);
    const index = days <= 0 ? 0 : days === 1 ? 1 : days <= 7 ? 2 : 3;
    if (preserveOrder) {
      const label = groups[index].label;
      const last = sections[sections.length - 1];
      if (last?.label === label) last.threads.push(thread);
      else sections.push({ label, threads: [thread] });
    } else groups[index].threads.push(thread);
  }
  if (preserveOrder) return sections;
  return groups.filter((group) => group.threads.length > 0);
}
