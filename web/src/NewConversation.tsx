import { useEffect, useRef, useState } from "react";
import { Bot, FolderPlus, Monitor } from "lucide-react";
import Composer from "./Composer";
import {
  clearComposerDraft,
  readComposerDraft,
  writeComposerDraft,
} from "./composer-drafts";
import { RuntimeSettingsEditor } from "./ExecutionControls";
import {
  abandonPendingConversation,
  pendingConversation,
  startConversation,
} from "./conversation-start";
import { useRuntimeCapabilities } from "./useRuntimeCapabilities";
import { Spinner } from "./components";
import type {
  Command,
  ExecutionSettings,
  Host,
  MessagePayload,
  Project,
  Thread,
} from "./types";

export default function NewConversation({
  hosts,
  projects,
  initialProjectId,
  loading,
  draftScope,
  onNewProject,
  onPair,
  onNavigate,
  onCreated,
}: {
  hosts: Host[];
  projects: Project[];
  initialProjectId?: string;
  loading: boolean;
  draftScope: string;
  onNewProject: () => void;
  onPair: () => void;
  onNavigate: (id: string) => void;
  onCreated: (thread: Thread, command: Command) => void;
}) {
  const [projectId, setProjectId] = useState(initialProjectId || "");
  const project = projects.find((entry) => entry.id === projectId);
  const host = hosts.find((entry) => entry.id === project?.host_id);
  const [settings, setSettings] = useState<ExecutionSettings>({});
  const [starting, setStarting] = useState(false);
  const [pendingThread, setPendingThread] = useState<Thread>();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!projects.some((entry) => entry.id === projectId)) {
      let remembered = "";
      try {
        remembered =
          sessionStorage.getItem(`anywhere:last-project:${draftScope}`) || "";
      } catch {
        /* Optional preference. */
      }
      setProjectId(
        projects.find((entry) => entry.id === initialProjectId)?.id ||
          projects.find((entry) => entry.id === remembered)?.id ||
          projects.find((entry) =>
            hosts.some((node) => node.id === entry.host_id && node.online),
          )?.id ||
          projects[0]?.id ||
          "",
      );
    }
  }, [initialProjectId, projects, hosts, projectId, draftScope]);
  useEffect(() => {
    if (!project) return;
    setSettings(
      pendingConversation(project.id, draftScope)?.request.execution_settings ||
        project.execution_settings ||
        {},
    );
    setPendingThread(pendingConversation(project.id, draftScope)?.thread);
    try {
      sessionStorage.setItem(`anywhere:last-project:${draftScope}`, project.id);
    } catch {
      /* Optional preference. */
    }
  }, [project?.id, draftScope]);
  const runtime = useRuntimeCapabilities(host, project?.cwd || "");
  const send = async (payload: MessagePayload) => {
    if (!project || !host?.online)
      throw new Error("请先选择在线设备上的项目。");
    setStarting(true);
    try {
      const result = await startConversation(
        project,
        settings,
        payload,
        draftScope,
        (thread) => {
          if (alive.current) setPendingThread(thread);
        },
      );
      if (
        readComposerDraft(`new:${project.id}`, draftScope).retryRequestId ===
        payload.client_request_id
      )
        clearComposerDraft(`new:${project.id}`, draftScope);
      if (alive.current) onCreated(result.thread, result.command);
    } finally {
      if (alive.current) setStarting(false);
    }
  };
  const pending = project && pendingConversation(project.id, draftScope);
  const virtualThread: Thread | null = project
    ? {
        id: `new:${project.id}`,
        host_id: project.host_id,
        project_id: project.id,
        title: "新对话",
        cwd: project.cwd,
        status: "idle",
        created_at: "",
        updated_at: "",
        history_loaded: true,
        execution_settings: settings,
        effective_settings: {
          model: runtime.value?.defaults.model,
          effort: runtime.value?.defaults.effort,
          sandbox_mode: "workspace-write",
          approval_policy: "on-request",
          ...settings,
        },
      }
    : null;
  return (
    <section className="new-conversation-page" aria-label="新对话">
      <div className="new-conversation-heading">
        <Bot size={32} aria-hidden="true" />
        <h2>今天想完成什么？</h2>
        <p>描述你的目标，Codex 会在所选项目中开始工作。</p>
      </div>
      {loading && !projects.length ? (
        <Spinner label="加载项目" />
      ) : projects.length ? (
        <>
          <div className="new-conversation-context">
            <label>
              工作项目
              <select
                aria-label="工作项目"
                value={projectId}
                disabled={starting}
                onChange={(event) => setProjectId(event.target.value)}
              >
                {hosts.map((node) => (
                  <optgroup
                    key={node.id}
                    label={`${node.name}${node.online ? "" : " · 离线"}`}
                  >
                    {projects
                      .filter((entry) => entry.host_id === node.id)
                      .map((entry) => (
                        <option key={entry.id} value={entry.id}>
                          {entry.name}
                        </option>
                      ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <button
              className="text-button"
              onClick={onNewProject}
              disabled={starting}
            >
              <FolderPlus size={16} />
              新建项目
            </button>
          </div>
          {project && (
            <p className="new-conversation-location">
              <Monitor size={14} />
              {host?.name}
              {!host?.online && " · 离线"}
              <span title={project.cwd}>{project.cwd}</span>
            </p>
          )}
          {virtualThread && (
            <>
              <Composer
                key={virtualThread.id}
                autoFocus
                thread={virtualThread}
                draftScope={draftScope}
                host={host}
                hostOnline={Boolean(host?.online)}
                project={project}
                onSettingsChange={async (value) => {
                  if (!starting && !pending) setSettings(value);
                }}
                onSend={send}
                onInterrupt={async () => {}}
                onNavigate={onNavigate}
                hideSettings
              />
              <details className="new-conversation-options">
                <summary>模型与权限</summary>
                <RuntimeSettingsEditor
                  host={host}
                  cwd={virtualThread.cwd}
                  settings={settings}
                  disabled={starting || Boolean(pending)}
                  onChange={setSettings}
                />
              </details>
            </>
          )}
          {pendingThread && (
            <button
              className="text-button pending-conversation-link"
              onClick={() => onNavigate(pendingThread.id)}
            >
              查看已创建的会话；草稿仍保留在此页
            </button>
          )}
          {pending && !starting && (
            <div className="new-conversation-recovery">
              <p>
                上次创建的会话会保留。你可以继续检查它的状态，也可以开始另一条对话。
              </p>
              <button
                className="text-button"
                onClick={() => {
                  if (!project) return;
                  abandonPendingConversation(project.id, draftScope);
                  const draft = readComposerDraft(
                    `new:${project.id}`,
                    draftScope,
                  );
                  writeComposerDraft(
                    `new:${project.id}`,
                    { ...draft, retryRequestId: null },
                    draftScope,
                  );
                  setPendingThread(undefined);
                  setSettings({ ...project.execution_settings });
                }}
              >
                开始另一条新对话
              </button>
            </div>
          )}
        </>
      ) : (
        <div className="new-conversation-setup">
          <p>
            {hosts.length
              ? "选择设备上的目录，添加第一个项目。"
              : "连接运行 Codex 的电脑后，就可以从这里开始。"}
          </p>
          <button
            className="button button-primary"
            onClick={hosts.length ? onNewProject : onPair}
          >
            {hosts.length ? "添加项目" : "连接设备"}
          </button>
        </div>
      )}
    </section>
  );
}
