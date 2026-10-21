import type { Asset, JsonObject } from "./types";

export function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}
export function string(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}
export function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
export function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "";
}
export function fileName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || path;
}
export function safeLink(value: string | undefined): string | undefined {
  if (!value) return undefined;
  if (/^\/tasks\/[a-zA-Z0-9_-]+(?:\?[^\s]*)?$/.test(value)) return value;
  try {
    const url = new URL(value);
    if (["https:", "http:", "mailto:"].includes(url.protocol)) return url.href;
  } catch {
    /* Reject relative paths, file paths, and executable protocols. */
  }
  return undefined;
}
export function imageSource(
  value: string | undefined,
  assets: Asset[] = [],
): string | undefined {
  if (!value) return undefined;
  if (/^\/api\/assets\/[a-zA-Z0-9_-]+$/.test(value)) return value;
  try {
    const url = new URL(value);
    if (url.protocol === "https:") return url.href;
  } catch {
    /* Match a bridged local asset below. */
  }
  let decoded = value;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    /* Keep the original. */
  }
  const normalize = (path: string) => {
    const normalized = path
      .replace(/^file:\/\/(?:localhost)?/, "")
      .replace(/\\/g, "/")
      .replace(/^\/([a-zA-Z]:\/)/, "$1");
    return /^[a-zA-Z]:\//.test(normalized)
      ? normalized.toLowerCase()
      : normalized;
  };
  const exact = assets.find((asset) =>
    [asset.source_path, asset.sourcePath].some(
      (path) => path && normalize(path) === normalize(decoded),
    ),
  );
  const byName = assets.filter((asset) =>
    [asset.source_path, asset.sourcePath, asset.name].some(
      (path) => path && fileName(path) === fileName(decoded),
    ),
  );
  const match = exact || (byName.length === 1 ? byName[0] : undefined);
  return match && /^\/api\/assets\/[a-zA-Z0-9_-]+$/.test(match.url)
    ? match.url
    : undefined;
}
export function timeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
export function relativeDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const days = Math.floor((Date.now() - date.getTime()) / 86400000);
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
export function busyStatus(status?: string) {
  return status === "active" || status === "waiting" || status === "starting";
}
export const statusLabels: Record<string, string> = {
  importing: "导入历史中",
  idle: "就绪",
  active: "运行中",
  waiting: "等待处理",
  error: "执行失败",
  interrupted: "已中断",
  starting: "启动中",
};
