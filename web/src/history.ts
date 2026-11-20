import type { Item, Thread, ThreadDetail } from "./types";

export function historyNeedsRefresh(message?: string | null): boolean {
  return Boolean(
    message &&
    [
      "History changed while loading this page",
      "History changed since this cursor was created",
      "Native history page anchor is no longer cached",
      "历史已更新，请刷新最近消息后继续加载",
    ].some((text) => message.includes(text)),
  );
}

export function historyErrorText(message: string): string {
  return historyNeedsRefresh(message)
    ? "历史记录在读取期间已更新，请刷新最新历史；若会话仍在运行，请等待结束后再试。"
    : message;
}

export type HistoryCache = {
  id: string;
  version: string;
  items: Item[];
  expanded: boolean;
  hasMore: boolean;
  before: number | null;
};
export function snapshotVersion(thread: Thread) {
  if (thread.history_generation) return thread.history_generation;
  return JSON.stringify([
    thread.history_synced_at ?? null,
    thread.history_native_updated_at ?? null,
  ]);
}
function mergeItems(older: Item[], newer: Item[]) {
  return [
    ...new Map(
      [...older, ...newer].map((item) => [item.native_id || item.id, item]),
    ).values(),
  ].sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
}
export function latestHistory(
  cache: HistoryCache | null,
  page: ThreadDetail,
): HistoryCache {
  const version = snapshotVersion(page.thread);
  const same = cache?.id === page.thread.id && cache.version === version;
  return {
    id: page.thread.id,
    version,
    items: same ? mergeItems(cache.items, page.items) : page.items,
    expanded: same && cache.expanded,
    hasMore:
      same && cache.expanded ? cache.hasMore : Boolean(page.has_more_items),
    before:
      same && cache.expanded
        ? cache.before
        : (page.next_before_position ?? null),
  };
}
export function olderHistory(
  cache: HistoryCache,
  page: ThreadDetail,
): HistoryCache | null {
  if (
    cache.id !== page.thread.id ||
    cache.version !== snapshotVersion(page.thread)
  )
    return null;
  return {
    ...cache,
    items: mergeItems(cache.items, page.items),
    expanded: true,
    hasMore: Boolean(page.has_more_items),
    before: page.next_before_position ?? null,
  };
}
export function withHistory(
  page: ThreadDetail,
  cache: HistoryCache,
): ThreadDetail {
  return {
    ...page,
    items: cache.items,
    has_more_items: cache.hasMore,
    next_before_position: cache.before,
  };
}
