import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowDown,
  AlertCircle,
  Bot,
  Check,
  CheckCheck,
  ChevronRight,
  FileDiff,
  Globe,
  ImageIcon,
  ListChecks,
  LoaderCircle,
  MessageSquare,
  Terminal,
  Wrench,
} from "lucide-react";
import {
  AssetGallery,
  CopyButton,
  Disclosure,
  Markdown,
  References,
  Spinner,
} from "./components";
import type { Asset, Item, Reference, StreamItem } from "./types";
import { json, list, object, safeLink, string, timeLabel } from "./utils";
import { galleryAssets } from "./markdown-assets";
import { api, errorText } from "./api";
import {
  activeWorkLabel,
  entryRunning,
  timelineUnits,
  workSummary,
  type TimelineUnit,
  type WorkGroup,
} from "./timeline-work";

export type TimelineProps = {
  items: Item[];
  streams: Record<string, StreamItem>;
  busy: boolean;
  targetItem?: string;
  loading: boolean;
  importing?: boolean;
  hasMore?: boolean;
  loadingOlder?: boolean;
  olderError?: string;
  onLoadOlder?: () => void;
  onImage: (src: string, alt: string) => void;
  onNavigate: (id: string, item?: string) => void;
};

export function messageText(item: Item): string {
  const data = item.data;
  if (typeof data._displayText === "string") return data._displayText;
  if (typeof data.text === "string") return data.text;
  return list(data.content)
    .map((part) => string(object(part).text))
    .filter(Boolean)
    .join("\n\n");
}
function findLinks(
  data: unknown,
  seen = new Set<string>(),
  depth = 0,
): { url: string; title: string }[] {
  if (depth > 6 || seen.size > 25) return [];
  if (Array.isArray(data))
    return data.flatMap((value) => findLinks(value, seen, depth + 1));
  const value = object(data);
  const url = safeLink(string(value.url || value.link));
  const own =
    url && /^https?:/.test(url) && !seen.has(url)
      ? [{ url, title: string(value.title || value.name, url) }]
      : [];
  if (url) seen.add(url);
  return [
    ...own,
    ...Object.values(value).flatMap((child) =>
      child && typeof child === "object"
        ? findLinks(child, seen, depth + 1)
        : [],
    ),
  ];
}
function boundedJson(value: unknown) {
  const text = json(value);
  return text.length > 100000
    ? `${text.slice(0, 100000)}\n… 内容过长，已截断显示`
    : text;
}

function NativeTimelineItem({
  item,
  stream,
  onImage,
  onNavigate,
  full = false,
}: {
  item: Item;
  stream?: StreamItem;
  onImage: TimelineProps["onImage"];
  onNavigate: TimelineProps["onNavigate"];
  full?: boolean;
}) {
  const formattedJson = full ? json : boundedJson;
  const data = item.data;
  const type = item.type || string(data.type);
  const assets = list(data._assets) as Asset[];
  const references = list(data._references) as Reference[];
  const running = ["inProgress", "in_progress", "running", "started"].includes(
    string(data.status),
  );
  const common = { onImage, onNavigate };
  const footer = (
    <>
      <References references={references} onNavigate={onNavigate} />
      <AssetGallery assets={assets} onImage={onImage} />
    </>
  );
  if (type === "userMessage" || type === "user_message") {
    const nativeImages = list(data.content)
      .map(object)
      .filter((content) => content.type === "image")
      .map((content) => string(content.url))
      .filter((url) => url.startsWith("https://"));
    return (
      <article
        className="message user-message"
        id={`item-${item.native_id || item.id}`}
        data-item-id={item.id}
      >
        <div className="message-heading">
          <span className="avatar avatar-user">你</span>
          <span>你</span>
          <time>{timeLabel(item.created_at)}</time>
        </div>
        <div className="user-bubble">
          <p className="user-text">{messageText(item)}</p>
          {footer}
          {nativeImages.map((src) => (
            <AssetGallery
              key={src}
              assets={[
                {
                  id: src,
                  name: "附加图片",
                  mime: "image/png",
                  size: 0,
                  url: src,
                },
              ]}
              onImage={onImage}
            />
          ))}
        </div>
      </article>
    );
  }
  if (type === "agentMessage" || type === "agent_message") {
    const text = stream?.text || messageText(item);
    return (
      <article
        className="message agent-message"
        id={`item-${item.native_id || item.id}`}
        data-item-id={item.id}
      >
        <div className="message-heading">
          <span className="avatar avatar-agent">
            <Bot size={16} />
          </span>
          <span>Codex</span>
          {data._pending ? (
            <span className="live-label">正在生成</span>
          ) : (
            <time>{timeLabel(item.created_at)}</time>
          )}
          <CopyButton text={text} />
        </div>
        <div className="agent-body">
          <Markdown text={text} assets={assets} {...common} />
          <References references={references} onNavigate={onNavigate} />
          <AssetGallery
            assets={galleryAssets(text, assets)}
            onImage={onImage}
          />
          {stream && <span className="stream-cursor" aria-label="正在生成" />}
        </div>
      </article>
    );
  }
  const content = (() => {
    if (type === "commandExecution" || type === "command_execution")
      return (
        <Disclosure
          icon={<Terminal size={15} />}
          title={string(data.command, "执行命令")}
          meta={
            typeof data.exitCode === "number"
              ? `退出 ${data.exitCode}`
              : string(data.status)
          }
          running={running}
          error={typeof data.exitCode === "number" && data.exitCode !== 0}
        >
          <div className="command-header">
            <span>{string(data.cwd)}</span>
            <CopyButton text={string(data.command)} />
          </div>
          <pre className="terminal-output">
            <span className="terminal-prompt">$ </span>
            {string(data.command)}
            {`\n\n${string(data.aggregatedOutput || data.output)}${stream?.text || ""}`}
          </pre>
        </Disclosure>
      );
    if (type === "fileChange" || type === "file_change")
      return (
        <Disclosure
          icon={<FileDiff size={15} />}
          title={`修改了 ${list(data.changes).length} 个文件`}
          meta={string(data.status)}
          running={running}
        >
          {list(data.changes).map((change, index) => {
            const entry = object(change);
            return (
              <section className="diff-file" key={index}>
                <h4>
                  <FileDiff size={14} />
                  {string(entry.path)}
                  <span>{string(object(entry.kind).type || entry.kind)}</span>
                </h4>
                <pre className="diff-output">
                  {string(entry.diff)
                    .split("\n")
                    .map((line, lineIndex) => (
                      <span
                        key={lineIndex}
                        className={
                          line.startsWith("+") && !line.startsWith("+++")
                            ? "diff-added"
                            : line.startsWith("-") && !line.startsWith("---")
                              ? "diff-removed"
                              : ""
                        }
                      >
                        {line || " "}
                        {"\n"}
                      </span>
                    ))}
                </pre>
              </section>
            );
          })}
        </Disclosure>
      );
    if (type === "reasoning")
      return (
        <Disclosure
          icon={<MessageSquare size={15} />}
          title="思考过程"
          running={running}
        >
          <Markdown
            text={
              [...list(data.summary), ...list(data.content)]
                .map((part) =>
                  typeof part === "string" ? part : string(object(part).text),
                )
                .join("\n\n") + (stream?.text || "")
            }
            {...common}
          />
        </Disclosure>
      );
    if (type === "webSearch" || type === "web_search") {
      const links = findLinks(data);
      return (
        <Disclosure
          icon={<Globe size={15} />}
          title={`搜索网页${data.query ? ` · ${string(data.query)}` : ""}`}
          meta={links.length ? `${links.length} 个来源` : undefined}
          running={running}
        >
          {links.length ? (
            <div className="source-list">
              {links.map((link) => (
                <a
                  href={link.url}
                  key={link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Globe size={14} />
                  <span>
                    {link.title}
                    <small>{new URL(link.url).hostname}</small>
                  </span>
                </a>
              ))}
            </div>
          ) : (
            <p className="muted">
              Codex 返回了搜索操作记录，当前事件未包含可跳转的来源。
            </p>
          )}
          <details className="raw-details">
            <summary>操作详情</summary>
            <pre>{formattedJson(data)}</pre>
          </details>
        </Disclosure>
      );
    }
    if (type === "imageView" || type === "imageGeneration")
      return (
        <div className="image-result">
          <div className="image-result-label">
            <ImageIcon size={15} />
            {type === "imageGeneration" ? "生成图片" : "查看图片"}
          </div>
          {assets.length === 0 && (
            <p className="muted">
              {running ? "正在处理图片…" : "等待节点同步图片工件。"}
            </p>
          )}
        </div>
      );
    if (type === "plan")
      return (
        <Disclosure
          icon={<ListChecks size={15} />}
          title="执行计划"
          running={running}
        >
          <Markdown
            text={string(data.text, json(data.plan || data))}
            {...common}
          />
        </Disclosure>
      );
    if (type === "contextCompaction")
      return (
        <div className="system-note">
          <CheckCheck size={14} />
          上下文已整理
        </div>
      );
    if (type === "mcpToolCall" || type === "dynamicToolCall")
      return (
        <Disclosure
          icon={<Wrench size={15} />}
          title={`${string(data.server)}${data.server ? " / " : ""}${string(data.tool, "工具调用")}`}
          meta={string(data.status)}
          running={running}
          error={Boolean(data.error)}
        >
          <p className="eyebrow">输入</p>
          <pre>{formattedJson(data.arguments)}</pre>
          {data.result != null && (
            <>
              <p className="eyebrow">结果</p>
              <pre>{formattedJson(data.result)}</pre>
            </>
          )}
          {data.error != null && (
            <pre className="text-error">{formattedJson(data.error)}</pre>
          )}
        </Disclosure>
      );
    return (
      <Disclosure
        icon={<Wrench size={15} />}
        title={type || "工具事件"}
        meta={string(data.status)}
        running={running}
      >
        <pre>
          {formattedJson(
            Object.fromEntries(
              Object.entries(data).filter(([key]) => !key.startsWith("_")),
            ),
          )}
        </pre>
      </Disclosure>
    );
  })();
  return (
    <article
      className="tool-item"
      id={`item-${item.native_id || item.id}`}
      data-item-id={item.id}
    >
      {content}
      {footer}
    </article>
  );
}

function TimelineItem(props: {
  item: Item;
  stream?: StreamItem;
  onImage: TimelineProps["onImage"];
  onNavigate: TimelineProps["onNavigate"];
  reveal?: boolean;
}) {
  const { item, reveal } = props;
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [raw, setRaw] = useState<Item | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), [item.id]);
  useLayoutEffect(() => {
    if (reveal) {
      const details =
        wrapperRef.current?.querySelector<HTMLDetailsElement>(
          "details.tool-card",
        );
      if (details) details.open = true;
    }
  }, [reveal, item.id]);
  const expand = async () => {
    if (expanded) {
      setExpanded(false);
      return;
    }
    if (raw) {
      setExpanded(true);
      return;
    }
    if (loading) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const result = await api<{ item: Item }>(
        `/threads/${encodeURIComponent(item.thread_id)}/items/${encodeURIComponent(item.id)}`,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) {
        setRaw(result.item);
        setExpanded(true);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(errorText(reason));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  const shown =
    expanded && raw
      ? {
          ...raw,
          data: {
            ...raw.data,
            ...(item.data._assets ? { _assets: item.data._assets } : {}),
            ...(item.data._references
              ? { _references: item.data._references }
              : {}),
          },
        }
      : item;
  return (
    <div className="timeline-item-wrapper" ref={wrapperRef}>
      <NativeTimelineItem {...props} item={shown} full={expanded} />
      {item.data._truncated === true && (
        <div className="item-expansion">
          <button
            className="text-button"
            disabled={loading}
            onClick={() => void expand()}
          >
            {loading ? (
              <Spinner label="读取完整内容" />
            ) : expanded ? (
              "收起完整内容"
            ) : (
              "查看完整内容"
            )}
          </button>
          {error && (
            <p className="text-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function WorkProcess({
  group,
  active,
  targetItem,
  onImage,
  onNavigate,
}: {
  group: WorkGroup;
  active: boolean;
  targetItem?: string;
  onImage: TimelineProps["onImage"];
  onNavigate: TimelineProps["onNavigate"];
}) {
  const containsTarget = Boolean(
    targetItem &&
    group.entries.some(
      ({ item }) => item.id === targetItem || item.native_id === targetItem,
    ),
  );
  const [expanded, setExpanded] = useState(containsTarget);
  const [mounted, setMounted] = useState(containsTarget);
  const revealedTarget = useRef(containsTarget ? targetItem : undefined);
  const reveal = containsTarget && revealedTarget.current !== targetItem;
  const open = expanded || reveal;
  useLayoutEffect(() => {
    if (!containsTarget) revealedTarget.current = undefined;
    else if (reveal) {
      revealedTarget.current = targetItem;
      setExpanded(true);
      setMounted(true);
    }
  }, [containsTarget, reveal, targetItem]);
  const running = active || group.entries.some(entryRunning);
  const current = [...group.entries].reverse().find(entryRunning);
  const { counts, failures } = workSummary(group.entries);
  return (
    <details
      className={`work-process${running ? " work-process-running" : ""}${failures ? " work-process-error" : ""}`}
      open={open}
    >
      <summary
        onClick={(event) => {
          event.preventDefault();
          setMounted(true);
          setExpanded(!open);
        }}
      >
        <ChevronRight className="work-process-arrow" size={14} />
        <span className="work-process-status" aria-hidden="true">
          {failures ? (
            <AlertCircle size={14} />
          ) : running ? (
            <LoaderCircle className="spin" size={14} />
          ) : (
            <Check size={14} />
          )}
        </span>
        <span className="work-process-title">
          {running ? "正在工作" : "工作过程"}
        </span>
        <span className="work-process-counts">{counts}</span>
        {failures > 0 && (
          <span className="work-process-failure">{failures} 项失败</span>
        )}
        {running && current && (
          <span className="work-process-current">
            {activeWorkLabel(current)}
          </span>
        )}
      </summary>
      {(mounted || open) && (
        <div className="work-process-items">
          {group.entries.map(({ item, stream }) => (
            <TimelineItem
              key={item.native_id || item.id}
              item={item}
              stream={stream}
              reveal={item.id === targetItem || item.native_id === targetItem}
              onImage={onImage}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </details>
  );
}

export default function Timeline({
  items,
  streams,
  busy,
  loading,
  importing = false,
  hasMore = false,
  loadingOlder = false,
  olderError = "",
  onLoadOlder,
  targetItem,
  onImage,
  onNavigate,
}: TimelineProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [nearBottom, setNearBottom] = useState(true);
  const previousLastItem = useRef("");
  const revealedTarget = useRef("");
  const targetHighlight = useRef<{
    element: HTMLElement;
    timer: ReturnType<typeof setTimeout>;
  } | null>(null);
  useEffect(
    () => () => {
      const highlight = targetHighlight.current;
      if (highlight) {
        clearTimeout(highlight.timer);
        highlight.element.classList.remove("referenced-item");
      }
    },
    [],
  );
  const previousUnits = useRef<TimelineUnit[]>([]);
  const units = timelineUnits(items, streams, previousUnits.current);
  previousUnits.current = units;
  const pendingCount = units.reduce(
    (total, unit) =>
      total +
      (unit.kind === "work"
        ? unit.entries.filter((entry) => entry.pending).length
        : Number(Boolean(unit.entry.pending))),
    0,
  );
  const prependPosition = useRef<{ height: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!loadingOlder && prependPosition.current && element) {
      element.scrollTop =
        prependPosition.current.top +
        element.scrollHeight -
        prependPosition.current.height;
      prependPosition.current = null;
    }
  }, [items, loadingOlder]);
  const scrollKey = Object.values(streams).reduce(
    (total, stream) => total + stream.text.length,
    0,
  );
  useEffect(() => {
    if (!targetItem) revealedTarget.current = "";
    if (targetItem && targetItem !== revealedTarget.current) {
      const matching = items.find(
        (item) => item.id === targetItem || item.native_id === targetItem,
      );
      if (matching) {
        const element = document.getElementById(
          `item-${matching.native_id || matching.id}`,
        );
        if (element) {
          const previous = targetHighlight.current;
          if (previous) {
            clearTimeout(previous.timer);
            previous.element.classList.remove("referenced-item");
          }
          element.scrollIntoView({ block: "center", behavior: "smooth" });
          element.classList.add("referenced-item");
          targetHighlight.current = {
            element,
            timer: setTimeout(
              () => element.classList.remove("referenced-item"),
              3500,
            ),
          };
          revealedTarget.current = targetItem;
        }
      }
    } else if (nearBottom && !targetItem)
      endRef.current?.scrollIntoView({
        behavior: previousLastItem.current ? "smooth" : "instant",
        block: "end",
      });
    previousLastItem.current = items.at(-1)?.id || "";
  }, [items, scrollKey, targetItem, nearBottom]);
  if (loading && !items.length)
    return (
      <div className="timeline-loading">
        <Spinner label="正在读取对话" />
      </div>
    );
  return (
    <div
      className="timeline-scroll"
      ref={scrollRef}
      onScroll={() => {
        const element = scrollRef.current;
        if (element)
          setNearBottom(
            element.scrollHeight - element.scrollTop - element.clientHeight <
              130,
          );
      }}
    >
      <div className="timeline-inner">
        {(hasMore || olderError) && (
          <div className="history-pagination">
            {hasMore && (
              <button
                className="text-button"
                disabled={loadingOlder}
                onClick={() => {
                  const element = scrollRef.current;
                  if (element)
                    prependPosition.current = {
                      height: element.scrollHeight,
                      top: element.scrollTop,
                    };
                  setNearBottom(false);
                  onLoadOlder?.();
                }}
              >
                {loadingOlder ? (
                  <Spinner label="加载更早消息" />
                ) : (
                  "加载更早消息"
                )}
              </button>
            )}
            {olderError && (
              <p className="text-error" role="alert">
                {olderError}
              </p>
            )}
          </div>
        )}
        {targetItem &&
          !items.some(
            (item) => item.id === targetItem || item.native_id === targetItem,
          ) &&
          !loading &&
          !importing && (
            <p className="reference-search-note">
              {loadingOlder
                ? "正在定位引用消息…"
                : hasMore
                  ? "引用位于更早的历史中，可继续加载消息。"
                  : "未找到这条引用消息。"}
            </p>
          )}
        {!items.length && !pendingCount && (
          <div className="thread-empty">
            <span className="empty-orbit">
              <Bot size={28} />
            </span>
            <h2>{importing ? "正在导入会话历史" : "从一个想法开始"}</h2>
            <p>
              {importing
                ? "历史消息和图片会逐步显示，导入完成后可以继续原会话。"
                : "描述目标、附上截图，或引用已有任务。"}
              <br />
              {importing
                ? "导入历史不会发起模型推理。"
                : "Codex 将在所选设备的项目目录中执行。"}
            </p>
          </div>
        )}
        {units.map((unit, index) =>
          unit.kind === "work" ? (
            <WorkProcess
              key={unit.key}
              group={unit}
              active={busy && index === units.length - 1}
              targetItem={targetItem}
              onImage={onImage}
              onNavigate={onNavigate}
            />
          ) : (
            <TimelineItem
              key={`item:${unit.key}`}
              item={unit.entry.item}
              stream={unit.entry.stream}
              reveal={
                unit.entry.item.id === targetItem ||
                unit.entry.item.native_id === targetItem
              }
              onImage={onImage}
              onNavigate={onNavigate}
            />
          ),
        )}
        {busy &&
          !pendingCount &&
          !Object.keys(streams).length &&
          units.at(-1)?.kind !== "work" && (
            <div className="working-indicator">
              <span className="working-dots">
                <i />
                <i />
                <i />
              </span>
              Codex 正在工作
            </div>
          )}
        <div ref={endRef} className="timeline-end" />
      </div>
      {!nearBottom && items.length > 0 && (
        <button
          className="scroll-bottom"
          onClick={() => {
            setNearBottom(true);
            endRef.current?.scrollIntoView({ behavior: "smooth" });
          }}
        >
          <ArrowDown size={15} />
          最新消息
        </button>
      )}
    </div>
  );
}
