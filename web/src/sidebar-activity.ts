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

export function groupThreadsByDate(threads: Thread[], now = new Date()) {
  const groups = [
    { label: "今天", threads: [] as Thread[] },
    { label: "昨天", threads: [] as Thread[] },
    { label: "过去7天", threads: [] as Thread[] },
    { label: "更早", threads: [] as Thread[] },
  ];
  for (const thread of [...threads].sort(
    (left, right) => threadActivityTime(right) - threadActivityTime(left),
  )) {
    const days = threadAgeDays(thread, now);
    const index = days <= 0 ? 0 : days === 1 ? 1 : days <= 7 ? 2 : 3;
    groups[index].threads.push(thread);
  }
  return groups.filter((group) => group.threads.length > 0);
}
