import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  ChevronRight,
  Folder,
  Layers,
  MessageSquarePlus,
  Monitor,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Settings2,
  X,
} from "lucide-react";
import { Spinner } from "./components";
import type { Host, Project, Thread } from "./types";

export type NavigationView = "all" | "active" | "waiting" | "archived";
export type ThreadUpdate = {
  display_title?: string | null;
  pinned?: boolean;
  archived?: boolean;
};
type NavigationThread = Thread & ThreadUpdate;
type NavigationGroup = {
  id: string;
  name: string;
  cwd: string;
  project?: Project;
  threads: NavigationThread[];
};

function directoryKey(cwd: string, host?: Host) {
  const normalized =
    cwd.replace(/\\/g, "/").replace(/\/+$/, "") || (cwd ? "/" : "");
  return /^(win|windows)/i.test(host?.platform || "") || /^[a-z]:/i.test(cwd)
    ? normalized.toLowerCase()
    : normalized;
}
function directoryName(cwd: string) {
  return cwd.split(/[\\/]/).filter(Boolean).at(-1) || cwd || "未归属项目";
}
function projectGroups(
  projects: Project[],
  threads: NavigationThread[],
  host?: Host,
) {
  const groups: NavigationGroup[] = projects.map((project) => ({
    id: `project:${project.id}`,
    name: project.name,
    cwd: project.cwd,
    project,
    threads: [],
  }));
  const byProject = new Map(groups.map((group) => [group.project!.id, group]));
  const byDirectory = new Map<string, NavigationGroup>();
  for (const group of groups) {
    const key = directoryKey(group.cwd, host);
    if (key && !byDirectory.has(key)) byDirectory.set(key, group);
  }
  for (const thread of threads) {
    const key = directoryKey(thread.cwd || "", host);
    let group =
      (thread.project_id && byProject.get(thread.project_id)) ||
      byDirectory.get(key);
    if (!group) {
      group = {
        id: `directory:${key}`,
        name: directoryName(thread.cwd || ""),
        cwd: thread.cwd || "",
        threads: [],
      };
      groups.push(group);
      byDirectory.set(key, group);
    }
    group.threads.push(thread);
  }
  return groups;
}
function threadLabel(thread: NavigationThread) {
  return thread.display_title || thread.title || "未命名会话";
}
function matchesView(thread: NavigationThread, view: NavigationView) {
  if (view === "archived") return !!thread.archived;
  if (thread.archived) return false;
  if (view === "waiting") return thread.status === "waiting";
  if (view === "active")
    return thread.status === "active" || thread.status === "starting";
  return true;
}
function threadActivity(thread: NavigationThread) {
  if (thread.status === "waiting") return "待处理";
  if (thread.status === "active") return "运行中";
  if (thread.status === "starting") return "启动中";
  if (thread.status === "error" || thread.status === "failed") return "出错";
  if (thread.status === "interrupted") return "已停止";
  if (thread.status === "importing") return "加载中";
  return "";
}

function RenameThreadDialog({
  thread,
  saving,
  error,
  onSave,
  onClose,
}: {
  thread: NavigationThread;
  saving: boolean;
  error: string;
  onSave: (name: string) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(threadLabel(thread));
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    nameInput.current?.focus();
    nameInput.current?.select();
    return () => element?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="modal sidebar-rename-dialog"
      aria-label="重命名会话"
      onCancel={(event) => {
        event.preventDefault();
        if (!saving) onClose();
      }}
    >
      <header className="modal-header">
        <h2>重命名会话</h2>
        <button
          className="icon-button"
          type="button"
          aria-label="关闭窗口"
          disabled={saving}
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </header>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSave(name.trim());
        }}
      >
        <label htmlFor="sidebar-thread-name">会话名称</label>
        <input
          id="sidebar-thread-name"
          ref={nameInput}
          autoFocus
          maxLength={200}
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={saving}
        />
        <p className="muted">留空时使用原始会话标题。</p>
        {error && (
          <p className="text-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="button"
            onClick={onClose}
            disabled={saving}
          >
            取消
          </button>
          <button type="submit" className="button primary" disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export default function ProjectNavigation({
  hosts,
  projects,
  threads,
  selectedProjectId,
  currentThreadId,
  loading,
  loadingMore,
  hasMore,
  onFilter,
  onNavigate,
  onNewProject,
  onRefresh,
  onMore,
  onSettings,
  hostPages,
  onHostMore,
  onNewConversation,
  view: controlledView,
  onViewChange,
  onThreadUpdate,
}: {
  hosts: Host[];
  projects: Project[];
  threads: Thread[];
  selectedProjectId: string | null;
  currentThreadId: string;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onFilter: (id: string | null) => void;
  onNavigate: (id: string) => void;
  onNewProject: () => void;
  onRefresh: () => void;
  onMore: () => void;
  onSettings?: (project: Project) => void;
  hostPages?: Record<string, { hasMore: boolean; loading: boolean }>;
  onHostMore?: (hostId: string) => void;
  onNewConversation?: (project: Project) => void;
  view?: NavigationView;
  onViewChange?: (view: NavigationView) => void;
  onThreadUpdate?: (thread: Thread, changes: ThreadUpdate) => Promise<void>;
}) {
  const [localView, setLocalView] = useState<NavigationView>("all");
  const view = controlledView ?? localView;
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [collapsedHosts, setCollapsedHosts] = useState<Set<string>>(new Set());
  const navigationId = useId();
  const revealedThread = useRef<{ threadId: string; hostId: string } | null>(
    null,
  );
  const currentThreadHostId = threads.find(
    (thread) => thread.id === currentThreadId,
  )?.host_id;
  const [menuThread, setMenuThread] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const [renameThread, setRenameThread] = useState<NavigationThread | null>(
    null,
  );
  const [savingId, setSavingId] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState("");
  useEffect(() => {
    if (!currentThreadId) {
      revealedThread.current = null;
      return;
    }
    if (!currentThreadHostId) return;
    if (
      revealedThread.current?.threadId === currentThreadId &&
      revealedThread.current.hostId === currentThreadHostId
    )
      return;
    revealedThread.current = {
      threadId: currentThreadId,
      hostId: currentThreadHostId,
    };
    setCollapsedHosts((previous) => {
      if (!previous.has(currentThreadHostId)) return previous;
      const next = new Set(previous);
      next.delete(currentThreadHostId);
      return next;
    });
  }, [currentThreadId, currentThreadHostId]);
  useEffect(() => {
    if (!menuThread) return;
    const close = (event: MouseEvent) => {
      if (
        !(event.target as Element).closest(".thread-menu, .thread-menu-popover")
      )
        setMenuThread(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuThread(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menuThread]);
  const changeView = (next: NavigationView) => {
    setLocalView(next);
    setMenuThread(null);
    onViewChange?.(next);
  };
  const updateThread = async (
    thread: NavigationThread,
    changes: ThreadUpdate,
  ) => {
    if (!onThreadUpdate || savingId) return;
    setSavingId(thread.id);
    setUpdateError("");
    try {
      await onThreadUpdate(thread, changes);
      setMenuThread(null);
      setRenameThread(null);
    } catch (error) {
      setUpdateError(
        error instanceof Error ? error.message : "更新会话失败，请重试。",
      );
    } finally {
      setSavingId(null);
    }
  };
  const toggleGroup = (key: string) =>
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const toggleHost = (hostId: string) => {
    setMenuThread(null);
    setCollapsedHosts((previous) => {
      const next = new Set(previous);
      if (next.has(hostId)) next.delete(hostId);
      else next.add(hostId);
      return next;
    });
  };
  const selectedProject = projects.find(
    (project) => project.id === selectedProjectId,
  );
  const threadHostIds = selectedProjectId
    ? [selectedProject?.host_id || threads[0]?.host_id].filter(
        (id): id is string => Boolean(id),
      )
    : [
        ...new Set([
          ...hosts.map((host) => host.id),
          ...threads.map((thread) => thread.host_id),
          ...projects.map((project) => project.host_id),
        ]),
      ];
  const hasMatchingThreads = threads.some(
    (thread) =>
      threadHostIds.includes(thread.host_id) && matchesView(thread, view),
  );
  const viewEmptyMessage =
    view === "active"
      ? "没有正在运行的会话"
      : view === "waiting"
        ? "没有等待处理的会话"
        : "没有已归档的会话";

  return (
    <nav
      className="project-navigation grouped-navigation unified-navigation"
      aria-label="项目与会话"
    >
      <div className="navigation-views" aria-label="会话视图">
        {(
          [
            ["all", "最近"],
            ["active", "运行中"],
            ["waiting", "待处理"],
            ["archived", "已归档"],
          ] as const
        ).map(([value, label]) => (
          <button
            type="button"
            className={view === value ? "selected" : ""}
            key={value}
            aria-pressed={view === value}
            onClick={() => changeView(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="navigation-scope">
        <button
          className={`all-threads-button ${selectedProjectId === null ? "selected" : ""}`}
          onClick={() => onFilter(null)}
          title={
            selectedProjectId ? "清除项目筛选，返回全部会话" : "所有项目的会话"
          }
        >
          <Layers size={15} />
          {selectedProjectId ? "返回全部会话" : "全部会话"}
          <span>
            {selectedProjectId === null
              ? threads.filter((thread) => matchesView(thread, view)).length
              : ""}
          </span>
        </button>
        <button
          className="icon-button"
          onClick={onNewProject}
          aria-label="添加项目"
          title="新建项目并选择设备目录"
        >
          <Plus size={16} />
        </button>
        <button
          className="icon-button"
          onClick={onRefresh}
          aria-label="刷新任务列表"
        >
          <RefreshCw size={15} />
        </button>
      </div>
      {selectedProjectId && (
        <p className="navigation-filter-note">
          正在筛选：{selectedProject?.name || "所选项目"}
        </p>
      )}
      {updateError && !renameThread && (
        <p className="navigation-update-error text-error" role="alert">
          {updateError}
        </p>
      )}
      <div
        className="host-thread-groups"
        role="region"
        aria-label="项目与会话列表"
        tabIndex={0}
        onScroll={() => setMenuThread(null)}
      >
        {view !== "all" && !hasMatchingThreads && (
          <div className="sidebar-empty" role="status">
            {loading ? (
              <Spinner label="加载会话" />
            ) : (
              <>
                <p>{viewEmptyMessage}</p>
                {(hasMore ||
                  Object.values(hostPages || {}).some(
                    (page) => page.hasMore,
                  )) && <p>当前已加载的会话中暂无匹配项，可继续加载更多。</p>}
              </>
            )}
          </div>
        )}
        {threadHostIds.map((hostId) => {
          const host = hosts.find((value) => value.id === hostId);
          const hostName = host?.name || hostId;
          const hostThreads = (threads as NavigationThread[]).filter(
            (thread) => thread.host_id === hostId && matchesView(thread, view),
          );
          const hostProjects = projects.filter(
            (project) =>
              project.host_id === hostId &&
              (!selectedProjectId || project.id === selectedProjectId),
          );
          const groups = projectGroups(hostProjects, hostThreads, host).filter(
            (group) => view === "all" || group.threads.length > 0,
          );
          const perHost = selectedProjectId === null && !!hostPages;
          const page = perHost
            ? hostPages[hostId] || { hasMore: false, loading: false }
            : { hasMore, loading: loadingMore };
          if (view !== "all" && !groups.length && !page.hasMore) return null;
          const hostExpanded = !collapsedHosts.has(hostId);
          const contentId = `${navigationId}-host-${encodeURIComponent(hostId)}`;
          return (
            <section
              className={`host-thread-group${hostExpanded ? "" : " is-collapsed"}${!groups.length && !page.hasMore ? " is-empty" : ""}`}
              key={hostId}
              aria-label={`${hostName} 会话`}
              aria-busy={loading || page.loading}
            >
              <header className="host-thread-heading">
                <button
                  type="button"
                  className="icon-button host-collapse-button"
                  aria-label={`${hostExpanded ? "收起" : "展开"} ${hostName} 的会话`}
                  aria-expanded={hostExpanded}
                  aria-controls={contentId}
                  title={`${hostExpanded ? "收起" : "展开"} ${hostName} 的会话`}
                  onClick={() => toggleHost(hostId)}
                >
                  {hostExpanded ? (
                    <ChevronDown size={15} />
                  ) : (
                    <ChevronRight size={15} />
                  )}
                </button>
                <h3>
                  <Monitor size={12} />
                  <span title={hostName}>{hostName}</span>
                </h3>
                <i
                  className={`mini-status ${host?.online ? "online" : ""}`}
                  role="img"
                  aria-label={host?.online ? "在线" : "离线"}
                />
                <span
                  className="host-thread-count"
                  title={`当前已加载 ${hostThreads.length} 个会话`}
                >
                  {hostThreads.length}
                </span>
              </header>
              <div
                id={contentId}
                className="host-thread-content"
                role="region"
                aria-label={`${hostName} 会话列表`}
                hidden={!hostExpanded}
                tabIndex={0}
                onScroll={() => setMenuThread(null)}
              >
                {groups.map((group) => {
                  const key = `${hostId}:${group.id}`;
                  const expanded = !collapsed.has(key);
                  const orderedThreads = [...group.threads].sort(
                    (first, second) =>
                      Number(!!second.pinned) - Number(!!first.pinned),
                  );
                  return (
                    <section
                      className={`machine-project-group ${group.project?.id === selectedProjectId ? "selected-project-group" : ""}`}
                      key={group.id}
                      aria-label={`${group.name} 项目会话`}
                    >
                      <div className="project-row-with-settings">
                        <button
                          type="button"
                          className="icon-button project-collapse-button"
                          aria-label={expanded ? "收起会话" : "展开会话"}
                          aria-expanded={expanded}
                          title={`${expanded ? "收起" : "展开"} ${group.name} 的会话`}
                          onClick={() => toggleGroup(key)}
                        >
                          {expanded ? (
                            <ChevronDown size={15} />
                          ) : (
                            <ChevronRight size={15} />
                          )}
                        </button>
                        {group.project ? (
                          <button
                            title={`${group.cwd} · 点击筛选此项目`}
                            aria-label={`${group.name} · ${group.cwd}`}
                            className={`project-row ${group.project.id === selectedProjectId ? "expanded" : ""}`}
                            aria-pressed={
                              group.project.id === selectedProjectId
                            }
                            onClick={() =>
                              onFilter(
                                selectedProjectId === group.project!.id
                                  ? null
                                  : group.project!.id,
                              )
                            }
                          >
                            <Folder size={15} />
                            <span>
                              <strong>{group.name}</strong>
                            </span>
                            <em
                              title={
                                view === "all"
                                  ? `已加载 ${group.threads.length} 个会话，项目共 ${Math.max(group.project.thread_count, group.threads.length)} 个`
                                  : `此视图已加载 ${group.threads.length} 个会话`
                              }
                            >
                              {view === "all"
                                ? Math.max(
                                    group.project.thread_count,
                                    group.threads.length,
                                  )
                                : group.threads.length}
                            </em>
                          </button>
                        ) : (
                          <div
                            className="directory-group-heading"
                            title={group.cwd || "未归属项目"}
                          >
                            <Folder size={15} />
                            <span>
                              <strong>{group.name}</strong>
                            </span>
                            <em>{group.threads.length}</em>
                          </div>
                        )}
                        {group.project && onNewConversation && (
                          <button
                            className="icon-button project-new-button"
                            aria-label={`在 ${group.name} 新建对话`}
                            title="新建对话"
                            onClick={() => onNewConversation(group.project!)}
                          >
                            <MessageSquarePlus size={15} />
                          </button>
                        )}
                        {group.project && onSettings && (
                          <button
                            className="icon-button project-settings-button"
                            aria-label={`${group.name} 项目默认设置`}
                            title="项目默认设置"
                            onClick={() => onSettings(group.project!)}
                          >
                            <Settings2 size={15} />
                          </button>
                        )}
                      </div>
                      {expanded && (
                        <div className="project-thread-list">
                          {orderedThreads.map((thread) => (
                            <div
                              className="thread-row-with-menu"
                              key={thread.id}
                            >
                              <a
                                href={`/tasks/${thread.id}`}
                                className={`thread-row ${thread.id === currentThreadId ? "selected" : ""}`}
                                title={`${threadLabel(thread)}${threadActivity(thread) ? ` · ${threadActivity(thread)}` : ""}`}
                                aria-current={
                                  thread.id === currentThreadId
                                    ? "page"
                                    : undefined
                                }
                                onClick={(event) => {
                                  event.preventDefault();
                                  onNavigate(thread.id);
                                }}
                              >
                                <span
                                  className={`thread-dot status-${thread.status}`}
                                  aria-hidden="true"
                                />
                                <span className="thread-text">
                                  <strong>{threadLabel(thread)}</strong>
                                </span>
                                {threadActivity(thread) && (
                                  <span
                                    className={`thread-state status-${thread.status}`}
                                  >
                                    {threadActivity(thread)}
                                  </span>
                                )}
                                {thread.pinned && (
                                  <Pin
                                    className="thread-pin"
                                    size={12}
                                    aria-label="已置顶"
                                  />
                                )}
                              </a>
                              {onThreadUpdate && (
                                <details
                                  className="thread-menu"
                                  open={menuThread === thread.id}
                                >
                                  <summary
                                    aria-label={`${threadLabel(thread)} 会话操作`}
                                    aria-expanded={menuThread === thread.id}
                                    onClick={(event) => {
                                      event.preventDefault();
                                      const box =
                                        event.currentTarget.getBoundingClientRect();
                                      setMenuPosition({
                                        top: Math.max(
                                          8,
                                          Math.min(
                                            box.bottom + 4,
                                            window.innerHeight - 210,
                                          ),
                                        ),
                                        left: Math.max(8, box.right - 205),
                                      });
                                      setMenuThread(
                                        menuThread === thread.id
                                          ? null
                                          : thread.id,
                                      );
                                      setUpdateError("");
                                    }}
                                  >
                                    <MoreHorizontal size={16} />
                                  </summary>
                                  {menuThread === thread.id &&
                                    createPortal(
                                      <div
                                        className="thread-menu-popover"
                                        style={menuPosition}
                                      >
                                        <button
                                          type="button"
                                          disabled={!!savingId}
                                          onClick={() =>
                                            void updateThread(thread, {
                                              pinned: !thread.pinned,
                                            })
                                          }
                                        >
                                          {thread.pinned ? (
                                            <PinOff size={14} />
                                          ) : (
                                            <Pin size={14} />
                                          )}
                                          {thread.pinned
                                            ? "取消置顶"
                                            : "置顶会话"}
                                        </button>
                                        <button
                                          type="button"
                                          disabled={!!savingId}
                                          onClick={() => {
                                            setRenameThread(thread);
                                            setMenuThread(null);
                                            setUpdateError("");
                                          }}
                                        >
                                          <Pencil size={14} />
                                          重命名
                                        </button>
                                        <button
                                          type="button"
                                          disabled={!!savingId}
                                          onClick={() =>
                                            void updateThread(thread, {
                                              archived: !thread.archived,
                                            })
                                          }
                                        >
                                          {thread.archived ? (
                                            <ArchiveRestore size={14} />
                                          ) : (
                                            <Archive size={14} />
                                          )}
                                          {thread.archived
                                            ? "恢复会话"
                                            : "归档会话"}
                                        </button>
                                        {!thread.archived && (
                                          <p>
                                            归档后，正在运行的任务会继续执行。
                                          </p>
                                        )}
                                      </div>,
                                      document.body,
                                    )}
                                </details>
                              )}
                            </div>
                          ))}
                          {!group.threads.length &&
                            group.project?.id === selectedProjectId && (
                              <p className="project-conversation-empty">
                                {loading || page.loading ? (
                                  <Spinner label="加载会话" />
                                ) : view !== "all" ? (
                                  "当前视图没有会话"
                                ) : group.project?.thread_count ? (
                                  "选择项目以加载会话"
                                ) : (
                                  "此项目还没有会话"
                                )}
                              </p>
                            )}
                        </div>
                      )}
                    </section>
                  );
                })}
                {!groups.length && view === "all" && (
                  <div className="sidebar-empty">
                    {loading || page.loading ? (
                      <Spinner label="加载会话" />
                    ) : (
                      <>
                        <Folder size={22} />
                        <p>
                          {view === "all"
                            ? "这里还没有会话"
                            : "当前视图没有会话"}
                        </p>
                      </>
                    )}
                  </div>
                )}
                {page.hasMore && (
                  <button
                    className="sidebar-load-more"
                    aria-label={
                      perHost ? `加载 ${hostName} 的更多会话` : undefined
                    }
                    disabled={page.loading}
                    onClick={
                      perHost && onHostMore ? () => onHostMore(hostId) : onMore
                    }
                  >
                    {page.loading ? (
                      <Spinner label="加载中" />
                    ) : view !== "all" && !groups.length ? (
                      `加载 ${hostName} 的更多会话`
                    ) : (
                      "加载更多会话"
                    )}
                  </button>
                )}
              </div>
            </section>
          );
        })}
        {!threadHostIds.length && view === "all" && (
          <div className="sidebar-empty">
            {loading ? (
              <Spinner label="加载会话" />
            ) : (
              <>
                <Folder size={22} />
                <p>这里还没有会话</p>
                <button className="text-button" onClick={onNewProject}>
                  新建项目并选择设备目录
                </button>
              </>
            )}
          </div>
        )}
      </div>
      {renameThread && (
        <RenameThreadDialog
          thread={renameThread}
          saving={savingId === renameThread.id}
          error={updateError}
          onClose={() => setRenameThread(null)}
          onSave={(name) =>
            void updateThread(renameThread, { display_title: name || null })
          }
        />
      )}
    </nav>
  );
}
