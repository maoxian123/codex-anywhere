import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Bot,
  CircleHelp,
  CirclePlus,
  Code2,
  ExternalLink,
  Folder,
  Globe2,
  LogOut,
  Menu,
  Monitor,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Terminal,
  WifiOff,
  X,
} from "lucide-react";
import { api, errorText, post, setSession } from "./api";
import { ErrorMessage, Modal, Spinner } from "./components";
import Composer from "./Composer";
import {
  Login,
  NewTaskDialog,
  PairingDialog,
  useInitialSession,
} from "./Dialogs";
import Interactions from "./Interactions";
import Timeline from "./Timeline";
import { commandFailure, eventFailure } from "./runtime-errors";
import type {
  Command,
  Host,
  HubEvent,
  MessagePayload,
  Session,
  StreamItem,
  Thread,
  ThreadDetail,
} from "./types";
import {
  busyStatus,
  fileName,
  object,
  relativeDate,
  statusLabels,
  string,
} from "./utils";

function readRoute() {
  const match = window.location.pathname.match(/^\/tasks\/([^/]+)\/?$/);
  return {
    id: match ? decodeURIComponent(match[1]) : "",
    item: new URLSearchParams(window.location.search).get("item") || undefined,
  };
}
function desktopName(platform: string) {
  const lower = platform.toLowerCase();
  return lower.includes("win")
    ? "Windows"
    : lower.includes("darwin") || lower.includes("mac")
      ? "macOS"
      : lower.includes("linux")
        ? "Linux"
        : platform || "工作电脑";
}

export default function App() {
  const initial = useInitialSession();
  if (!initial.session)
    return (
      <main className="bootstrap-screen">
        <img src="/icons/codex-anywhere.svg" alt="Codex Anywhere" />
        {initial.error ? (
          <>
            <ErrorMessage>{initial.error}</ErrorMessage>
            <button className="button" onClick={initial.retry}>
              <RefreshCw size={15} />
              重新连接
            </button>
          </>
        ) : (
          <Spinner label="连接工作台" />
        )}
      </main>
    );
  if (!initial.session.authenticated)
    return <Login onLogin={initial.updateSession} />;
  return (
    <Workspace session={initial.session} onSession={initial.updateSession} />
  );
}

function Workspace({
  session,
  onSession,
}: {
  session: Session;
  onSession: (session: Session) => void;
}) {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [search, setSearch] = useState("");
  const [route, setRoute] = useState(readRoute);
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dialog, setDialog] = useState<"pair" | "new" | "about" | null>(null);
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState("");
  const [threadError, setThreadError] = useState("");
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "reconnecting"
  >("connecting");
  const [online, setOnline] = useState(navigator.onLine);
  const [streams, setStreams] = useState<Record<string, StreamItem>>({});
  const selectedRef = useRef(route.id);
  const searchRef = useRef(search);
  const threadFetchRef = useRef(0);
  const liveRef = useRef(true);
  const commandWatches = useRef(new Set<string>());
  const eventRef = useRef<(event: HubEvent) => void>(() => {});
  const refreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const completedStreams = useRef(new Set<string>());
  const cursorKey = `anywhere:cursor:${session.username || "admin"}`;
  const lastCursor = useRef(Number(sessionStorage.getItem(cursorKey) || 0));
  selectedRef.current = route.id;
  searchRef.current = search;

  const navigate = useCallback((id: string, item?: string) => {
    const url = id
      ? `/tasks/${encodeURIComponent(id)}${item ? `?item=${encodeURIComponent(item)}` : ""}`
      : "/";
    if (window.location.pathname + window.location.search !== url)
      window.history.pushState({}, "", url);
    setRoute({ id, item });
    setSidebarOpen(false);
  }, []);
  useEffect(() => {
    const listener = () => {
      setRoute(readRoute());
      setSidebarOpen(false);
    };
    window.addEventListener("popstate", listener);
    return () => window.removeEventListener("popstate", listener);
  }, []);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.key === "Escape") setSidebarOpen(false);
      if (
        target?.isContentEditable ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(target?.tagName || "") ||
        document.querySelector("dialog[open]")
      )
        return;
      if (
        event.key.toLowerCase() === "n" &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey
      ) {
        event.preventDefault();
        setDialog("new");
      }
    };
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, []);
  useEffect(() => {
    liveRef.current = true;
    const onOnline = () => setOnline(navigator.onLine);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOnline);
    return () => {
      liveRef.current = false;
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOnline);
      if (refreshRef.current) clearTimeout(refreshRef.current);
      if (listRefreshRef.current) clearTimeout(listRefreshRef.current);
    };
  }, []);

  const refreshList = useCallback(async () => {
    try {
      const query = searchRef.current;
      const [hostData, threadData] = await Promise.all([
        api<{ hosts: Host[] }>("/hosts"),
        api<{ threads: Thread[] }>(`/threads?q=${encodeURIComponent(query)}`),
      ]);
      if (liveRef.current && query === searchRef.current) {
        setHosts(hostData.hosts);
        setThreads(threadData.threads);
        setNetworkError("");
      }
    } catch (reason) {
      if (liveRef.current) setNetworkError(errorText(reason));
    } finally {
      if (liveRef.current) setLoading(false);
    }
  }, []);
  const refreshThread = useCallback(async (id = selectedRef.current) => {
    if (!id) return;
    const request = ++threadFetchRef.current;
    try {
      const value = await api<ThreadDetail>(
        `/threads/${encodeURIComponent(id)}`,
      );
      if (
        liveRef.current &&
        id === selectedRef.current &&
        request === threadFetchRef.current
      ) {
        setDetail(value);
        setThreadError("");
        setStreams((current) =>
          Object.fromEntries(
            Object.entries(current).filter(
              ([key]) => !completedStreams.current.has(key),
            ),
          ),
        );
        completedStreams.current.clear();
      }
    } catch (reason) {
      if (
        liveRef.current &&
        id === selectedRef.current &&
        request === threadFetchRef.current
      )
        setThreadError(errorText(reason));
    } finally {
      if (
        liveRef.current &&
        id === selectedRef.current &&
        request === threadFetchRef.current
      )
        setLoadingThread(false);
    }
  }, []);
  const scheduleRefresh = useCallback(
    (listOnly = false) => {
      if (listOnly) {
        if (!listRefreshRef.current)
          listRefreshRef.current = setTimeout(() => {
            listRefreshRef.current = null;
            void refreshList();
          }, 450);
      } else if (!refreshRef.current)
        refreshRef.current = setTimeout(() => {
          refreshRef.current = null;
          void refreshThread();
        }, 160);
    },
    [refreshList, refreshThread],
  );
  useEffect(() => {
    const timer = setTimeout(() => void refreshList(), search ? 220 : 0);
    return () => clearTimeout(timer);
  }, [search, refreshList]);
  useEffect(() => {
    setDetail(null);
    setStreams({});
    setThreadError("");
    setError("");
    completedStreams.current.clear();
    if (route.id) {
      setLoadingThread(true);
      void refreshThread(route.id);
    } else setLoadingThread(false);
  }, [route.id, refreshThread]);

  eventRef.current = (event) => {
    const method = event.method;
    const current = event.thread_id === selectedRef.current;
    if (
      current &&
      (method.endsWith("/delta") || method.endsWith("/outputDelta"))
    ) {
      // The hub persists accumulated deltas. Reconcile against that projection
      // to avoid duplicating tokens when a snapshot overlaps a replay stream.
      scheduleRefresh();
      return;
    }
    if (current && method === "item/completed") {
      const item = object(event.params.item);
      const id = string(item.id || event.params.itemId);
      if (id) completedStreams.current.add(id);
    }
    if (current && method === "turn/completed") {
      setStreams({});
    }
    if (current) scheduleRefresh();
    if (!method.includes("/delta") && !method.endsWith("/outputDelta"))
      scheduleRefresh(true);
    const failure = eventFailure(event);
    if (failure && (current || method === "anywhere/command_completed"))
      setError(failure);
  };

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelay = 800;
    const connect = () => {
      if (stopped) return;
      setConnection(lastCursor.current ? "reconnecting" : "connecting");
      const url = new URL("/ws/client", window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(url);
      socket.onmessage = (message) => {
        let value: unknown;
        try {
          value = JSON.parse(String(message.data));
        } catch {
          return;
        }
        const event = object(value);
        if (event.type === "ready") {
          socket?.send(
            JSON.stringify({ type: "subscribe", after: lastCursor.current }),
          );
          setConnection("connected");
          retryDelay = 800;
          void refreshList();
          void refreshThread();
        } else if (event.type === "event" && typeof event.id === "number") {
          if (event.id <= lastCursor.current) return;
          eventRef.current(value as HubEvent);
          lastCursor.current = event.id;
          sessionStorage.setItem(cursorKey, String(event.id));
        }
      };
      socket.onclose = () => {
        if (!stopped) {
          setConnection("reconnecting");
          retryTimer = setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 1.8, 15000);
        }
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    const timer = setInterval(() => {
      void refreshList();
      if (selectedRef.current) void refreshThread();
    }, 30000);
    return () => {
      stopped = true;
      clearInterval(timer);
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [cursorKey, refreshList, refreshThread]);

  const watchCommand = useCallback(
    (command: Command) => {
      if (commandWatches.current.has(command.id)) return;
      commandWatches.current.add(command.id);
      const poll = async (attempt = 0) => {
        if (!liveRef.current) {
          commandWatches.current.delete(command.id);
          return;
        }
        try {
          const value = (
            await api<{ command: Command }>(`/commands/${command.id}`)
          ).command;
          if (!liveRef.current) return;
          if (
            [
              "completed",
              "succeeded",
              "success",
              "failed",
              "error",
              "unknown",
            ].includes(value.status)
          ) {
            const failure = commandFailure(value);
            if (failure) setError(failure);
            scheduleRefresh();
            scheduleRefresh(true);
            commandWatches.current.delete(command.id);
            return;
          }
        } catch (reason) {
          if (attempt >= 10) {
            setError(errorText(reason));
            commandWatches.current.delete(command.id);
            return;
          }
        }
        if (attempt < 45)
          setTimeout(() => void poll(attempt + 1), attempt < 4 ? 700 : 1800);
        else {
          commandWatches.current.delete(command.id);
          scheduleRefresh();
          scheduleRefresh(true);
        }
      };
      setTimeout(() => void poll(), 350);
    },
    [scheduleRefresh],
  );

  const sendMessage = async (payload: MessagePayload, steer: boolean) => {
    const result = await post<{ command: Command }>(
      `/threads/${route.id}/${steer ? "steer" : "messages"}`,
      payload,
    );
    watchCommand(result.command);
    setError("");
    scheduleRefresh();
    scheduleRefresh(true);
  };
  const interrupt = async () => {
    const result = await post<{ command: Command }>(
      `/threads/${route.id}/interrupt`,
      {},
    );
    watchCommand(result.command);
    scheduleRefresh();
  };
  const logout = async () => {
    try {
      await post("/auth/logout", {});
      const value = await api<Session>("/auth/me");
      setSession(value);
      sessionStorage.removeItem(cursorKey);
      onSession(value);
    } catch (reason) {
      setError(errorText(reason));
    }
  };
  const selectedHost = hosts.find((host) => host.id === detail?.thread.host_id);
  const onlineCount = hosts.filter((host) => host.online).length;
  const activeCount = threads.filter((thread) =>
    busyStatus(thread.status),
  ).length;

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          onClick={() => setSidebarOpen(false)}
          aria-label="关闭侧边栏"
        />
      )}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <a
          href="/"
          className="brand"
          onClick={(event) => {
            event.preventDefault();
            navigate("");
          }}
        >
          <img src="/icons/codex-anywhere.svg" alt="" />
          <span>
            codex<span className="brand-light">anywhere</span>
          </span>
          <span className="version-tag">BETA</span>
        </a>
        <div className="workspace-selector">
          <span className="workspace-icon">
            <Code2 size={17} />
          </span>
          <span>
            <strong>我的工作空间</strong>
            <small>{session.username}</small>
          </span>
          <button
            className="icon-button"
            onClick={() => setDialog("about")}
            aria-label="工作空间信息"
          >
            <Settings2 size={15} />
          </button>
        </div>
        <button
          className="button new-task-button"
          onClick={() => setDialog("new")}
        >
          <Plus size={17} />
          新建任务<span className="keycap">N</span>
        </button>
        <div className="search-box">
          <Search size={15} />
          <input
            placeholder="搜索任务…"
            aria-label="搜索任务"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          {search && (
            <button
              className="icon-button"
              onClick={() => setSearch("")}
              aria-label="清除搜索"
            >
              <X size={13} />
            </button>
          )}
        </div>
        <div className="sidebar-section-label">
          <span>任务</span>
          <span className="count-badge">{threads.length}</span>
          <button
            className="icon-button"
            title="刷新列表"
            aria-label="刷新任务列表"
            onClick={() => void refreshList()}
          >
            <RefreshCw size={13} />
          </button>
        </div>
        <nav className="thread-list" aria-label="任务列表">
          {loading ? (
            <div className="sidebar-empty">
              <Spinner label="加载任务" />
            </div>
          ) : threads.length ? (
            threads.map((thread) => (
              <a
                className={`thread-row ${route.id === thread.id ? "selected" : ""}`}
                key={thread.id}
                href={`/tasks/${thread.id}`}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(thread.id);
                }}
              >
                <span className={`thread-dot status-${thread.status}`} />
                <span className="thread-text">
                  <strong>{thread.title || "未命名任务"}</strong>
                  <small>
                    {fileName(thread.cwd)}
                    <span>{relativeDate(thread.updated_at)}</span>
                  </small>
                </span>
                {thread.status === "waiting" && (
                  <span className="attention-dot" title="等待处理" />
                )}
              </a>
            ))
          ) : (
            <div className="sidebar-empty">
              <Folder size={24} />
              <p>{search ? "没有匹配的任务" : "你的任务会出现在这里"}</p>
            </div>
          )}
        </nav>
        <div className="sidebar-devices">
          <div className="sidebar-section-label">
            <span>工作设备</span>
            <span className="count-badge">{onlineCount} 在线</span>
            <button
              className="icon-button"
              title="添加设备"
              aria-label="添加设备"
              onClick={() => setDialog("pair")}
            >
              <Plus size={14} />
            </button>
          </div>
          {hosts.length ? (
            hosts.map((host) => (
              <div className="device-row" key={host.id}>
                <Monitor size={16} />
                <span>
                  <strong>{host.name}</strong>
                  <small>{desktopName(host.platform)}</small>
                </span>
                <span
                  className={`device-status ${host.online ? "online" : ""}`}
                  title={host.online ? "在线" : "离线"}
                />
                <span className="sr-only">{host.online ? "在线" : "离线"}</span>
              </div>
            ))
          ) : (
            <button
              className="connect-device"
              onClick={() => setDialog("pair")}
            >
              <CirclePlus size={16} />
              连接你的第一台设备
              <ArrowRight size={13} />
            </button>
          )}
        </div>
        <footer className="sidebar-footer">
          <button className="text-button" onClick={() => setDialog("about")}>
            <CircleHelp size={15} />
            使用说明
          </button>
          <button
            className="icon-button"
            onClick={() => void logout()}
            title="退出登录"
            aria-label="退出登录"
          >
            <LogOut size={15} />
          </button>
        </footer>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-menu"
              onClick={() => setSidebarOpen(true)}
              aria-label="打开导航"
            >
              <Menu size={20} />
            </button>
            <span className="breadcrumb">工作空间</span>
            <span className="breadcrumb-divider">/</span>
            <span className="topbar-title">
              {detail?.thread.title || (route.id ? "加载任务" : "概览")}
            </span>
          </div>
          <div className="topbar-right">
            <span
              className={`connection-status ${connection === "connected" && online ? "live" : ""}`}
            >
              <span />
              {!online
                ? "网络离线"
                : connection === "connected"
                  ? "实时连接"
                  : "正在重连"}
            </span>
            <button
              className="icon-button"
              aria-label="更多信息"
              onClick={() => setDialog("about")}
            >
              <MoreHorizontal size={19} />
            </button>
          </div>
        </header>
        {networkError && (
          <div className="global-error">
            <ErrorMessage>{networkError}</ErrorMessage>
            <button
              className="icon-button"
              onClick={() => setNetworkError("")}
              aria-label="关闭网络提示"
            >
              <X size={15} />
            </button>
          </div>
        )}
        {error && error !== detail?.thread.last_error && (
          <div className="global-error">
            <ErrorMessage>{error}</ErrorMessage>
            <button
              className="icon-button"
              onClick={() => setError("")}
              aria-label="关闭提示"
            >
              <X size={15} />
            </button>
          </div>
        )}
        {!online && (
          <div className="offline-banner">
            <WifiOff size={14} />
            网络已断开，设备上的任务仍会继续运行。恢复连接后将同步最新记录。
          </div>
        )}
        {route.id ? (
          <>
            {detail && (
              <div className="task-header">
                <div className="task-heading">
                  <h1>{detail.thread.title || "未命名任务"}</h1>
                  <span
                    className={`status-pill status-${detail.thread.status}`}
                  >
                    <span />
                    {statusLabels[detail.thread.status] || detail.thread.status}
                  </span>
                </div>
                <div className="task-metadata">
                  <span>
                    <Monitor size={13} />
                    {selectedHost?.name || "工作设备"}
                    <i
                      className={`mini-status ${selectedHost?.online ? "online" : ""}`}
                    />
                  </span>
                  <span title={detail.thread.cwd}>
                    <Folder size={13} />
                    {detail.thread.cwd}
                  </span>
                  <span className="permission-label">
                    <ShieldCheck size={13} />
                    按需审批
                  </span>
                </div>
              </div>
            )}
            {detail?.thread.last_error && (
              <div className="thread-error">
                <ErrorMessage>{detail.thread.last_error}</ErrorMessage>
              </div>
            )}
            {threadError && (
              <div className="thread-error">
                <ErrorMessage>{threadError}</ErrorMessage>
                <button
                  className="button button-small"
                  onClick={() => {
                    setLoadingThread(true);
                    void refreshThread();
                  }}
                >
                  <RefreshCw size={14} />
                  重试
                </button>
              </div>
            )}
            <Timeline
              key={route.id}
              items={detail?.items || []}
              streams={streams}
              busy={busyStatus(detail?.thread.status)}
              loading={loadingThread}
              targetItem={route.item}
              onImage={(src, alt) => setLightbox({ src, alt })}
              onNavigate={navigate}
            />
            {detail && (
              <>
                <Interactions
                  interactions={detail.interactions}
                  onCommand={(command) => {
                    watchCommand(command);
                    scheduleRefresh();
                  }}
                />
                <Composer
                  key={detail.thread.id}
                  thread={detail.thread}
                  hostOnline={!!selectedHost?.online}
                  onSend={sendMessage}
                  onInterrupt={interrupt}
                  onNavigate={navigate}
                />
              </>
            )}
          </>
        ) : (
          <div className="overview-scroll">
            <div className="overview">
              <div className="overview-topline">
                <span className="eyebrow">CODEX, CONNECTED.</span>
                <span className="overview-date">
                  {new Date().toLocaleDateString("zh-CN", {
                    month: "long",
                    day: "numeric",
                    weekday: "long",
                  })}
                </span>
              </div>
              <div className="overview-hero">
                <span className="hero-symbol">
                  <Terminal size={34} strokeWidth={1.6} />
                </span>
                <h1>灵感随行，工作继续。</h1>
                <p>
                  把想法交给 Codex。连接工作电脑，
                  <br className="mobile-break" />
                  在这里查看进展、回应审批、继续对话。
                </p>
                <div className="hero-actions">
                  <button
                    className="button button-primary"
                    onClick={() => setDialog(hosts.length ? "new" : "pair")}
                  >
                    <Plus size={16} />
                    {hosts.length ? "开始新任务" : "连接工作设备"}
                  </button>
                  {hosts.length > 0 && (
                    <button
                      className="button button-quiet"
                      onClick={() => setDialog("pair")}
                    >
                      <Monitor size={15} />
                      连接设备
                    </button>
                  )}
                </div>
              </div>
              <div className="overview-stats">
                <div>
                  <span className="stat-icon">
                    <Monitor size={19} />
                  </span>
                  <span>
                    <strong>
                      {onlineCount}
                      <small> / {hosts.length}</small>
                    </strong>
                    <p>设备在线</p>
                  </span>
                  <span className={`stat-dot ${onlineCount ? "online" : ""}`} />
                </div>
                <div>
                  <span className="stat-icon">
                    <Terminal size={19} />
                  </span>
                  <span>
                    <strong>{activeCount}</strong>
                    <p>任务运行中</p>
                  </span>
                </div>
                <div>
                  <span className="stat-icon">
                    <Globe2 size={19} />
                  </span>
                  <span>
                    <strong>跨设备</strong>
                    <p>执行在本机，随处可连接</p>
                  </span>
                </div>
              </div>
              <div className="section-heading">
                <h2>最近任务</h2>
                <button
                  className="text-button"
                  onClick={() => setDialog("new")}
                >
                  <Plus size={14} />
                  新建任务
                </button>
              </div>
              <div className="recent-tasks">
                {loading ? (
                  <div className="overview-empty">
                    <Spinner label="加载工作空间" />
                  </div>
                ) : threads.length ? (
                  threads.slice(0, 5).map((thread) => (
                    <a
                      className="recent-task"
                      key={thread.id}
                      href={`/tasks/${thread.id}`}
                      onClick={(event) => {
                        event.preventDefault();
                        navigate(thread.id);
                      }}
                    >
                      <span className="recent-task-icon">
                        <Bot size={18} />
                      </span>
                      <span className="recent-task-main">
                        <strong>{thread.title || "未命名任务"}</strong>
                        <small>
                          <Folder size={12} />
                          {fileName(thread.cwd)}
                          <span>·</span>
                          {hosts.find((host) => host.id === thread.host_id)
                            ?.name || "工作设备"}
                        </small>
                      </span>
                      <span className={`status-pill status-${thread.status}`}>
                        <span />
                        {statusLabels[thread.status] || thread.status}
                      </span>
                      <time>{relativeDate(thread.updated_at)}</time>
                      <ArrowRight size={15} />
                    </a>
                  ))
                ) : (
                  <div className="overview-empty">
                    <span className="empty-icon">
                      <Folder size={25} />
                    </span>
                    <h3>还没有任务</h3>
                    <p>
                      {hosts.length
                        ? "创建第一个任务，让 Codex 开始工作。"
                        : "先连接工作电脑，再开始你的第一个任务。"}
                    </p>
                    <button
                      className="text-button"
                      onClick={() => setDialog(hosts.length ? "new" : "pair")}
                    >
                      {hosts.length ? "创建任务" : "连接设备"}
                      <ArrowRight size={14} />
                    </button>
                  </div>
                )}
              </div>
              <div className="overview-note">
                <ShieldCheck size={15} />
                <span>
                  项目与 Codex
                  登录信息保留在你的设备上。任务执行时会按设置请求审批。
                </span>
              </div>
            </div>
          </div>
        )}
      </main>
      {dialog === "pair" && (
        <PairingDialog
          onClose={() => {
            setDialog(null);
            void refreshList();
          }}
        />
      )}
      {dialog === "new" && (
        <NewTaskDialog
          hosts={hosts}
          onClose={() => setDialog(null)}
          onCreated={(thread, command) => {
            setThreads((current) => [
              thread,
              ...current.filter((entry) => entry.id !== thread.id),
            ]);
            navigate(thread.id);
            watchCommand(command);
          }}
        />
      )}
      {dialog === "about" && (
        <Modal
          title="Codex Anywhere"
          subtitle="你的 Codex 工作台，随处连接，持续运行。"
          onClose={() => setDialog(null)}
        >
          <div className="about-content">
            <img src="/icons/codex-anywhere.svg" alt="" />
            <p>
              每台工作电脑运行 Python
              节点，与管理中心保持连接。网页负责交互，Codex
              在所选电脑的工作目录中执行。
            </p>
            <dl>
              <div>
                <dt>设备</dt>
                <dd>添加设备后，用一次性配对码连接节点。</dd>
              </div>
              <div>
                <dt>任务引用</dt>
                <dd>输入 @ 或点击引用按钮，选择另一个任务或具体消息。</dd>
              </div>
              <div>
                <dt>网页与图片</dt>
                <dd>添加网页地址和摘录；支持上传、粘贴和拖入图片。</dd>
              </div>
              <div>
                <dt>持续执行</dt>
                <dd>关闭网页不影响运行。离线时会在恢复后同步已有事件。</dd>
              </div>
            </dl>
            <p className="field-hint">
              v0.1 · Python / Flask · Codex App Server
            </p>
          </div>
        </Modal>
      )}
      {lightbox && (
        <Modal title={lightbox.alt} onClose={() => setLightbox(null)} wide>
          <div className="lightbox">
            <img src={lightbox.src} alt={lightbox.alt} />
            <a
              className="button button-small"
              href={lightbox.src}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={14} />
              打开原图
            </a>
          </div>
        </Modal>
      )}
    </div>
  );
}
