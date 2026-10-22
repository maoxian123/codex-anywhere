import type { Item, StreamItem } from "./types";
import { list, object, string } from "./utils";

export type TimelineEntry = {
  item: Item;
  stream?: StreamItem;
  pending?: boolean;
};
export type WorkGroup = {
  kind: "work";
  key: string;
  turn: string;
  entries: TimelineEntry[];
};
export type TimelineUnit =
  WorkGroup | { kind: "item"; key: string; entry: TimelineEntry };

export function nativeType(item: Item) {
  return (item.type || string(item.data.type))
    .replaceAll("_", "")
    .toLowerCase();
}

export function entryAliases(entry: TimelineEntry) {
  return [entry.item.id, entry.item.native_id, entry.stream?.id].filter(
    (id): id is string => Boolean(id),
  );
}

function isStandalone(item: Item) {
  return (
    ["usermessage", "agentmessage", "imageview", "imagegeneration"].includes(
      nativeType(item),
    ) ||
    list(item.data._assets).some((asset) =>
      string(object(asset).mime).startsWith("image/"),
    )
  );
}

/** Keep native order; a message, image artifact, or turn boundary ends a group. */
export function timelineUnits(
  items: Item[],
  streams: Record<string, StreamItem>,
  previous: TimelineUnit[] = [],
): TimelineUnit[] {
  const known = new Set(items.flatMap((item) => [item.id, item.native_id]));
  const uniqueStreams = new Map(
    Object.values(streams).map((stream) => [stream.id, stream]),
  );
  const entries: TimelineEntry[] = items.map((item) => ({
    item,
    stream:
      streams[item.native_id] ||
      streams[item.id] ||
      uniqueStreams.get(item.native_id) ||
      uniqueStreams.get(item.id),
  }));
  for (const [key, stream] of Object.entries(streams)) {
    if (known.has(key)) known.add(stream.id);
  }
  for (const stream of uniqueStreams.values()) {
    if (known.has(stream.id)) continue;
    entries.push({
      item: {
        id: stream.id,
        native_id: stream.id,
        thread_id: items.at(-1)?.thread_id || "",
        turn_id: items.at(-1)?.turn_id,
        type:
          stream.kind === "message"
            ? "agentMessage"
            : stream.kind === "command"
              ? "commandExecution"
              : "reasoning",
        data: { status: "inProgress", _pending: true },
      },
      stream,
      pending: true,
    });
  }
  const units: TimelineUnit[] = [];
  for (const entry of entries) {
    const { item } = entry;
    if (isStandalone(item)) {
      units.push({ kind: "item", key: item.native_id || item.id, entry });
      continue;
    }
    const last = units.at(-1);
    const turn = item.turn_id || "";
    if (last?.kind === "work" && last.turn === turn) {
      last.entries.push(entry);
    } else {
      units.push({
        kind: "work",
        key: `work:${item.thread_id}:${turn}:${item.native_id || item.id}`,
        turn,
        entries: [entry],
      });
    }
  }
  // History can prepend the first half of a tool block, and a live item gains a
  // persisted hub ID when it completes. Preserve the existing group's React key
  // through either change by matching native/persisted identities, not positions.
  const priorKeys = new Map<string, string>();
  for (const unit of previous) {
    if (unit.kind !== "work") continue;
    for (const entry of unit.entries) {
      for (const alias of entryAliases(entry)) priorKeys.set(alias, unit.key);
    }
  }
  const usedKeys = new Set<string>();
  for (const unit of units) {
    if (unit.kind !== "work") continue;
    for (const alias of unit.entries.flatMap(entryAliases)) {
      const priorKey = priorKeys.get(alias);
      if (priorKey && !usedKeys.has(priorKey)) {
        unit.key = priorKey;
        break;
      }
    }
    // A newly inserted group can share an old first-item-derived key with a
    // preserved group after a split. Keep both blocks distinct in that case.
    while (usedKeys.has(unit.key)) unit.key += ":split";
    usedKeys.add(unit.key);
  }
  return units;
}

export function entryRunning(entry: TimelineEntry) {
  return (
    Boolean(entry.stream) ||
    ["inprogress", "in_progress", "running", "started"].includes(
      string(entry.item.data.status).toLowerCase(),
    )
  );
}

export function entryFailed(entry: TimelineEntry) {
  const data = entry.item.data;
  return (
    Boolean(data.error) ||
    data.isError === true ||
    object(data.result).isError === true ||
    (typeof data.exitCode === "number" && data.exitCode !== 0) ||
    (typeof data.exit_code === "number" && data.exit_code !== 0) ||
    ["failed", "error", "errored"].includes(string(data.status).toLowerCase())
  );
}

export function workSummary(entries: TimelineEntry[]) {
  let thoughts = 0;
  let tools = 0;
  let plans = 0;
  let compactions = 0;
  let other = 0;
  let failures = 0;
  for (const entry of entries) {
    const type = nativeType(entry.item);
    if (type === "reasoning") thoughts += 1;
    else if (type === "plan") plans += 1;
    else if (type === "contextcompaction") compactions += 1;
    else if (
      [
        "commandexecution",
        "filechange",
        "websearch",
        "mcptoolcall",
        "dynamictoolcall",
      ].includes(type)
    )
      tools += 1;
    else other += 1;
    if (entryFailed(entry)) failures += 1;
  }
  return {
    counts: [
      thoughts ? `${thoughts} 次思考` : "",
      tools ? `${tools} 次工具调用` : "",
      plans ? `${plans} 项计划` : "",
      compactions ? `${compactions} 次上下文整理` : "",
      other ? `${other} 项记录` : "",
    ]
      .filter(Boolean)
      .join(" · "),
    failures,
  };
}

export function activeWorkLabel(entry?: TimelineEntry) {
  if (!entry) return "";
  const type = nativeType(entry.item);
  if (type === "reasoning") return "思考中";
  if (type === "commandexecution") return "执行命令";
  if (type === "filechange") return "修改文件";
  if (type === "websearch") return "搜索网页";
  if (type === "plan") return "更新计划";
  return string(entry.item.data.tool, "调用工具").slice(0, 40);
}
