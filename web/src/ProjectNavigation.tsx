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
import type { Host, Project, Thread } from "./types";
import { relativeDate } from "./utils";

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
}) {
  const [treeOpen, setTreeOpen] = useState(Boolean(selectedProjectId));
  useEffect(() => {
    if (selectedProjectId) setTreeOpen(true);
  }, [selectedProjectId]);
  const threadList = (
    <>
      <div className="project-thread-list">
        {loading ? (
          <div className="sidebar-empty">
            <Spinner label="加载会话" />
          </div>
        ) : threads.length ? (
          threads.map((thread) => (
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
            <Folder size={22} />
            <p>这里还没有会话</p>
          </div>
        )}
      </div>
      {hasMore && (
        <button
          className="sidebar-load-more"
          disabled={loadingMore}
          onClick={onMore}
        >
          {loadingMore ? <Spinner label="加载中" /> : "加载更多会话"}
        </button>
      )}
    </>
  );
  const hostIds = [
    ...new Set([
      ...hosts.map((host) => host.id),
      ...projects.map((project) => project.host_id),
    ]),
  ];
  return (
    <nav className="project-navigation" aria-label="项目与会话">
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
                  {selectedProjectId === project.id && threadList}
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
      {selectedProjectId === null && <>{threadList}</>}
    </nav>
  );
}
