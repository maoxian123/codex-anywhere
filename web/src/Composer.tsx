import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowUp,
  AtSign,
  ChevronLeft,
  FileCode2,
  Globe,
  ImagePlus,
  Link2,
  LoaderCircle,
  Plus,
  Search,
  Settings2,
  Square,
  X,
} from "lucide-react";
import { api, errorText } from "./api";
import { ErrorMessage, Modal, References, Spinner } from "./components";
import { messageText } from "./Timeline";
import { ThreadExecutionControls } from "./ExecutionControls";
import { settingsLabel } from "./execution-settings";
import type {
  Asset,
  Host,
  Project,
  ExecutionSettings,
  MessagePayload,
  Reference,
  Thread,
  ThreadDetail,
} from "./types";
import { fileName, safeLink } from "./utils";
import { RemoteCommandError } from "./commands";

function TaskPicker({
  currentId,
  onClose,
  onSelect,
}: {
  currentId: string;
  onClose: () => void;
  onSelect: (reference: Reference) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Thread[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const [opening, setOpening] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timer = setTimeout(() => {
      void api<{ threads: Thread[] }>(`/threads?q=${encodeURIComponent(query)}`)
        .then((value) => {
          if (!cancelled) {
            setResults(
              value.threads.filter((thread) => thread.id !== currentId),
            );
            setError("");
          }
        })
        .catch((reason) => {
          if (!cancelled) setError(errorText(reason));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, currentId]);
  const select = (thread: Thread, itemId?: string) => {
    onSelect({
      kind: "task",
      thread_id: thread.id,
      title: thread.title,
      ...(itemId ? { item_id: itemId } : {}),
    });
    onClose();
  };
  const openThread = async (id: string) => {
    setOpening(true);
    try {
      setDetail(
        await api<ThreadDetail>(
          `/threads/${encodeURIComponent(id)}?item_limit=100`,
        ),
      );
      setError("");
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setOpening(false);
    }
  };
  return (
    <Modal
      title={detail ? "选取引用消息" : "引用其他任务"}
      subtitle="引用内容会作为上下文提供给 Codex，并保留来源快照。"
      onClose={onClose}
    >
      {error && <ErrorMessage>{error}</ErrorMessage>}
      {detail ? (
        <>
          <button
            className="text-button back-link"
            onClick={() => setDetail(null)}
          >
            <ChevronLeft size={15} />
            返回任务列表
          </button>
          <div className="picker-thread-heading">
            <FileCode2 size={17} />
            <strong>{detail.thread.title}</strong>
            <button
              className="button button-small"
              onClick={() => select(detail.thread)}
            >
              引用整个任务
            </button>
          </div>
          <div className="reference-message-list">
            {detail.items
              .filter((item) =>
                [
                  "userMessage",
                  "agentMessage",
                  "user_message",
                  "agent_message",
                ].includes(item.type),
              )
              .map((item) => (
                <button
                  key={item.id}
                  onClick={() =>
                    select(detail.thread, item.native_id || item.id)
                  }
                >
                  <span className="eyebrow">
                    {item.type.startsWith("user") ? "你" : "Codex"}
                  </span>
                  <p>{messageText(item).slice(0, 350) || "图片消息"}</p>
                  <Plus size={15} />
                </button>
              ))}
            {!detail.items.length && (
              <p className="empty-small">
                此任务还没有消息，可以引用整个任务。
              </p>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="search-box picker-search">
            <Search size={16} />
            <input
              autoFocus
              placeholder="搜索任务名称或内容"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="搜索引用任务"
            />
          </div>
          <div className="picker-list">
            {loading ? (
              <div className="empty-small">
                <Spinner label="搜索中" />
              </div>
            ) : results.length ? (
              results.map((thread) => (
                <div className="picker-row" key={thread.id}>
                  <button
                    className="picker-main"
                    onClick={() => select(thread)}
                  >
                    <FileCode2 size={17} />
                    <span>
                      <strong>{thread.title || "未命名任务"}</strong>
                      <small>{fileName(thread.cwd)}</small>
                    </span>
                  </button>
                  <button
                    className="text-button"
                    onClick={() => void openThread(thread.id)}
                    disabled={opening}
                  >
                    {opening ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      "选取消息"
                    )}
                  </button>
                </div>
              ))
            ) : (
              <div className="empty-small">
                {query ? "没有找到匹配的任务" : "暂无其他任务可引用"}
              </div>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}

function WebPicker({
  onClose,
  onSelect,
}: {
  onClose: () => void;
  onSelect: (reference: Reference) => void;
}) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [excerpt, setExcerpt] = useState("");
  const [error, setError] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const safe = safeLink(url.trim());
    if (!safe || !/^https?:\/\//.test(safe)) {
      setError("请输入有效的 http 或 https 网页地址。");
      return;
    }
    onSelect({
      kind: "web",
      url: safe,
      ...(title.trim() ? { title: title.trim() } : {}),
      ...(excerpt.trim() ? { excerpt: excerpt.trim() } : {}),
    });
    onClose();
  };
  return (
    <Modal
      title="添加网页引用"
      subtitle="粘贴网页地址和相关摘录，Codex 将收到你提供的内容。"
      onClose={onClose}
    >
      <form onSubmit={submit} className="form-stack">
        <label>
          网页地址<span className="required">*</span>
          <input
            type="url"
            placeholder="https://example.com/article"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            required
            autoFocus
          />
        </label>
        <label>
          标题<span className="field-optional">可选</span>
          <input
            placeholder="给这条来源起一个名字"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            maxLength={300}
          />
        </label>
        <label>
          引用摘录<span className="field-optional">可选</span>
          <textarea
            rows={5}
            placeholder="粘贴需要 Codex 参考的原文、结论或搜索结果…"
            value={excerpt}
            onChange={(event) => setExcerpt(event.target.value)}
            maxLength={30000}
          />
        </label>
        <p className="field-hint">
          系统会保存地址与摘录，不会自动抓取网页正文。
        </p>
        {error && <ErrorMessage>{error}</ErrorMessage>}
        <div className="modal-actions">
          <button
            type="button"
            className="button button-quiet"
            onClick={onClose}
          >
            取消
          </button>
          <button type="submit" className="button button-primary">
            <Link2 size={15} />
            添加引用
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function Composer({
  thread,
  hostOnline,
  onSend,
  onInterrupt,
  onNavigate,
  host,
  project,
  onSettingsChange,
  onProjectSettings,
}: {
  thread: Thread;
  hostOnline: boolean;
  onSend: (payload: MessagePayload, steer: boolean) => Promise<void>;
  onInterrupt: () => Promise<void>;
  onNavigate: (id: string, item?: string) => void;
  host?: Host;
  project?: Project;
  onSettingsChange?: (settings: ExecutionSettings) => Promise<void>;
  onProjectSettings?: (project: Project) => void;
}) {
  const [text, setText] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [references, setReferences] = useState<Reference[]>([]);
  const [uploading, setUploading] = useState(false);
  const [sending, setSending] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [interrupting, setInterrupting] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [picker, setPicker] = useState<"task" | "web" | null>(null);
  const [retryRequestId, setRetryRequestId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const busy = ["active", "waiting"].includes(thread.status);
  const historyReady =
    thread.history_loaded !== false &&
    thread.status !== "importing" &&
    !(thread.status === "idle" && thread.history_stale);
  const ready = hostOnline && thread.status !== "starting" && historyReady;
  useEffect(() => {
    const input = textRef.current;
    if (input) {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    }
  }, [text]);
  const edited = () => setRetryRequestId(null);
  const uploadFiles = async (files: FileList | File[]) => {
    if (uploading || sending) return;
    setUploading(true);
    setError("");
    try {
      for (const file of Array.from(files)) {
        if (!file.type.startsWith("image/"))
          throw new Error(`“${file.name}”不是图片文件。`);
        if (file.size > 20 * 1024 * 1024)
          throw new Error(`“${file.name}”超过 20 MiB 限制。`);
        const form = new FormData();
        form.append("file", file);
        const result = await api<{ asset: Asset }>("/assets", {
          method: "POST",
          body: form,
        });
        setAssets((current) => [...current, result.asset]);
        edited();
      }
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };
  const send = async () => {
    if (
      (!text.trim() && !assets.length && !references.length) ||
      sending ||
      uploading ||
      settingsSaving ||
      !ready
    )
      return;
    setSending(true);
    setError("");
    const requestId = retryRequestId || crypto.randomUUID();
    setRetryRequestId(requestId);
    try {
      await onSend(
        {
          text: text.trim(),
          asset_ids: assets.map((asset) => asset.id),
          references: references.map((reference) =>
            reference.kind === "task"
              ? {
                  kind: "task",
                  thread_id: reference.thread_id,
                  ...(reference.item_id ? { item_id: reference.item_id } : {}),
                }
              : {
                  kind: "web",
                  url: reference.url,
                  title: reference.title,
                  excerpt: reference.excerpt,
                },
          ),
          client_request_id: requestId,
        },
        busy,
      );
      setText("");
      setAssets([]);
      setReferences([]);
      setRetryRequestId(null);
    } catch (reason) {
      if (
        reason instanceof RemoteCommandError &&
        ["failed", "error"].includes(reason.command.status)
      )
        setRetryRequestId(null);
      setError(errorText(reason));
    } finally {
      setSending(false);
      textRef.current?.focus();
    }
  };
  const addReference = (reference: Reference) => {
    setReferences((current) =>
      current.some(
        (existing) =>
          existing.kind === reference.kind &&
          (reference.kind === "task"
            ? existing.thread_id === reference.thread_id &&
              existing.item_id === reference.item_id
            : existing.url === reference.url),
      )
        ? current
        : [...current, reference],
    );
    edited();
  };
  return (
    <div className="composer-area">
      {error && (
        <div className="composer-error">
          <ErrorMessage>{error}</ErrorMessage>
          <button
            className="icon-button"
            aria-label="关闭错误提示"
            onClick={() => setError("")}
          >
            <X size={15} />
          </button>
        </div>
      )}
      <div
        className={`composer ${dragging ? "is-dragging" : ""}`}
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("Files")) {
            event.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null))
            setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          void uploadFiles(event.dataTransfer.files);
        }}
      >
        {dragging && (
          <div className="drop-overlay">
            <ImagePlus size={26} />
            <span>松开以添加图片</span>
          </div>
        )}
        {(assets.length > 0 || references.length > 0) && (
          <div className="composer-attachments">
            <References
              references={references}
              onNavigate={onNavigate}
              removable={(index) => {
                setReferences((current) =>
                  current.filter((_, itemIndex) => itemIndex !== index),
                );
                edited();
              }}
            />
            {assets.length > 0 && (
              <div className="upload-previews">
                {assets.map((asset) => (
                  <div className="upload-preview" key={asset.id}>
                    <img src={asset.url} alt={asset.name} />
                    <button
                      aria-label={`移除图片 ${asset.name}`}
                      onClick={() => {
                        setAssets((current) =>
                          current.filter((value) => value.id !== asset.id),
                        );
                        edited();
                      }}
                    >
                      <X size={12} />
                    </button>
                    <span>{asset.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <textarea
          ref={textRef}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            edited();
          }}
          placeholder={
            !historyReady
              ? "正在导入历史，完成后即可继续对话…"
              : busy
                ? "补充要求，帮助 Codex 调整当前任务…"
                : "描述你想完成的工作…"
          }
          aria-label={busy ? "补充任务要求" : "发送消息给 Codex"}
          rows={1}
          title="Enter 发送 · Shift+Enter 换行"
          disabled={sending}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void send();
            }
            if (
              event.key === "@" &&
              !event.nativeEvent.isComposing &&
              !event.ctrlKey &&
              !event.metaKey
            ) {
              event.preventDefault();
              setPicker("task");
            }
          }}
          onPaste={(event) => {
            if (event.clipboardData.files.length) {
              event.preventDefault();
              void uploadFiles(event.clipboardData.files);
            }
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-tools">
            <input
              ref={inputRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(event) => {
                if (event.target.files) void uploadFiles(event.target.files);
              }}
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={uploading || sending}
              title="上传图片（每张最多 20 MiB）"
              aria-label="上传图片"
            >
              {uploading ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <ImagePlus size={18} />
              )}
            </button>
            <button
              className="icon-button"
              onClick={() => setPicker("task")}
              disabled={sending}
              title="引用任务（@）"
              aria-label="引用任务"
            >
              <AtSign size={18} />
            </button>
            <button
              className="icon-button"
              onClick={() => setPicker("web")}
              disabled={sending}
              title="添加网页引用"
              aria-label="添加网页引用"
            >
              <Globe size={18} />
            </button>
            {busy && <span className="composer-mode">补充当前任务</span>}
          </div>
          <details className="composer-settings">
            <summary
              aria-label="运行设置"
              title={settingsLabel({
                ...thread.effective_settings,
                ...thread.execution_settings,
              })}
            >
              <Settings2 size={14} />
              <span>
                {thread.execution_settings?.model ||
                  thread.effective_settings?.model ||
                  "运行设置"}
              </span>
              {(settingsSaving ||
                (thread.settings_revision ?? 0) >
                  (thread.effective_settings_revision ?? 0)) && (
                <i title={settingsSaving ? "保存设置中" : "下次执行生效"} />
              )}
            </summary>
            <div className="composer-settings-panel">
              <ThreadExecutionControls
                thread={thread}
                host={host}
                project={project}
                onSave={onSettingsChange}
                onProjectSettings={onProjectSettings}
                onBusyChange={setSettingsSaving}
              />
            </div>
          </details>
          <div className="composer-actions">
            {busy && (
              <button
                className="interrupt-button"
                title="中断当前执行"
                aria-label="中断当前执行"
                disabled={interrupting || !hostOnline}
                onClick={() => {
                  setInterrupting(true);
                  setError("");
                  void onInterrupt()
                    .catch((reason) => setError(errorText(reason)))
                    .finally(() => setInterrupting(false));
                }}
              >
                {interrupting ? (
                  <LoaderCircle className="spin" size={15} />
                ) : (
                  <Square size={12} fill="currentColor" />
                )}
              </button>
            )}
            <button
              className="send-button"
              title={busy ? "补充要求" : "发送消息"}
              aria-label={busy ? "补充要求" : "发送消息"}
              disabled={
                sending ||
                uploading ||
                settingsSaving ||
                !ready ||
                (!text.trim() && !assets.length && !references.length)
              }
              onClick={() => void send()}
            >
              {sending ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <ArrowUp size={20} />
              )}
            </button>
          </div>
        </div>
      </div>
      {!ready && (
        <div className="composer-footnote">
          <span>
            {!hostOnline
              ? "设备离线，重新连接后可发送消息"
              : !historyReady
                ? "正在导入本机会话历史，完成后可继续原会话。"
                : "正在启动 Codex 会话…"}
          </span>
        </div>
      )}
      {picker === "task" && (
        <TaskPicker
          currentId={thread.id}
          onClose={() => setPicker(null)}
          onSelect={addReference}
        />
      )}
      {picker === "web" && (
        <WebPicker onClose={() => setPicker(null)} onSelect={addReference} />
      )}
    </div>
  );
}
