import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  ExternalLink,
  Folder,
  Menu,
  Monitor,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
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
import { ProjectDialog } from "./Projects";
import { ImportDialog } from "./Imports";
import ProjectNavigation from "./ProjectNavigation";
import SidebarMenu from "./SidebarMenu";
import Timeline from "./Timeline";
import { commandFailure, eventFailure } from "./runtime-errors";
import {
  latestHistory,
  olderHistory,
  withHistory,
  type HistoryCache,
} from "./history";
import { useAutomaticHistory } from "./useAutomaticHistory";
import { waitForCommand, waitForHistoryPage } from "./commands";
import { ProjectSettingsDialog } from "./ExecutionControls";
import {
  applySettingsUpdate,
  permissionLabel,
  preserveNewerSettings,
} from "./execution-settings";
import type {
  Command,
  Host,
  HubEvent,
  MessagePayload,
  Session,
  StreamItem,
  Thread,
  ThreadDetail,
  Project,
  ThreadPage,
  ExecutionSettings,
} from "./types";
import { busyStatus, object, statusLabels, string } from "./utils";

function readRoute() {
  const match = window.location.pathname.match(/^\/tasks\/([^/]+)\/?$/);
  return {
    id: match ? decodeURIComponent(match[1]) : "",
    item: new URLSearchParams(window.location.search).get("item") || undefined,
  };
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
  const [projects, setProjects] = useState<Project[]>([]);
  const [settingsProject, setSettingsProject] = useState<Project | null>(null);
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [listPages, setListPages] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [search, setSearch] = useState("");
  const [route, setRoute] = useState(readRoute);
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const detailRef = useRef(detail);
  detailRef.current = detail;
  const hostsRef = useRef(hosts);
  hostsRef.current = hosts;
  const [loading, setLoading] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dialog, setDialog] = useState<
    "pair" | "new" | "about" | "project" | "import" | null
  >(null);
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [networkError, setNetworkError] = useState("");
  const [threadError, setThreadError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [olderError, setOlderError] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "reconnecting"
  >("connecting");
  const [online, setOnline] = useState(navigator.onLine);
  const [streams, setStreams] = useState<Record<string, StreamItem>>({});
  const selectedRef = useRef(route.id);
  const searchRef = useRef(search);
  const listScopeRef = useRef({ projectFilter, listPages });
  const listOffsetsRef = useRef<{ key: string; offsets: number[] }>({
    key: "",
    offsets: [0],
  });
  const threadFetchRef = useRef(0);
  const historyCache = useRef<HistoryCache | null>(null);
  const olderRequest = useRef<AbortController | null>(null);
  const referencePages = useRef({ key: "", count: 0 });
  const listFetchRef = useRef(0);
  const threadRequests = useRef(
    new Map<string, { again: boolean; promise: Promise<void> }>(),
  );
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
  listScopeRef.current = { projectFilter, listPages };

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
      olderRequest.current?.abort();
    };
  }, []);

  const refreshList = useCallback(async () => {
    const request = ++listFetchRef.current;
    try {
      const query = searchRef.current;
      const scope = listScopeRef.current;
      const scopeKey = JSON.stringify([query, scope.projectFilter]);
      const offsets = Array.from({ length: scope.listPages }, (_, index) =>
        listOffsetsRef.current.key === scopeKey
          ? (listOffsetsRef.current.offsets[index] ?? index * 50)
          : index * 50,
      );
      const [hostData, projectData, ...threadPages] = await Promise.all([
        api<{ hosts: Host[] }>("/hosts"),
        api<{ projects: Project[] }>("/projects"),
        ...Array.from({ length: scope.listPages }, (_, index) =>
          api<ThreadPage>(
            `/threads?q=${encodeURIComponent(query)}&limit=50&offset=${offsets[index]}${scope.projectFilter ? `&project_id=${encodeURIComponent(scope.projectFilter)}` : ""}`,
          ),
        ),
      ]);
      if (
        liveRef.current &&
        request === listFetchRef.current &&
        query === searchRef.current &&
        scope.projectFilter === listScopeRef.current.projectFilter &&
        scope.listPages === listScopeRef.current.listPages
      ) {
        setHosts(hostData.hosts);
        setProjects(projectData.projects);
        setThreads([
          ...new Map(
            threadPages
              .flatMap((page) => page.threads)
              .map((thread) => [thread.id, thread]),
          ).values(),
        ]);
        setHasMore(Boolean(threadPages.at(-1)?.has_more));
        listOffsetsRef.current = {
          key: scopeKey,
          offsets: [
            0,
            ...threadPages.map((page, index) =>
              typeof page.next_offset === "number"
                ? page.next_offset
                : offsets[index] + page.threads.length,
            ),
          ],
        };
        setNetworkError("");
      }
    } catch (reason) {
      if (liveRef.current && request === listFetchRef.current)
        setNetworkError(errorText(reason));
    } finally {
      if (liveRef.current && request === listFetchRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);
  const refreshThread = useCallback(async (id = selectedRef.current) => {
    if (!id) return;
    const existing = threadRequests.current.get(id);
    if (existing) {
      existing.again = true;
      return existing.promise;
    }
    const entry = { again: false, promise: Promise.resolve() };
    threadRequests.current.set(id, entry);
    entry.promise = (async () => {
      do {
        entry.again = false;
        const request = ++threadFetchRef.current;
        try {
          const value = await api<ThreadDetail>(
            `/threads/${encodeURIComponent(id)}?item_limit=100`,
          );
          if (
            liveRef.current &&
            id === selectedRef.current &&
            request === threadFetchRef.current
          ) {
            const cache = latestHistory(historyCache.current, value);
            historyCache.current = cache;
            setDetail((current) =>
              withHistory(
                {
                  ...value,
                  thread: preserveNewerSettings(value.thread, current?.thread),
                },
                cache,
              ),
            );
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
      } while (entry.again && liveRef.current && id === selectedRef.current);
    })().finally(() => {
      threadRequests.current.delete(id);
    });
    return entry.promise;
  }, []);
  const loadOlder = useCallback(
    async (retry = false) => {
      const cache = historyCache.current;
      const id = selectedRef.current;
      const thread = detailRef.current?.thread;
      const nativeMore =
        thread?.id === id &&
        thread.history_complete === false &&
        !!thread.history_cursor;
      if (
        !cache ||
        cache.id !== id ||
        ((!cache.hasMore || cache.before === null) && !nativeMore) ||
        olderRequest.current
      )
        return;
      const controller = new AbortController();
      olderRequest.current = controller;
      setLoadingOlder(true);
      setOlderError("");
      try {
        const nativePage = !cache.hasMore && nativeMore;
        if (nativePage && thread?.history_cursor) {
          if (
            !hostsRef.current.find((host) => host.id === thread.host_id)?.online
          )
            throw new Error("设备离线，连接后可加载更早的本机历史。");
          if (
            ["active", "waiting", "starting", "importing"].includes(
              thread.status,
            )
          )
            throw new Error("当前会话正在执行或同步，请稍后加载更早历史。");
          const result = await api<{ thread: Thread; command: Command | null }>(
            `/threads/${encodeURIComponent(id)}/history`,
            {
              method: "POST",
              body: JSON.stringify({
                cursor: thread.history_cursor,
                limit: 50,
                ...(retry ? { retry: true } : {}),
              }),
              signal: controller.signal,
            },
          );
          if (result.command)
            await waitForCommand(result.command, controller.signal);
          const projected = await waitForHistoryPage(
            id,
            thread.history_cursor,
            thread.history_generation,
            controller.signal,
          );
          if (controller.signal.aborted || selectedRef.current !== id) return;
          if (projected.history_generation !== thread.history_generation) {
            await refreshThread(id);
            return;
          }
        }
        const before = nativePage ? cache.items[0]?.position : cache.before;
        const page = await api<ThreadDetail>(
          `/threads/${encodeURIComponent(id)}?item_limit=100${typeof before === "number" ? `&before_position=${before}` : ""}`,
          { signal: controller.signal },
        );
        if (
          controller.signal.aborted ||
          selectedRef.current !== id ||
          !liveRef.current
        )
          return;
        const current = historyCache.current;
        if (!current || current.id !== id || current.version !== cache.version)
          return;
        const merged = olderHistory(current, page);
        if (!merged) {
          await refreshThread(id);
          return;
        }
        historyCache.current = merged;
        setDetail((previous) =>
          previous?.thread.id === id
            ? withHistory(
                {
                  ...previous,
                  thread: preserveNewerSettings(page.thread, previous.thread),
                },
                merged,
              )
            : previous,
        );
      } catch (reason) {
        if (
          !controller.signal.aborted &&
          selectedRef.current === id &&
          liveRef.current
        )
          setOlderError(errorText(reason));
      } finally {
        if (olderRequest.current === controller) {
          olderRequest.current = null;
          if (liveRef.current && selectedRef.current === id)
            setLoadingOlder(false);
        }
      }
    },
    [refreshThread],
  );
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
  }, [search, projectFilter, listPages, refreshList]);
  useEffect(() => {
    setListPages(1);
    setHasMore(false);
  }, [search, projectFilter]);
  useEffect(() => {
    setDetail(null);
    setStreams({});
    setThreadError("");
    setError("");
    historyCache.current = null;
    olderRequest.current?.abort();
    olderRequest.current = null;
    setLoadingOlder(false);
    setOlderError("");
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
      method === "anywhere/catalog_sync" &&
      detail?.thread.native_id &&
      Array.isArray(event.params.threads) &&
      event.params.threads.some(
        (entry) => object(entry).id === detail.thread.native_id,
      )
    )
      scheduleRefresh();
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
    if (failure && current) setError(failure);
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
    (command: Command, threadId = selectedRef.current) => {
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
            if (failure && threadId === selectedRef.current) setError(failure);
            scheduleRefresh();
            scheduleRefresh(true);
            commandWatches.current.delete(command.id);
            return;
          }
        } catch (reason) {
          if (attempt >= 10) {
            if (liveRef.current && threadId === selectedRef.current)
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
    watchCommand(result.command, route.id);
    if (route.id === selectedRef.current) setError("");
    scheduleRefresh();
    scheduleRefresh(true);
  };
  const saveThreadSettings = async (settings: ExecutionSettings) => {
    const thread = detail?.thread;
    if (!thread) return;
    try {
      const result = await api<{ thread: Thread }>(
        `/threads/${encodeURIComponent(thread.id)}/settings`,
        {
          method: "PATCH",
          body: JSON.stringify({
            execution_settings: settings,
            ...(thread.settings_revision !== undefined
              ? { settings_revision: thread.settings_revision }
              : {}),
          }),
        },
      );
      if (selectedRef.current === thread.id)
        setDetail((current) =>
          current?.thread.id === thread.id
            ? {
                ...current,
                thread: applySettingsUpdate(current.thread, result.thread),
              }
            : current,
        );
      setThreads((current) =>
        current.map((entry) =>
          entry.id === result.thread.id
            ? applySettingsUpdate(entry, result.thread)
            : entry,
        ),
      );
    } catch (reason) {
      await refreshThread(thread.id);
      throw reason;
    }
  };
  const interrupt = async () => {
    const result = await post<{ command: Command }>(
      `/threads/${route.id}/interrupt`,
      {},
    );
    watchCommand(result.command, route.id);
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
  const historyBusy = useAutomaticHistory({
    selectionId: route.id,
    thread: detail?.thread,
    host: selectedHost,
    refresh: refreshThread,
    onError: setHistoryError,
  });
  useEffect(() => {
    const key = `${route.id}:${route.item || ""}`;
    if (referencePages.current.key !== key)
      referencePages.current = { key, count: 0 };
    if (
      !route.item ||
      !detail ||
      detail.thread.id !== route.id ||
      detail.thread.history_loaded === false ||
      historyBusy ||
      loadingOlder ||
      olderError ||
      !(
        detail.has_more_items ||
        (detail.thread.history_complete === false &&
          detail.thread.history_cursor)
      ) ||
      referencePages.current.count >= 20 ||
      detail.items.some(
        (item) => item.id === route.item || item.native_id === route.item,
      )
    )
      return;
    referencePages.current.count += 1;
    void loadOlder();
  }, [
    route.id,
    route.item,
    detail,
    historyBusy,
    loadingOlder,
    olderError,
    loadOlder,
  ]);

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
        <div className="sidebar-toolbar">
          <button
            className="new-conversation-button"
            onClick={() => setDialog("new")}
            aria-label="新对话"
          >
            <Plus size={17} />
            新对话
          </button>
          <button
            className="icon-button"
            onClick={() => setDialog("project")}
            aria-label="新建项目"
            title="新建项目"
          >
            <Folder size={16} />
          </button>
          <SidebarMenu
            hosts={hosts}
            onPair={() => setDialog("pair")}
            onHistory={() => setDialog("import")}
            onAbout={() => setDialog("about")}
            onLogout={() => void logout()}
          />
        </div>
        <div className="search-box">
          <Search size={15} />
          <input
            placeholder={projectFilter ? "搜索项目会话…" : "搜索全部会话…"}
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
        <ProjectNavigation
          hosts={hosts}
          projects={projects}
          threads={threads}
          selectedProjectId={projectFilter}
          currentThreadId={route.id}
          loading={loading}
          loadingMore={loadingMore}
          hasMore={hasMore}
          onFilter={setProjectFilter}
          onNavigate={(id) => navigate(id)}
          onNewProject={() => setDialog("project")}
          onSettings={setSettingsProject}
          onRefresh={() => void refreshList()}
          onMore={() => {
            setLoadingMore(true);
            setListPages((value) => value + 1);
          }}
        />
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
            <span className="breadcrumb">会话</span>
            <span className="breadcrumb-divider">/</span>
            <h1 className="topbar-title">
              {detail?.thread.title || (route.id ? "加载会话" : "全部会话")}
            </h1>
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
                    {detail.thread.effective_settings?.sandbox_mode ||
                    detail.thread.execution_settings?.sandbox_mode ||
                    detail.thread.effective_settings?.approval_policy ||
                    detail.thread.execution_settings?.approval_policy
                      ? permissionLabel(
                          detail.thread.effective_settings ||
                            detail.thread.execution_settings,
                        )
                      : "保留原会话设置"}
                  </span>
                </div>
              </div>
            )}
            {detail?.thread.last_error && (
              <div className="thread-error">
                <ErrorMessage>{detail.thread.last_error}</ErrorMessage>
              </div>
            )}
            {historyError && (
              <div className="thread-error">
                <ErrorMessage>{historyError}</ErrorMessage>
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
              hasMore={Boolean(
                detail?.has_more_items ||
                (detail?.thread.history_complete === false &&
                  detail.thread.history_cursor),
              )}
              loadingOlder={loadingOlder}
              olderError={olderError}
              onLoadOlder={() => void loadOlder(Boolean(olderError))}
              importing={
                historyBusy ||
                detail?.thread.status === "importing" ||
                detail?.thread.history_loaded === false
              }
              targetItem={route.item}
              onImage={(src, alt) => setLightbox({ src, alt })}
              onNavigate={navigate}
            />
            {detail && (
              <>
                <Interactions
                  interactions={detail.interactions}
                  onCommand={(command) => {
                    watchCommand(command, detail.thread.id);
                    scheduleRefresh();
                  }}
                />
                <Composer
                  key={detail.thread.id}
                  thread={
                    historyBusy
                      ? { ...detail.thread, history_loaded: false }
                      : detail.thread
                  }
                  hostOnline={!!selectedHost?.online}
                  host={selectedHost}
                  project={projects.find(
                    (project) => project.id === detail.thread.project_id,
                  )}
                  onSettingsChange={saveThreadSettings}
                  onProjectSettings={setSettingsProject}
                  onSend={sendMessage}
                  onInterrupt={interrupt}
                  onNavigate={navigate}
                />
              </>
            )}
          </>
        ) : (
          <div className="conversation-empty-state">
            <Bot size={30} />
            <h1>选择一个会话继续</h1>
            <p>
              连接设备后，最近的 Codex 会话与项目会自动出现在左侧。
              <br />
              打开会话时再加载历史，随时继续工作。
            </p>
            <div>
              <button
                className="button button-primary"
                onClick={() => setDialog(hosts.length ? "new" : "pair")}
              >
                <Plus size={15} />
                {hosts.length ? "新对话" : "连接设备"}
              </button>
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
      {settingsProject && (
        <ProjectSettingsDialog
          key={settingsProject.id}
          project={settingsProject}
          host={hosts.find((host) => host.id === settingsProject.host_id)}
          onClose={() => setSettingsProject(null)}
          onSaved={(project) =>
            setProjects((current) =>
              current.map((entry) =>
                entry.id === project.id ? project : entry,
              ),
            )
          }
        />
      )}
      {dialog === "new" && (
        <NewTaskDialog
          hosts={hosts}
          projects={projects}
          initialProjectId={projectFilter}
          onNewProject={() => setDialog("project")}
          onClose={() => setDialog(null)}
          onCreated={(thread, command) => {
            setThreads((current) => [
              thread,
              ...current.filter((entry) => entry.id !== thread.id),
            ]);
            setProjectFilter(thread.project_id || null);
            navigate(thread.id);
            watchCommand(command, thread.id);
          }}
        />
      )}
      {dialog === "project" && (
        <ProjectDialog
          hosts={hosts}
          onClose={() => setDialog(null)}
          onCreated={(project) => {
            setProjects((current) => [
              project,
              ...current.filter((value) => value.id !== project.id),
            ]);
            setProjectFilter(project.id);
            setDialog("new");
            void refreshList();
          }}
        />
      )}
      {dialog === "import" && (
        <ImportDialog
          hosts={hosts}
          onClose={() => {
            setDialog(null);
            void refreshList();
          }}
          onImported={(thread) => {
            setThreads((current) =>
              current.some((value) => value.id === thread.id)
                ? current
                : projectFilter && thread.project_id !== projectFilter
                  ? current
                  : [thread, ...current],
            );
            void refreshList();
          }}
          onNavigate={(id, projectId) => {
            setProjectFilter(projectId || null);
            navigate(id);
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
