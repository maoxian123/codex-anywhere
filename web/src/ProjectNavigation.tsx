import { useEffect, useState } from "react";
import {
  ChevronDown,
  Folder,
  Layers,
  Monitor,
  Plus,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { Spinner } from "./components";
import { groupThreadsByDate, threadActivityLabel } from "./sidebar-activity";
import type { Host, Project, Thread } from "./types";

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
}) {
  const [treeOpen, setTreeOpen] = useState(Boolean(selectedProjectId));
  useEffect(() => {
    if (selectedProjectId) setTreeOpen(true);
  }, [selectedProjectId]);
  const threadList = (
    groupThreads: Thread[],
    page: { hasMore: boolean; loading: boolean },
    hostName: string,
    onLoadMore: () => void,
    hostPagination: boolean,
  ) => (
    <>
      <div className="project-thread-list">
        {(loading || page.loading) && !groupThreads.length ? (
          <div className="sidebar-empty">
            <Spinner label="加载会话" />
          </div>
        ) : groupThreads.length ? (
          groupThreadsByDate(groupThreads, undefined, true).map((group) => (
            <section className="thread-date-group" key={group.threads[0].id}>
              <h3 className="thread-date-label">{group.label}</h3>
              {group.threads.map((thread) => (
                <a
                  key={thread.id}
                  href={`/tasks/${thread.id}`}
                  className={`thread-row ${thread.id === currentThreadId ? "selected" : ""}`}
                  onClick={(event) => {
                    event.preventDefault();
                    onNavigate(thread.id);
                  }}
                >
                  <span className={`thread-dot status-${thread.status}`} />
                  <span className="thread-text">
                    <strong>{thread.title || "未命名会话"}</strong>
                    <small>
                      {thread.imported ? "本机会话" : "Codex"}
                      <span>{threadActivityLabel(thread)}</span>
                    </small>
                  </span>
                  {thread.status === "waiting" && (
                    <span className="attention-dot" title="等待处理" />
                  )}
                </a>
              ))}
            </section>
          ))
        ) : (
          <div className="sidebar-empty">
            <Folder size={22} />
            <p>这里还没有会话</p>
          </div>
        )}
      </div>
      {page.hasMore && (
        <button
          className="sidebar-load-more"
          aria-label={
            hostPagination ? `加载 ${hostName} 的更多会话` : undefined
          }
          disabled={page.loading}
          onClick={onLoadMore}
        >
          {page.loading ? <Spinner label="加载中" /> : "加载更多会话"}
        </button>
      )}
    </>
  );
  const hostIds = [...new Set(projects.map((project) => project.host_id))];
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
        ]),
      ];
  return (
    <nav
      className="project-navigation grouped-navigation"
      aria-label="项目与会话"
    >
      <div className="navigation-scope">
        <button
          className={`all-threads-button ${selectedProjectId === null ? "selected" : ""}`}
          onClick={() => {
            setTreeOpen(false);
            onFilter(null);
          }}
        >
          <Layers size={15} />
          全部会话
          <span>{selectedProjectId === null ? threads.length : ""}</span>
        </button>
        <button
          className="icon-button"
          onClick={onRefresh}
          aria-label="刷新任务列表"
        >
          <RefreshCw size={13} />
        </button>
      </div>
      <details
        className="project-filter-tree"
        open={treeOpen}
        onToggle={(event) => setTreeOpen(event.currentTarget.open)}
      >
        <summary>
          <ChevronDown size={13} />
          项目<span>{projects.length}</span>
        </summary>
        <div className="sidebar-section-label">
          <span>按设备与目录</span>
          <button
            className="icon-button"
            onClick={onNewProject}
            aria-label="添加项目"
          >
            <Plus size={14} />
          </button>
        </div>
        {hostIds.map((hostId) => {
          const host = hosts.find((value) => value.id === hostId);
          const group = projects.filter(
            (project) => project.host_id === hostId,
          );
          if (!group.length) return null;
          return (
            <section className="project-host-group" key={hostId}>
              <h3>
                <Monitor size={12} />
                <span>{host?.name || hostId}</span>
                <i className={`mini-status ${host?.online ? "online" : ""}`} />
              </h3>
              {group.map((project) => (
                <div className="project-group" key={project.id}>
                  <div className="project-row-with-settings">
                    <button
                      title={project.cwd}
                      className={`project-row ${selectedProjectId === project.id ? "expanded" : ""}`}
                      aria-expanded={selectedProjectId === project.id}
                      onClick={() =>
                        onFilter(
                          selectedProjectId === project.id ? null : project.id,
                        )
                      }
                    >
                      <ChevronDown size={13} />
                      <Folder size={14} />
                      <span>
                        <strong>{project.name}</strong>
                        <small>{project.cwd}</small>
                      </span>
                      <em>{project.thread_count}</em>
                    </button>
                    {onSettings && (
                      <button
                        className="icon-button project-settings-button"
                        aria-label={`${project.name} 项目默认设置`}
                        title="项目默认设置"
                        onClick={() => onSettings(project)}
                      >
                        <Settings2 size={13} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </section>
          );
        })}
        {!projects.length && (
          <button className="connect-device" onClick={onNewProject}>
            <Folder size={15} />
            新建项目并选择设备目录
          </button>
        )}
      </details>
      <div
        className={`host-thread-groups ${threadHostIds.length === 1 ? "single-host" : ""}`}
      >
        {threadHostIds.map((hostId) => {
          const host = hosts.find((value) => value.id === hostId);
          const hostName = host?.name || hostId;
          const groupThreads = threads.filter(
            (thread) => thread.host_id === hostId,
          );
          const perHost = selectedProjectId === null && !!hostPages;
          const page = perHost
            ? hostPages[hostId] || { hasMore: false, loading: false }
            : { hasMore, loading: loadingMore };
          const compact =
            !loading &&
            !page.loading &&
            !page.hasMore &&
            groupThreads.length <= 3;
          return (
            <section
              className={`host-thread-group ${compact ? "compact-thread-host" : ""} ${!groupThreads.length ? "empty-host" : ""}`}
              key={hostId}
              aria-label={`${hostName} 会话`}
              aria-busy={loading || page.loading}
            >
              <header className="host-thread-heading">
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
                  title={`当前已加载 ${groupThreads.length} 个会话`}
                >
                  {groupThreads.length}
                </span>
              </header>
              <div
                className="host-thread-scroll"
                role="region"
                aria-label={`${hostName} 会话列表`}
                tabIndex={0}
              >
                {threadList(
                  groupThreads,
                  page,
                  hostName,
                  perHost && onHostMore ? () => onHostMore(hostId) : onMore,
                  perHost,
                )}
              </div>
            </section>
          );
        })}
        {!threadHostIds.length && (
          <div className="sidebar-empty">
            {loading ? (
              <Spinner label="加载会话" />
            ) : (
              <>
                <Folder size={22} />
                <p>这里还没有会话</p>
              </>
            )}
          </div>
        )}
      </div>
    </nav>
  );
}
