import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Bell,
  ExternalLink,
  RefreshCw,
} from "lucide-react";
import { ApiError, api, errorText } from "./api";
import { ErrorMessage, Modal, Spinner } from "./components";
import Interactions from "./Interactions";
import type {
  Command,
  Host,
  Interaction,
  InteractionPage,
  PendingInteraction,
} from "./types";

const PAGE_SIZE = 50;
const NOTIFIED_KEY = "anywhere:interaction-notifications";
const emptyPage: InteractionPage = {
  interactions: [],
  total: 0,
  has_more: false,
  next_offset: null,
};

function rememberedNotifications() {
  try {
    const values: unknown = JSON.parse(
      sessionStorage.getItem(NOTIFIED_KEY) || "[]",
    );
    return new Set(
      Array.isArray(values)
        ? values.filter((id) => typeof id === "string")
        : [],
    );
  } catch {
    return new Set<string>();
  }
}

function notificationPermission(): NotificationPermission | "unsupported" {
  return typeof Notification === "undefined"
    ? "unsupported"
    : Notification.permission;
}

export default function InteractionAttention({
  refreshKey,
  hosts,
  onNavigate,
  onCommand,
}: {
  refreshKey: number;
  hosts: Host[];
  onNavigate: (threadId: string) => void;
  onCommand: (command: Command) => void;
}) {
  const [page, setPage] = useState(emptyPage);
  const pageRef = useRef(emptyPage);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<PendingInteraction | null>(null);
  const selectedRef = useRef<PendingInteraction | null>(null);
  const [detail, setDetail] = useState<Interaction | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailUnverified, setDetailUnverified] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [permission, setPermission] = useState(notificationPermission);
  const [permissionBusy, setPermissionBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const listRequest = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const listVersion = useRef(0);
  const detailVersion = useRef(0);
  const notified = useRef(rememberedNotifications());
  const notifications = useRef(new Set<Notification>());
  const originalTitle = useRef(document.title || "Codex Anywhere");
  const mounted = useRef(true);

  const notify = useCallback(
    (items: PendingInteraction[], totalIncreased: boolean) => {
      if (
        notificationPermission() !== "granted" ||
        (!document.hidden && document.hasFocus())
      )
        return;
      const unseen = items.filter((item) => !notified.current.has(item.id));
      if (!unseen.length && !totalIncreased) return;
      try {
        const notification = new Notification("Codex Anywhere · 待处理交互", {
          body: "有新的问题或审批等待处理，点击打开页面查看。",
          tag: unseen.length
            ? `codex-anywhere-interaction-${unseen[0].id}`
            : "codex-anywhere-pending-count",
        });
        unseen.forEach((item) => notified.current.add(item.id));
        try {
          sessionStorage.setItem(
            NOTIFIED_KEY,
            JSON.stringify([...notified.current]),
          );
        } catch {
          /* In-page reminders work when browser storage is unavailable. */
        }
        notifications.current.add(notification);
        notification.onclick = () => {
          if (!mounted.current) return;
          window.focus();
          setOpen(true);
          notification.close();
        };
        notification.onclose = () => notifications.current.delete(notification);
      } catch {
        setNotice("浏览器暂时无法显示桌面提醒，请留意页面上的待处理提示。");
      }
    },
    [],
  );

  const loadDetail = useCallback(async (metadata: PendingInteraction) => {
    if (!mounted.current) return;
    detailRequest.current?.abort();
    const controller = new AbortController();
    detailRequest.current = controller;
    const version = ++detailVersion.current;
    setDetailLoading(true);
    try {
      const result = await api<{ interaction: Interaction }>(
        `/interactions/${metadata.id}`,
        { signal: controller.signal },
      );
      if (
        controller.signal.aborted ||
        version !== detailVersion.current ||
        selectedRef.current?.id !== metadata.id
      )
        return;
      setDetail(result.interaction);
      setDetailError("");
      setDetailUnverified(false);
    } catch (reason) {
      if (controller.signal.aborted || version !== detailVersion.current)
        return;
      if (reason instanceof ApiError && [404, 409].includes(reason.status))
        setDetail(null);
      setDetailError(errorText(reason));
    } finally {
      if (!controller.signal.aborted && version === detailVersion.current)
        setDetailLoading(false);
    }
  }, []);

  const loadPage = useCallback(
    async (append = false) => {
      if (!mounted.current) return;
      const previous = pageRef.current;
      if (append && (!previous.has_more || previous.next_offset === null))
        return;
      listRequest.current?.abort();
      const controller = new AbortController();
      listRequest.current = controller;
      const version = ++listVersion.current;
      setLoading(true);
      try {
        const offset = append ? previous.next_offset! : 0;
        const readPage = async (cursor: number) => {
          const result = await api<InteractionPage>(
            `/interactions?limit=${PAGE_SIZE}&offset=${cursor}`,
            { signal: controller.signal },
          );
          if (
            !Array.isArray(result.interactions) ||
            !Number.isInteger(result.total) ||
            result.total < 0 ||
            (result.has_more &&
              (!Number.isInteger(result.next_offset) ||
                result.next_offset! <= cursor ||
                !result.interactions.length))
          ) {
            throw new Error("待处理列表正在变化，请刷新后重试。");
          }
          return result;
        };
        let result = await readPage(offset);
        let items = append
          ? [...previous.interactions, ...result.interactions]
          : result.interactions;
        let pagesRead = 1;
        const maxPages = Math.max(
          2,
          Math.ceil(previous.interactions.length / PAGE_SIZE) + 1,
        );
        // Keep the user's expanded list across periodic refreshes.
        while (
          !append &&
          items.length < previous.interactions.length &&
          result.has_more &&
          result.next_offset !== null &&
          !controller.signal.aborted
        ) {
          if (pagesRead++ >= maxPages)
            throw new Error("待处理列表正在变化，请刷新后重试。");
          result = await readPage(result.next_offset);
          items = [...items, ...result.interactions];
        }
        if (controller.signal.aborted || version !== listVersion.current)
          return;
        items = [...new Map(items.map((item) => [item.id, item])).values()];
        const next = { ...result, interactions: items };
        pageRef.current = next;
        setPage(next);
        setLoadError("");
        setPermission(notificationPermission());
        if (!append)
          notify(items, previous.total > 0 && next.total > previous.total);
        const current = selectedRef.current;
        if (current) {
          // A selected request may be outside the loaded page or already resolved.
          const metadata =
            items.find((item) => item.id === current.id) || current;
          if (!next.has_more && !items.some((item) => item.id === current.id))
            setDetailUnverified(true);
          selectedRef.current = metadata;
          setSelected(metadata);
          void loadDetail(metadata);
        }
      } catch (reason) {
        if (!controller.signal.aborted && version === listVersion.current)
          setLoadError(errorText(reason));
      } finally {
        if (!controller.signal.aborted && version === listVersion.current)
          setLoading(false);
      }
    },
    [loadDetail, notify],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    void loadPage();
  }, [refreshKey, loadPage]);
  useEffect(() => {
    const refresh = () => {
      void loadPage();
    };
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
      listRequest.current?.abort();
      detailRequest.current?.abort();
      notifications.current.forEach((notification) => notification.close());
      notifications.current.clear();
    };
  }, [loadPage]);
  useEffect(() => {
    document.title = page.total
      ? `[${page.total} 待处理] Codex Anywhere`
      : originalTitle.current;
  }, [page.total]);
  useEffect(
    () => () => {
      document.title = originalTitle.current;
    },
    [],
  );

  const clearSelection = () => {
    selectedRef.current = null;
    setSelected(null);
    detailRequest.current?.abort();
    detailVersion.current++;
    setDetail(null);
    setDetailError("");
    setDetailUnverified(false);
    setDetailLoading(false);
  };
  const close = () => {
    setOpen(false);
    clearSelection();
  };
  const enableNotifications = async () => {
    if (permissionBusy || permission === "unsupported") return;
    setPermissionBusy(true);
    try {
      const value = await Notification.requestPermission();
      if (!mounted.current) return;
      setPermission(value);
      setNotice(
        value === "granted"
          ? "已启用桌面提醒，页面在后台时会提示新的待处理交互。"
          : value === "denied"
            ? "桌面提醒已被浏览器关闭，可在站点设置中开启。"
            : "未启用桌面提醒，页面内提示仍然有效。",
      );
      if (value === "granted") void loadPage();
    } catch {
      if (mounted.current)
        setNotice("无法启用桌面提醒，请检查浏览器的站点通知设置。");
    } finally {
      if (mounted.current) setPermissionBusy(false);
    }
  };
  const online = selected
    ? hosts.find((host) => host.id === selected.host_id)?.online === true
    : false;

  return (
    <>
      <button
        type="button"
        className={`interaction-attention-button ${page.total ? "has-pending" : ""} ${loadError ? "has-error" : ""}`}
        aria-label="待处理交互"
        title={
          loadError
            ? "待处理交互暂时无法更新，点击查看并重试"
            : "查看待处理的问题和审批"
        }
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        {loadError ? (
          <AlertCircle size={16} aria-hidden="true" />
        ) : (
          <Bell size={16} aria-hidden="true" />
        )}
        <span>待处理</span>
        {page.total > 0 && (
          <span className="interaction-attention-count">{page.total}</span>
        )}
      </button>
      <span
        className="interaction-attention-live"
        role="status"
        aria-live="polite"
      >
        {loadError
          ? "待处理交互暂时无法更新，请打开待处理列表重试。"
          : page.total > 0
            ? `有 ${page.total} 项待处理交互，需要你的回答或审批。`
            : ""}
      </span>
      {open && (
        <Modal
          title="待处理交互"
          subtitle={`共 ${page.total} 项，支持在这里回答问题和处理审批。`}
          onClose={close}
          wide
        >
          <div className="interaction-attention-panel">
            <div className="interaction-attention-toolbar">
              {selected ? (
                <button
                  type="button"
                  className="button button-small button-quiet"
                  onClick={clearSelection}
                >
                  <ArrowLeft size={14} />
                  返回列表
                </button>
              ) : (
                <span>所有机器和会话</span>
              )}
              <button
                type="button"
                className="button button-small button-quiet"
                disabled={loading}
                onClick={() => void loadPage()}
              >
                <RefreshCw size={14} />
                刷新
              </button>
            </div>
            {loadError && <ErrorMessage>{loadError}</ErrorMessage>}
            {selected ? (
              <div className="interaction-attention-detail">
                <div className="interaction-attention-context">
                  <div>
                    <strong>{selected.thread_title}</strong>
                    <small>
                      {selected.host_name}
                      {!online ? " · 节点离线" : ""}
                    </small>
                  </div>
                  {selected.thread_id && (
                    <button
                      type="button"
                      className="button button-small button-quiet"
                      onClick={() => {
                        const id = selected.thread_id;
                        close();
                        if (id) onNavigate(id);
                      }}
                    >
                      <ExternalLink size={14} />
                      打开会话
                    </button>
                  )}
                </div>
                {detailLoading && !detail && <Spinner label="加载交互请求" />}
                {detailError && <ErrorMessage>{detailError}</ErrorMessage>}
                {detailUnverified && detailLoading && (
                  <Spinner label="同步交互状态" />
                )}
                {detail &&
                  !detailUnverified &&
                  (["pending", "open", "waiting", "answering"].includes(
                    detail.status,
                  ) ? (
                    <Interactions
                      interactions={[detail]}
                      hostOnline={online}
                      onCommand={(command) => {
                        void loadPage();
                        onCommand(command);
                      }}
                      onResolved={() => void loadPage()}
                    />
                  ) : (
                    <p role="status">这条请求已处理或已过期。</p>
                  ))}
              </div>
            ) : (
              <>
                <ul
                  className="interaction-attention-list"
                  aria-label="待处理交互列表"
                >
                  {page.interactions.map((item) => (
                    <li key={item.id}>
                      <button
                        type="button"
                        onClick={() => {
                          selectedRef.current = item;
                          setSelected(item);
                          setDetail(null);
                          setDetailError("");
                          setDetailUnverified(false);
                          void loadDetail(item);
                        }}
                      >
                        <span className="interaction-attention-item-title">
                          {item.thread_title}
                        </span>
                        <span className="interaction-attention-item-meta">
                          {item.host_name} ·{" "}
                          {item.question_count
                            ? `${item.question_count} 个问题`
                            : item.method.toLowerCase().includes("approval")
                              ? "审批请求"
                              : "交互请求"}
                          {!item.blocking ? " · 可稍后回答" : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {loading && <Spinner label="加载待处理交互" />}
                {!loading && !loadError && page.total === 0 && (
                  <p className="interaction-attention-empty">
                    当前没有待处理的交互。
                  </p>
                )}
                {page.has_more && (
                  <button
                    type="button"
                    className="button button-small"
                    disabled={loading}
                    onClick={() => void loadPage(true)}
                  >
                    加载更多
                  </button>
                )}
              </>
            )}
            <footer className="interaction-attention-notifications">
              <button
                type="button"
                className="button button-small button-quiet"
                disabled={
                  permissionBusy ||
                  permission === "unsupported" ||
                  permission === "granted" ||
                  permission === "denied"
                }
                onClick={() => void enableNotifications()}
              >
                {permissionBusy
                  ? "正在启用…"
                  : permission === "granted"
                    ? "桌面提醒已启用"
                    : "启用桌面提醒"}
              </button>
              <small>
                {notice ||
                  (permission === "unsupported"
                    ? "此浏览器不支持桌面提醒，仍会显示页面内提示。"
                    : permission === "denied"
                      ? "桌面提醒已被浏览器关闭，可在站点设置中开启。"
                      : "桌面提醒仅在此页面仍打开时有效。")}
              </small>
            </footer>
          </div>
        </Modal>
      )}
    </>
  );
}
