import {
  Folder,
  Layers,
  Monitor,
  Plus,
  RefreshCw,
  Settings2,
} from "lucide-react";
import { Spinner } from "./components";
import type { Host, Project, Thread } from "./types";

type NavigationGroup = {
  id: string;
  name: string;
  cwd: string;
  project?: Project;
  threads: Thread[];
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
function projectGroups(projects: Project[], threads: Thread[], host?: Host) {
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

  return (
    <nav
      className="project-navigation grouped-navigation"
      aria-label="项目与会话"
    >
      <div className="navigation-scope">
        <button
          className={`all-threads-button ${selectedProjectId === null ? "selected" : ""}`}
          onClick={() => onFilter(null)}
        >
          <Layers size={15} />
          全部会话
          <span>{selectedProjectId === null ? threads.length : ""}</span>
        </button>
        <button
          className="icon-button"
          onClick={onNewProject}
          aria-label="添加项目"
          title="新建项目并选择设备目录"
        >
          <Plus size={14} />
        </button>
        <button
          className="icon-button"
          onClick={onRefresh}
          aria-label="刷新任务列表"
        >
          <RefreshCw size={13} />
        </button>
      </div>
      <div
        className={`host-thread-groups ${threadHostIds.length === 1 ? "single-host" : ""}`}
      >
        {threadHostIds.map((hostId) => {
          const host = hosts.find((value) => value.id === hostId);
          const hostName = host?.name || hostId;
          const hostThreads = threads.filter(
            (thread) => thread.host_id === hostId,
          );
          const hostProjects = projects.filter(
            (project) =>
              project.host_id === hostId &&
              (!selectedProjectId || project.id === selectedProjectId),
          );
          const groups = projectGroups(hostProjects, hostThreads, host);
          const perHost = selectedProjectId === null && !!hostPages;
          const page = perHost
            ? hostPages[hostId] || { hasMore: false, loading: false }
            : { hasMore, loading: loadingMore };
          const compact =
            !loading &&
            !page.loading &&
            !page.hasMore &&
            hostThreads.length + groups.length <= 4;
          return (
            <section
              className={`host-thread-group ${compact ? "compact-thread-host" : ""} ${!hostThreads.length && !groups.length ? "empty-host" : ""}`}
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
                  title={`当前已加载 ${hostThreads.length} 个会话`}
                >
                  {hostThreads.length}
                </span>
              </header>
              <div
                className="host-thread-scroll"
                role="region"
                aria-label={`${hostName} 会话列表`}
                tabIndex={0}
              >
                {groups.map((group) => (
                  <section
                    className={`machine-project-group ${group.project?.id === selectedProjectId ? "selected-project-group" : ""}`}
                    key={group.id}
                    aria-label={`${group.name} 项目会话`}
                  >
                    <div className="project-row-with-settings">
                      {group.project ? (
                        <button
                          title={group.cwd}
                          className={`project-row ${group.project.id === selectedProjectId ? "expanded" : ""}`}
                          aria-pressed={group.project.id === selectedProjectId}
                          onClick={() =>
                            onFilter(
                              selectedProjectId === group.project!.id
                                ? null
                                : group.project!.id,
                            )
                          }
                        >
                          <Folder size={13} />
                          <span>
                            <strong>{group.name}</strong>
                            <small>{group.cwd}</small>
                          </span>
                          <em
                            title={`已加载 ${group.threads.length} 个会话，项目共 ${Math.max(group.project.thread_count, group.threads.length)} 个`}
                          >
                            {Math.max(
                              group.project.thread_count,
                              group.threads.length,
                            )}
                          </em>
                        </button>
                      ) : (
                        <div
                          className="directory-group-heading"
                          title={group.cwd || "未归属项目"}
                        >
                          <Folder size={13} />
                          <span>
                            <strong>{group.name}</strong>
                            {group.cwd && <small>{group.cwd}</small>}
                          </span>
                          <em>{group.threads.length}</em>
                        </div>
                      )}
                      {group.project && onSettings && (
                        <button
                          className="icon-button project-settings-button"
                          aria-label={`${group.name} 项目默认设置`}
                          title="项目默认设置"
                          onClick={() => onSettings(group.project!)}
                        >
                          <Settings2 size={13} />
                        </button>
                      )}
                    </div>
                    <div className="project-thread-list">
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
                          <span
                            className={`thread-dot status-${thread.status}`}
                          />
                          <span className="thread-text">
                            <strong>{thread.title || "未命名会话"}</strong>
                            <small>
                              {thread.imported ? "本机会话" : "Codex"}
                            </small>
                          </span>
                          {thread.status === "waiting" && (
                            <span className="attention-dot" title="等待处理" />
                          )}
                        </a>
                      ))}
                      {!group.threads.length && (
                        <p className="project-conversation-empty">
                          {loading || page.loading ? (
                            <Spinner label="加载会话" />
                          ) : group.project?.thread_count ? (
                            "选择项目以加载会话"
                          ) : (
                            "此项目还没有会话"
                          )}
                        </p>
                      )}
                    </div>
                  </section>
                ))}
                {!groups.length && (
                  <div className="sidebar-empty">
                    {loading || page.loading ? (
                      <Spinner label="加载会话" />
                    ) : (
                      <>
                        <Folder size={22} />
                        <p>这里还没有会话</p>
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
                    {page.loading ? <Spinner label="加载中" /> : "加载更多会话"}
                  </button>
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
                <button className="text-button" onClick={onNewProject}>
                  新建项目并选择设备目录
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </nav>
  );
}
