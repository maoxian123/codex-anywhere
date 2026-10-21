import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Download,
  Folder,
  Monitor,
  Search,
} from "lucide-react";
import { errorText, post } from "./api";
import {
  remoteCommand,
  waitForCommand,
  waitForImportedThread,
} from "./commands";
import { ErrorMessage, Modal, Spinner } from "./components";
import type { Command, Host, Thread } from "./types";
import { list, object, string } from "./utils";

type CatalogEntry = {
  id: string;
  cwd: string;
  title: string;
  preview: string;
  updated?: number | string;
};
type CatalogPage = { entries: CatalogEntry[]; next: string | null };
function catalogPage(result: unknown): CatalogPage {
  const data = object(result);
  return {
    entries: list(data.data)
      .map(object)
      .filter((entry) => typeof entry.id === "string")
      .map((entry) => ({
        id: string(entry.id),
        cwd: string(entry.cwd),
        title: string(entry.name || entry.title || entry.preview, "未命名会话"),
        preview: string(entry.preview),
        updated:
          typeof entry.updatedAt === "number" ||
          typeof entry.updatedAt === "string"
            ? entry.updatedAt
            : undefined,
      })),
    next:
      typeof data.nextCursor === "string" && data.nextCursor
        ? data.nextCursor
        : null,
  };
}
export function ImportDialog({
  hosts,
  onClose,
  onImported,
  onNavigate,
}: {
  hosts: Host[];
  onClose: () => void;
  onImported: (thread: Thread) => void;
  onNavigate: (id: string, projectId?: string | null) => void;
}) {
  const [hostId, setHostId] = useState(
    hosts.find((host) => host.online)?.id || "",
  );
  const host = hosts.find((value) => value.id === hostId);
  const [search, setSearch] = useState("");
  const [cwd, setCwd] = useState("");
  const [archived, setArchived] = useState(false);
  const [pages, setPages] = useState<CatalogPage[]>([]);
  const [index, setIndex] = useState(0);
  const [selected, setSelected] = useState(new Set<string>());
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [results, setResults] = useState<
    Record<
      string,
      {
        title: string;
        error?: string;
        threadId?: string;
        projectId?: string | null;
      }
    >
  >({});
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
  }, []);
  const load = async (cursor?: string, nextIndex = 0) => {
    if (!host?.online || importing) return;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setLoading(true);
    setError("");
    try {
      const result = await remoteCommand<unknown>(
        `/hosts/${hostId}/catalog`,
        {
          limit: 50,
          archived,
          ...(cursor ? { cursor } : {}),
          ...(search.trim() ? { search: search.trim() } : {}),
          ...(cwd.trim() ? { cwd: cwd.trim() } : {}),
        },
        request.signal,
      );
      if (request.signal.aborted || !alive.current) return;
      const page = catalogPage(result);
      setPages((current) =>
        nextIndex ? [...current.slice(0, nextIndex), page] : [page],
      );
      setIndex(nextIndex);
    } catch (reason) {
      if (!request.signal.aborted && alive.current) setError(errorText(reason));
    } finally {
      if (!request.signal.aborted && alive.current) setLoading(false);
    }
  };
  useEffect(() => {
    controller.current?.abort();
    setPages([]);
    setIndex(0);
    setSelected(new Set());
    setResults({});
    setError("");
    const timer = setTimeout(() => void load(), 250);
    return () => {
      clearTimeout(timer);
      controller.current?.abort();
    };
  }, [hostId, search, archived, cwd]);
  const current = pages[index];
  const loaded = pages.flatMap((page) => page.entries);
  const pageIds = current?.entries.map((entry) => entry.id) || [];
  const allPageSelected =
    pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const groups = new Map<string, CatalogEntry[]>();
  current?.entries.forEach((entry) =>
    groups.set(entry.cwd, [...(groups.get(entry.cwd) || []), entry]),
  );
  const toggle = (id: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const importSelected = async () => {
    if (!host?.online || importing || !selected.size) return;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const entries = [
      ...new Map(
        loaded
          .filter((entry) => selected.has(entry.id))
          .map((entry) => [entry.id, entry]),
      ).values(),
    ];
    setImporting(true);
    setError("");
    for (let entryIndex = 0; entryIndex < entries.length; entryIndex += 1) {
      if (request.signal.aborted || !alive.current) break;
      const entry = entries[entryIndex];
      setProgress(
        `导入 ${entryIndex + 1} / ${entries.length} · ${entry.title.slice(0, 60)}`,
      );
      try {
        const result = await post<{ thread: Thread; command: Command }>(
          `/hosts/${hostId}/imports`,
          {
            native_thread_id: entry.id,
            title: entry.title.slice(0, 200),
            ...(entry.cwd ? { cwd: entry.cwd } : {}),
          },
        );
        await waitForCommand(result.command, request.signal);
        const importedThread = await waitForImportedThread(
          result.thread.id,
          request.signal,
        );
        if (request.signal.aborted || !alive.current) break;
        onImported(importedThread);
        setResults((previous) => ({
          ...previous,
          [entry.id]: {
            title: importedThread.title || entry.title,
            threadId: importedThread.id,
            projectId: importedThread.project_id,
          },
        }));
        setSelected((previous) => {
          const next = new Set(previous);
          next.delete(entry.id);
          return next;
        });
      } catch (reason) {
        if (request.signal.aborted || !alive.current) break;
        setResults((previous) => ({
          ...previous,
          [entry.id]: { title: entry.title, error: errorText(reason) },
        }));
      }
    }
    if (!request.signal.aborted && alive.current) {
      setImporting(false);
      setProgress("");
    }
  };
  return (
    <Modal
      title="导入本机 Codex 会话"
      subtitle="按需读取所选设备的会话目录，再选择需要导入的历史。导入本身不会发起模型推理。"
      onClose={onClose}
      wide
    >
      <div className="import-controls">
        <label>
          <span className="label-with-icon">
            <Monitor size={14} />
            工作设备
          </span>
          <select
            value={hostId}
            onChange={(event) => setHostId(event.target.value)}
            disabled={importing}
          >
            <option value="" disabled>
              选择在线设备
            </option>
            {hosts.map((value) => (
              <option key={value.id} value={value.id} disabled={!value.online}>
                {value.name}
                {value.online ? "" : " · 离线"}
              </option>
            ))}
          </select>
        </label>
        <label>
          搜索会话
          <div className="search-box">
            <Search size={14} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="标题或预览内容"
              disabled={importing}
            />
          </div>
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={archived}
            onChange={(event) => setArchived(event.target.checked)}
            disabled={importing}
          />
          已归档会话
        </label>
      </div>
      <details className="advanced-settings">
        <summary>按工作目录筛选</summary>
        <input
          aria-label="会话工作目录筛选"
          placeholder="可选：设备上的绝对目录"
          value={cwd}
          onChange={(event) => setCwd(event.target.value)}
          disabled={importing}
        />
      </details>
      <div className="import-selection">
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={allPageSelected}
            disabled={loading || importing || !pageIds.length}
            onChange={() =>
              setSelected((previous) => {
                const next = new Set(previous);
                pageIds.forEach((id) =>
                  allPageSelected ? next.delete(id) : next.add(id),
                );
                return next;
              })
            }
          />
          选择当前页
        </label>
        <button
          className="text-button"
          disabled={loading || importing || !loaded.length}
          onClick={() => setSelected(new Set(loaded.map((entry) => entry.id)))}
        >
          选择已加载的全部（{loaded.length}）
        </button>
        <span>已选 {selected.size}</span>
        <button
          className="text-button"
          disabled={importing || !selected.size}
          onClick={() => setSelected(new Set())}
        >
          清空
        </button>
      </div>
      <div className="catalog-list">
        {loading ? (
          <div className="empty-small">
            <Spinner label="读取设备上的会话目录" />
          </div>
        ) : current?.entries.length ? (
          [...groups.entries()].map(([path, entries]) => (
            <section className="catalog-group" key={path}>
              <h3>
                <Folder size={14} />
                <span>{path || "未记录工作目录"}</span>
              </h3>
              {entries.map((entry) => (
                <label className="catalog-entry" key={entry.id}>
                  <input
                    type="checkbox"
                    checked={selected.has(entry.id)}
                    onChange={() => toggle(entry.id)}
                    disabled={importing}
                  />
                  <span>
                    <strong>{entry.title.slice(0, 160)}</strong>
                    {entry.preview && entry.preview !== entry.title && (
                      <small>{entry.preview.slice(0, 200)}</small>
                    )}
                    <code>{entry.id}</code>
                  </span>
                  {results[entry.id]?.threadId && (
                    <Check size={15} className="text-success" />
                  )}
                </label>
              ))}
            </section>
          ))
        ) : (
          <p className="empty-small">
            {host?.online
              ? "没有找到匹配的本机会话。可切换设备、归档状态或搜索条件。"
              : "请选择一台在线设备。"}
          </p>
        )}
      </div>
      <div className="catalog-pagination">
        <button
          className="button button-small button-quiet"
          disabled={loading || importing || index === 0}
          onClick={() => setIndex((value) => value - 1)}
        >
          <ArrowLeft size={14} />
          上一页
        </button>
        <span>第 {index + 1} 页 · 每页最多 50 条</span>
        <button
          className="button button-small button-quiet"
          disabled={loading || importing || !current?.next}
          onClick={() => {
            if (pages[index + 1]) setIndex((value) => value + 1);
            else if (current?.next) void load(current.next, index + 1);
          }}
        >
          下一页
          <ArrowRight size={14} />
        </button>
      </div>
      {error && <ErrorMessage>{error}</ErrorMessage>}
      {Object.keys(results).length > 0 && (
        <div className="import-results">
          {Object.entries(results).map(([id, result]) => (
            <div
              key={id}
              className={result.error ? "text-error" : "text-success"}
            >
              <span>
                {result.title.slice(0, 80)} · {result.error || "导入完成"}
              </span>
              {result.threadId && (
                <button
                  className="text-button"
                  onClick={() => {
                    onNavigate(result.threadId!, result.projectId);
                    onClose();
                  }}
                >
                  打开会话
                  <ArrowRight size={12} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="modal-actions">
        <p className="field-hint">
          {importing
            ? progress
            : "仅导入所选会话的历史；关闭窗口后，已提交的导入仍会继续。"}
        </p>
        <button className="button button-quiet" onClick={onClose}>
          {importing ? "关闭窗口" : "取消"}
        </button>
        <button
          className="button button-primary"
          disabled={importing || loading || !host?.online || !selected.size}
          onClick={() => void importSelected()}
        >
          {importing ? (
            <Spinner label="导入中" />
          ) : (
            <>
              <Download size={15} />
              导入所选（{selected.size}）
            </>
          )}
        </button>
      </div>
    </Modal>
  );
}
