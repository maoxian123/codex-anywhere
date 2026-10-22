import { useEffect, useRef, useState, type FormEvent } from "react";
import { HardDrive, RefreshCw, Trash2 } from "lucide-react";
import { api, errorText } from "./api";
import { ErrorMessage, Modal, Spinner } from "./components";
import type { Host } from "./types";

type ManagedHost = Host & {
  data: {
    projects: number;
    threads: number;
    messages: number;
    attachments: number;
    attachment_bytes: number;
  };
};
type DeleteResult = {
  deleted: boolean;
  host_id: string;
  warning?: string;
  cleanup?: { failed: number; missing: number; deleted: number };
  retained_shared_assets?: number;
};

function quantity(value: number) {
  return Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
}

function attachmentSize(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[unit]}`;
}

export default function HostSettings({
  onHostDeleted,
}: {
  onHostDeleted?: (hostId: string) => void;
}) {
  const [hosts, setHosts] = useState<ManagedHost[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<ManagedHost | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [warning, setWarning] = useState("");
  const deleting = useRef(false);
  const removed = useRef(new Set<string>());
  const modalContainer = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    void api<{ hosts: ManagedHost[] }>("/settings/hosts", {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted)
          setHosts(
            result.hosts.filter((host) => !removed.current.has(host.id)),
          );
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(errorText(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const container = modalContainer.current;
    if (!container) return;
    const preventPendingClose = (event: Event) => {
      if (deleting.current) event.preventDefault();
    };
    container.addEventListener("cancel", preventPendingClose, true);
    return () =>
      container.removeEventListener("cancel", preventPendingClose, true);
  }, [selected?.id]);

  function choose(host: ManagedHost) {
    if (deleting.current) return;
    setSelected(host);
    setConfirmation("");
    setDeleteError("");
  }

  function close() {
    if (deleting.current) return;
    setSelected(null);
    setConfirmation("");
    setDeleteError("");
  }

  async function remove(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (deleting.current || !selected || confirmation !== selected.name) return;
    const host = selected;
    deleting.current = true;
    setPending(true);
    setDeleteError("");
    try {
      const result = await api<DeleteResult>(
        `/hosts/${encodeURIComponent(host.id)}`,
        {
          method: "DELETE",
          body: JSON.stringify({ confirm_name: confirmation }),
        },
      );
      if (!result?.deleted || result.host_id !== host.id)
        throw new Error("管理中心未确认删除，请刷新机器列表核对结果。");
      removed.current.add(host.id);
      setHosts(
        (current) => current?.filter((value) => value.id !== host.id) ?? [],
      );
      setSelected(null);
      setConfirmation("");
      setNotice(`已删除“${host.name}”在管理中心保存的机器记录与数据。`);
      const cleanup =
        result.warning ||
        (result.cleanup?.failed
          ? `仍有 ${quantity(result.cleanup.failed)} 个附件文件清理未完成。`
          : "");
      setWarning(
        [
          cleanup,
          result.retained_shared_assets
            ? "已保留其他机器仍在使用的共享附件。"
            : "",
        ]
          .filter(Boolean)
          .join(" "),
      );
      onHostDeleted?.(host.id);
    } catch (error) {
      setDeleteError(errorText(error));
    } finally {
      deleting.current = false;
      setPending(false);
    }
  }

  return (
    <section
      className="settings-section host-settings-section"
      id="host-settings"
      aria-labelledby="host-settings-title"
    >
      <header className="settings-section-header host-settings-header">
        <div>
          <h2 id="host-settings-title">机器与数据</h2>
          <p>管理已配对的机器及管理中心保存的数据。</p>
        </div>
        <button
          className="button button-small"
          aria-label="刷新机器列表"
          disabled={loading || pending}
          onClick={() => setAttempt((value) => value + 1)}
        >
          <RefreshCw size={14} aria-hidden="true" />
          刷新
        </button>
      </header>
      {notice && (
        <p className="host-settings-notice" role="status">
          {notice}
        </p>
      )}
      {warning && <p className="host-settings-warning">{warning}</p>}
      {loadError && (
        <div className="host-settings-load-error">
          <ErrorMessage>{loadError}</ErrorMessage>
          <button
            className="button button-small"
            disabled={loading || pending}
            onClick={() => setAttempt((value) => value + 1)}
          >
            重新加载机器列表
          </button>
        </div>
      )}
      {loading && <Spinner label="正在加载机器列表" />}
      {hosts?.length === 0 && !loading && !loadError && (
        <p className="host-settings-empty">还没有已配对的机器。</p>
      )}
      <div className="host-settings-list">
        {hosts?.map((host) => (
          <article
            className="host-settings-card"
            key={host.id}
            aria-labelledby={`host-settings-name-${host.id}`}
          >
            <header className="host-settings-card-header">
              <div className="host-settings-identity">
                <HardDrive size={18} aria-hidden="true" />
                <div>
                  <h3 id={`host-settings-name-${host.id}`}>{host.name}</h3>
                  <p>
                    {host.platform || "未知系统"}
                    {host.version ? ` · ${host.version}` : ""}
                  </p>
                </div>
              </div>
              <span
                className={`host-settings-state ${host.online ? "is-online" : "is-offline"}`}
              >
                {host.online ? "在线" : "离线"}
              </span>
            </header>
            <dl
              className="host-settings-statistics"
              aria-label="管理中心保存的数据"
            >
              <div>
                <dt>项目</dt>
                <dd>{quantity(host.data.projects)}</dd>
              </div>
              <div>
                <dt>会话</dt>
                <dd>{quantity(host.data.threads)}</dd>
              </div>
              <div>
                <dt>消息</dt>
                <dd>{quantity(host.data.messages)}</dd>
              </div>
              <div>
                <dt>附件</dt>
                <dd>{quantity(host.data.attachments)}</dd>
              </div>
              <div>
                <dt>附件大小</dt>
                <dd>{attachmentSize(host.data.attachment_bytes)}</dd>
              </div>
            </dl>
            <footer className="host-settings-card-footer">
              <button
                className="button button-small host-settings-delete"
                aria-label={`删除 ${host.name} 的机器与数据`}
                disabled={loading || pending}
                onClick={() => choose(host)}
              >
                <Trash2 size={14} aria-hidden="true" />
                删除机器与数据
              </button>
            </footer>
          </article>
        ))}
      </div>
      {selected && (
        <div ref={modalContainer}>
          <Modal title="删除机器与管理中心数据" onClose={close}>
            <form className="host-settings-confirmation" onSubmit={remove}>
              <p>
                将删除管理中心保存的“{selected.name}
                ”机器记录、项目、会话、消息和附件，此操作无法撤销。
              </p>
              <p>该机器的连接将失效，再次连接需要重新配对。</p>
              <p>
                机器上的原始 Codex
                会话和项目文件会保留，本地正在运行的任务不会因本操作终止。
              </p>
              <fieldset disabled={pending}>
                <label htmlFor="host-delete-confirmation">
                  输入机器名称以确认删除
                </label>
                <p className="settings-field-hint" id="host-delete-name-hint">
                  请完整输入“{selected.name}”。
                </p>
                <input
                  id="host-delete-confirmation"
                  aria-describedby="host-delete-name-hint"
                  autoComplete="off"
                  spellCheck={false}
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
                {deleteError && <ErrorMessage>{deleteError}</ErrorMessage>}
                <div className="modal-actions">
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={close}
                  >
                    取消
                  </button>
                  <button
                    className="button host-settings-delete"
                    type="submit"
                    disabled={pending || confirmation !== selected.name}
                  >
                    {pending ? "正在删除…" : "确认删除机器与数据"}
                  </button>
                </div>
              </fieldset>
            </form>
          </Modal>
        </div>
      )}
    </section>
  );
}
