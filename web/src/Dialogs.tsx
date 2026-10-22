import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowRight,
  CircleCheck,
  FolderOpen,
  KeyRound,
  Link2,
  Monitor,
  Plus,
  Terminal,
} from "lucide-react";
import { api, errorText, post, setSession } from "./api";
import { CopyButton, ErrorMessage, Modal, Spinner } from "./components";
import { RuntimeSettingsEditor } from "./ExecutionControls";
import type {
  Command,
  ExecutionSettings,
  Host,
  Project,
  Session,
  Thread,
} from "./types";

export function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await post<Session>("/auth/login", {
        username,
        password,
      });
      setSession(session);
      onLogin(session);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-page">
      <div className="login-brand">
        <img src="/icons/codex-anywhere.svg" alt="" />
        <span>
          codex<span className="brand-light">anywhere</span>
        </span>
      </div>
      <div className="login-card">
        <div className="login-emblem">
          <Terminal size={30} />
        </div>
        <p className="eyebrow">YOUR WORK, WITHIN REACH</p>
        <h1>工作从这里继续。</h1>
        <p className="login-description">
          连接你的设备，在任何地方
          <br />与 Codex 一起完成工作。
        </p>
        <form onSubmit={(event) => void submit(event)} className="form-stack">
          <label>
            用户名
            <input
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="管理员用户名"
              required
              autoFocus
              disabled={busy}
            />
          </label>
          <label>
            密码
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="输入登录密码"
              required
              disabled={busy}
            />
          </label>
          {error && <ErrorMessage>{error}</ErrorMessage>}
          <button
            type="submit"
            className="button button-primary login-submit"
            disabled={busy}
          >
            {busy ? (
              <Spinner label="正在登录" />
            ) : (
              <>
                进入工作台
                <ArrowRight size={16} />
              </>
            )}
          </button>
        </form>
        <p className="login-note">
          <KeyRound size={12} />
          使用管理中心配置的管理员凭据登录
        </p>
      </div>
      <footer className="login-footer">在你的设备执行 · 从任何地方连接</footer>
    </main>
  );
}

export function PairingDialog({ onClose }: { onClose: () => void }) {
  const [pairing, setPairing] = useState<{
    code: string;
    expires_at: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const generate = async () => {
    setBusy(true);
    setError("");
    try {
      setPairing(
        await post<{ code: string; expires_at: string }>("/pairings", {}),
      );
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const expired = pairing
    ? now >= new Date(pairing.expires_at).getTime()
    : false;
  const command = pairing
    ? `codex-anywhere node pair --server ${window.location.origin} --code ${pairing.code} --name "my-computer"`
    : "";
  return (
    <Modal
      title="连接一台设备"
      subtitle="在运行 Codex 的电脑上安装并启动节点服务。"
      onClose={onClose}
    >
      <div className="pairing-steps">
        <section>
          <span className="step-number">1</span>
          <div>
            <h3>在工作电脑安装节点</h3>
            <p>
              先安装 Codex CLI、完成登录，然后在本项目的 Python 环境中运行命令。
            </p>
            <div className="code-block">
              <code>python -m pip install -e .</code>
              <CopyButton text="python -m pip install -e ." />
            </div>
          </div>
        </section>
        <section>
          <span className="step-number">2</span>
          <div>
            <h3>生成一次性配对码</h3>
            <p>配对码用于将这台电脑连接到你的管理中心。</p>
            {pairing ? (
              <div className={`pair-code ${expired ? "expired" : ""}`}>
                <span>{pairing.code}</span>
                <CopyButton text={pairing.code} />
                <small>
                  {expired
                    ? "配对码已过期"
                    : `有效至 ${new Date(pairing.expires_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`}
                </small>
              </div>
            ) : null}
            <button
              className="button button-small"
              onClick={() => void generate()}
              disabled={busy}
            >
              {busy ? (
                <Spinner label="生成中" />
              ) : (
                <>
                  <Link2 size={14} />
                  {pairing ? "重新生成配对码" : "生成配对码"}
                </>
              )}
            </button>
          </div>
        </section>
        <section>
          <span className="step-number">3</span>
          <div>
            <h3>执行配对并启动</h3>
            <p>
              配对后可在网页浏览此设备的目录并创建项目。远程连接请使用 HTTPS。
            </p>
            {pairing && !expired ? (
              <>
                <div className="code-block">
                  <code>{command}</code>
                  <CopyButton text={command} />
                </div>
                <div className="code-block">
                  <code>codex-anywhere node run</code>
                  <CopyButton text="codex-anywhere node run" />
                </div>
              </>
            ) : (
              <div className="code-placeholder">
                <Terminal size={15} />
                生成配对码后显示连接命令
              </div>
            )}
          </div>
        </section>
      </div>
      {error && <ErrorMessage>{error}</ErrorMessage>}
      <div className="modal-actions">
        <span className="field-hint">
          <CircleCheck size={13} />
          连接成功后设备会自动显示
        </span>
        <button className="button button-primary" onClick={onClose}>
          完成
        </button>
      </div>
    </Modal>
  );
}

export function NewTaskDialog({
  hosts,
  projects,
  initialProjectId,
  onClose,
  onCreated,
  onNewProject,
}: {
  hosts: Host[];
  projects: Project[];
  initialProjectId?: string | null;
  onClose: () => void;
  onCreated: (thread: Thread, command: Command) => void;
  onNewProject: () => void;
}) {
  const initial = projects.find((project) => project.id === initialProjectId);
  const [hostId, setHostId] = useState(
    initial?.host_id || hosts.find((host) => host.online)?.id || "",
  );
  const [projectId, setProjectId] = useState(
    initial?.id ||
      projects.find((project) => project.host_id === hostId)?.id ||
      "",
  );
  const project = projects.find(
    (value) => value.id === projectId && value.host_id === hostId,
  );
  const host = hosts.find((value) => value.id === hostId);
  const [settings, setSettings] = useState<ExecutionSettings>(
    project?.execution_settings || {},
  );
  useEffect(() => {
    setSettings(project?.execution_settings || {});
  }, [project?.id]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (
      !projects.some(
        (value) => value.id === projectId && value.host_id === hostId,
      )
    )
      setProjectId(
        projects.find((value) => value.host_id === hostId)?.id || "",
      );
  }, [projects, hostId, projectId]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!project || !host?.online) return;
    setBusy(true);
    setError("");
    try {
      const result = await post<{ thread: Thread; command: Command }>(
        "/threads",
        {
          host_id: hostId,
          project_id: projectId,
          ...(Object.keys(settings).length
            ? { execution_settings: settings }
            : {}),
        },
      );
      onCreated(result.thread, result.command);
      onClose();
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="创建新任务"
      subtitle="选择工作设备和项目，在对应目录中开始 Codex 会话。"
      onClose={onClose}
    >
      <form className="form-stack" onSubmit={(event) => void submit(event)}>
        <label>
          <span className="label-with-icon">
            <Monitor size={14} />
            运行设备
          </span>
          <select
            value={hostId}
            onChange={(event) => setHostId(event.target.value)}
            required
            disabled={busy}
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
          <span className="label-with-icon">
            <FolderOpen size={14} />
            项目
          </span>
          <select
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            required
            disabled={busy}
          >
            <option value="" disabled>
              选择项目
            </option>
            {projects
              .filter((value) => value.host_id === hostId)
              .map((value) => (
                <option key={value.id} value={value.id}>
                  {value.name} · {value.cwd}
                </option>
              ))}
          </select>
        </label>
        {project && <p className="field-hint">工作目录：{project.cwd}</p>}
        <button
          type="button"
          className="text-button"
          onClick={onNewProject}
          disabled={busy}
        >
          <Plus size={14} />
          新建项目 / 选择设备目录
        </button>
        {project && (
          <RuntimeSettingsEditor
            key={project.id}
            host={host}
            cwd={project.cwd}
            settings={settings}
            disabled={busy}
            onChange={setSettings}
          />
        )}
        {!host?.online && (
          <ErrorMessage>当前设备离线，请选择在线设备。</ErrorMessage>
        )}
        {error && <ErrorMessage>{error}</ErrorMessage>}
        <div className="modal-actions">
          <button
            type="button"
            className="button button-quiet"
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="submit"
            className="button button-primary"
            disabled={busy || !host?.online || !project}
          >
            {busy ? (
              <Spinner label="创建中" />
            ) : (
              <>
                <Plus size={15} />
                创建任务
              </>
            )}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function useInitialSession() {
  const [session, updateSession] = useState<Session | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const expired = () => setRetry((value) => value + 1);
    window.addEventListener("anywhere:session-expired", expired);
    return () =>
      window.removeEventListener("anywhere:session-expired", expired);
  }, []);
  useEffect(() => {
    let disposed = false;
    setError("");
    void api<Session>("/auth/me")
      .then((value) => {
        if (!disposed) {
          setSession(value);
          updateSession(value);
        }
      })
      .catch((reason) => {
        if (!disposed) setError(errorText(reason));
      });
    return () => {
      disposed = true;
    };
  }, [retry]);
  return {
    session,
    updateSession,
    error,
    retry: () => setRetry((value) => value + 1),
  };
}
