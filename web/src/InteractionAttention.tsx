import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
const currentStatuses = [
  "pending",
  "open",
  "waiting",
  "answering",
  "answer_unknown",
];

function CurrentQuestion({
  metadata,
  active,
  revision,
  hostOnline,
  stale,
  onDraftChange,
  onExpired,
  onRefresh,
  onCommand,
}: {
  metadata: PendingInteraction;
  active: boolean;
  revision: number;
  hostOnline: boolean;
  stale: boolean;
  onDraftChange: () => void;
  onExpired: (id: string) => void;
  onRefresh: () => void;
  onCommand: (command: Command) => void;
}) {
  const [detail, setDetail] = useState<Interaction | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const version = useRef(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const current = ++version.current;
    setLoading(true);
    void api<{ interaction: Interaction }>(
      `/interactions/${encodeURIComponent(metadata.id)}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (controller.signal.aborted || current !== version.current) return;
        if (!currentStatuses.includes(result.interaction.status)) {
          onExpired(metadata.id);
          return;
        }
        if (result.interaction.thread_id !== metadata.thread_id)
          throw new Error("问题所属会话已变化，请刷新后重试。");
        setDetail(result.interaction);
        setError("");
      })
      .catch((reason) => {
        if (controller.signal.aborted || current !== version.current) return;
        if (reason instanceof ApiError && [404, 409].includes(reason.status))
          onExpired(metadata.id);
        else setError(errorText(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted && current === version.current)
          setLoading(false);
      });
    return () => controller.abort();
  }, [active, metadata.id, revision, onExpired]);
  return (
    <section
      className="interaction-current-question"
      aria-label="当前轮次问题"
      hidden={!active}
    >
      <div className="interaction-current-question-context">
        <div>
          <strong>{metadata.thread_title}</strong>
          <small>
            {metadata.host_name}
            {!hostOnline ? " · 设备离线" : ""}
          </small>
        </div>
      </div>
      {!detail && loading && <Spinner label="读取当前问题" />}
      {error && <ErrorMessage>{error}</ErrorMessage>}
      {(error || stale) && (
        <button
          type="button"
          className="button button-small"
          disabled={loading}
          onClick={onRefresh}
        >
          重新确认当前问题
        </button>
      )}
      {detail && (
        <Interactions
          interactions={[detail]}
          hostOnline={hostOnline}
          verified={!loading && !error && !stale}
          onDraftChange={onDraftChange}
          onCommand={onCommand}
          onResolved={onRefresh}
        />
      )}
    </section>
  );
}

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
  bannerTarget,
  questionTarget,
  activeThreadId = null,
}: {
  refreshKey: number;
  hosts: Host[];
  onNavigate: (threadId: string) => void;
  onCommand: (command: Command) => void;
  bannerTarget?: HTMLElement | null;
  questionTarget?: HTMLElement | null;
  activeThreadId?: string | null;
}) {
  const [page, setPage] = useState(emptyPage);
  const pageRef = useRef(emptyPage);
  const [inlineId, setInlineId] = useState("");
  const inlineRef = useRef("");
  const activeThreadRef = useRef(activeThreadId);
  activeThreadRef.current = activeThreadId;
  const touchedQuestions = useRef(new Set<string>());
  const [pageRevision, setPageRevision] = useState(0);
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
        const previousIds = new Set(
          previous.interactions.map((item) => item.id),
        );
        const questions = items
          .filter(
            (item) =>
              item.question_count > 0 &&
              item.thread_id === activeThreadRef.current,
          )
          .sort(
            (left, right) =>
              right.created_at.localeCompare(left.created_at) ||
              Number(previousIds.has(left.id)) -
                Number(previousIds.has(right.id)),
          );
        const currentQuestion = questions.find(
          (item) => item.id === inlineRef.current,
        );
        const nextQuestion =
          currentQuestion && touchedQuestions.current.has(currentQuestion.id)
            ? currentQuestion
            : questions[0];
        inlineRef.current = nextQuestion?.id || "";
        setInlineId(inlineRef.current);
        touchedQuestions.current = new Set(
          [...touchedQuestions.current].filter((id) =>
            items.some((item) => item.id === id),
          ),
        );
        setPageRevision((value) => value + 1);
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
    const questions = pageRef.current.interactions
      .filter(
        (item) => item.question_count > 0 && item.thread_id === activeThreadId,
      )
      .sort((left, right) => right.created_at.localeCompare(left.created_at));
    const current = questions.find((item) => item.id === inlineRef.current);
    inlineRef.current =
      current?.id ||
      questions.find((item) => touchedQuestions.current.has(item.id))?.id ||
      questions[0]?.id ||
      "";
    setInlineId(inlineRef.current);
  }, [activeThreadId]);
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
  const expireInline = useCallback((id: string) => {
    const current = pageRef.current;
    if (!current.interactions.some((item) => item.id === id)) return;
    const interactions = current.interactions.filter((item) => item.id !== id);
    const next = {
      ...current,
      interactions,
      total: Math.max(0, current.total - 1),
    };
    pageRef.current = next;
    setPage(next);
    touchedQuestions.current.delete(id);
    if (inlineRef.current === id) {
      inlineRef.current =
        interactions.find(
          (item) =>
            item.question_count > 0 &&
            item.thread_id === activeThreadRef.current,
        )?.id || "";
      setInlineId(inlineRef.current);
    }
  }, []);
  const selectInteraction = (item: PendingInteraction) => {
    if (item.question_count > 0 && item.thread_id) {
      touchedQuestions.current.add(item.id);
      inlineRef.current = item.id;
      setInlineId(item.id);
      setOpen(false);
      clearSelection();
      onNavigate(item.thread_id);
      return;
    }
    selectedRef.current = item;
    setSelected(item);
    setDetail(null);
    setDetailError("");
    setDetailUnverified(false);
    void loadDetail(item);
  };
  const openPending = () => {
    setOpen(true);
    const current = pageRef.current;
    if (current.total === 1 && current.interactions.length === 1)
      selectInteraction(current.interactions[0]);
    else {
      setOpen(true);
      clearSelection();
    }
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
  const questionCount = page.interactions.reduce(
    (count, item) => count + item.question_count,
    0,
  );
  const approvalCount = page.interactions.filter((item) =>
    item.method.toLowerCase().includes("approval"),
  ).length;
  const otherCount = page.interactions.filter(
    (item) =>
      !item.question_count && !item.method.toLowerCase().includes("approval"),
  ).length;
  const completeCounts =
    !page.has_more && page.interactions.length === page.total;
  const counts = [
    questionCount ? `${questionCount} 个问题` : "",
    approvalCount ? `${approvalCount} 项审批` : "",
    otherCount ? `${otherCount} 项交互` : "",
  ].filter(Boolean);
  const nonBlockingQuestions =
    completeCounts &&
    page.interactions.every(
      (item) => item.question_count > 0 && !item.blocking,
    );
  const banner =
    page.total > 0 ? (
      <section
        className={`interaction-attention-banner${loadError ? " has-stale-data" : ""}`}
        aria-label="待处理提醒"
      >
        <Bell size={19} aria-hidden="true" />
        <div className="interaction-attention-banner-copy">
          <strong>
            {completeCounts && counts.length
              ? `你有 ${counts.join("、")}等待处理`
              : `你有 ${page.total} 项问题或审批等待处理`}
          </strong>
          <p>
            {loadError
              ? "提醒暂时无法更新，仍保留上次的待处理记录。"
              : nonBlockingQuestions
                ? "问题已保留，请进入对应会话回答。"
                : "进入对应会话回答问题，或查看待处理审批。"}
          </p>
        </div>
        <button
          type="button"
          className="button button-small interaction-attention-banner-action"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={openPending}
        >
          查看待处理
        </button>
      </section>
    ) : null;

  const questionContent = (
    <>
      {page.interactions
        .filter((item) => item.question_count > 0)
        .map((item) => (
          <CurrentQuestion
            key={item.id}
            metadata={item}
            active={
              Boolean(activeThreadId) &&
              item.thread_id === activeThreadId &&
              inlineId === item.id
            }
            revision={pageRevision}
            hostOnline={
              hosts.find((host) => host.id === item.host_id)?.online === true
            }
            stale={Boolean(loadError)}
            onDraftChange={() => touchedQuestions.current.add(item.id)}
            onExpired={expireInline}
            onRefresh={() => void loadPage()}
            onCommand={(command) => {
              void loadPage();
              onCommand(command);
            }}
          />
        ))}
    </>
  );

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
      {bannerTarget === undefined
        ? banner
        : bannerTarget && createPortal(banner, bannerTarget)}
      {questionTarget === undefined
        ? questionContent
        : questionTarget && createPortal(questionContent, questionTarget)}
      {open && (
        <Modal
          title="待处理交互"
          subtitle={`共 ${page.total} 项，选择问题进入对应会话回答，也可在这里处理审批。`}
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
                  ([
                    "pending",
                    "open",
                    "waiting",
                    "answering",
                    "answer_unknown",
                  ].includes(detail.status) ? (
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
                        onClick={() => selectInteraction(item)}
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
