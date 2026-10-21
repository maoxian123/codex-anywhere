import type { Item, Thread, ThreadDetail } from "./types";

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
    items: mergeItems(page.items, cache.items),
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
