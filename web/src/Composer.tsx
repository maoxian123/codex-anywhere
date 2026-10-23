import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  ArrowUp,
  ListPlus,
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
import {
  ThreadExecutionControls,
  ThreadServiceTierControl,
} from "./ExecutionControls";
import { permissionLabel, settingsLabel } from "./execution-settings";
import type {
  Asset,
  Host,
  Project,
  ExecutionSettings,
  MessagePayload,
  MessageQueueState,
  Reference,
  Thread,
  ThreadDetail,
} from "./types";
import { fileName, safeLink } from "./utils";
import { RemoteCommandError } from "./commands";
import MessageQueue from "./MessageQueue";
import {
  clearComposerDraft,
  emptyComposerDraft,
  readComposerDraft,
  subscribeComposerDraft,
  taskMentionAtCaret,
  writeComposerDraft,
  type ComposerDraft,
  type TaskMention,
} from "./composer-drafts";

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
      title: thread.display_title || thread.title,
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
            <strong>
              {detail.thread.display_title || detail.thread.title}
            </strong>
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
                      <strong>
                        {thread.display_title || thread.title || "未命名任务"}
                      </strong>
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

type ComposerProps = {
  thread: Thread;
  hostOnline: boolean;
  onSend: (payload: MessagePayload, steer: boolean) => Promise<void>;
  onInterrupt: () => Promise<void>;
  onNavigate: (id: string, item?: string) => void;
  host?: Host;
  project?: Project;
  onSettingsChange?: (settings: ExecutionSettings) => Promise<void>;
  onProjectSettings?: (project: Project) => void;
  draftScope?: string;
  hideSettings?: boolean;
  autoFocus?: boolean;
  queue?: MessageQueueState;
  onQueueChange?: () => void | Promise<void>;
};

export default function Composer(props: ComposerProps) {
  return (
    <ComposerInput
      key={JSON.stringify([props.draftScope || "", props.thread.id])}
      {...props}
    />
  );
}

function ComposerInput({
  thread,
  hostOnline,
  onSend,
  onInterrupt,
  onNavigate,
  host,
  project,
  onSettingsChange,
  onProjectSettings,
  draftScope = "",
  hideSettings = false,
  autoFocus = false,
  queue,
  onQueueChange,
}: ComposerProps) {
  const [draft, setDraft] = useState(() =>
    readComposerDraft(thread.id, draftScope),
  );
  const draftRef = useRef(draft);
  const mountedRef = useRef(true);
  const sendingRef = useRef(false);
  const { text, assets, references, retryRequestId } = draft;
  const updateDraft = (change: (current: ComposerDraft) => ComposerDraft) => {
    const next = change(
      mountedRef.current
        ? draftRef.current
        : readComposerDraft(thread.id, draftScope),
    );
    draftRef.current = next;
    writeComposerDraft(thread.id, next, draftScope);
    if (mountedRef.current) setDraft(next);
  };
  useEffect(() => {
    mountedRef.current = true;
    const unsubscribe = subscribeComposerDraft(
      thread.id,
      (next) => {
        draftRef.current = next;
        setDraft(next);
      },
      draftScope,
    );
    return () => {
      mountedRef.current = false;
      unsubscribe();
    };
  }, [thread.id, draftScope]);
  const [uploading, setUploading] = useState(false);
  const [sending, setSending] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [interrupting, setInterrupting] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [picker, setPicker] = useState<"task" | "web" | null>(null);
  const [mention, setMention] = useState<TaskMention | null>(null);
  const [mentionResults, setMentionResults] = useState<Thread[]>([]);
  const [mentionLoading, setMentionLoading] = useState(false);
  const [mentionError, setMentionError] = useState("");
  const [activeMention, setActiveMention] = useState(0);
  const dismissedMention = useRef("");
  const composingRef = useRef(false);
  const suggestionsId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (autoFocus && window.innerWidth > 680) textRef.current?.focus();
  }, [autoFocus]);
  const busy = ["active", "waiting"].includes(thread.status);
  const queueing = busy || !!queue?.messages.length;
  const historyReady =
    thread.history_loaded !== false &&
    thread.status !== "importing" &&
    !(thread.status === "idle" && thread.history_stale);
  const ready = hostOnline && thread.status !== "starting" && historyReady;
  const pendingSettings =
    (thread.settings_revision ?? 0) > (thread.effective_settings_revision ?? 0);
  const nextSettings = {
    ...thread.effective_settings,
    ...thread.execution_settings,
  };
  useEffect(() => {
    if (!mention) return;
    let cancelled = false;
    setMentionResults([]);
    setMentionLoading(true);
    setMentionError("");
    setActiveMention(0);
    const timer = setTimeout(() => {
      void api<{ threads: Thread[] }>(
        `/threads?q=${encodeURIComponent(mention.query)}`,
      )
        .then((value) => {
          if (!cancelled)
            setMentionResults(
              value.threads
                .filter((value) => value.id !== thread.id)
                .slice(0, 6),
            );
        })
        .catch((reason) => {
          if (!cancelled) setMentionError(errorText(reason));
        })
        .finally(() => {
          if (!cancelled) setMentionLoading(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [mention?.query, thread.id]);
  useEffect(() => {
    const input = textRef.current;
    if (input) {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    }
  }, [text]);
  const updateMention = (value: string, caret: number) => {
    const next = composingRef.current ? null : taskMentionAtCaret(value, caret);
    setMention(
      next && JSON.stringify(next) !== dismissedMention.current ? next : null,
    );
  };
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
        updateDraft((current) => ({
          ...current,
          assets: [...current.assets, result.asset],
          retryRequestId: null,
        }));
      }
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };
  const send = async (steer = false) => {
    if (
      (!text.trim() && !assets.length && !references.length) ||
      sendingRef.current ||
      uploading ||
      settingsSaving ||
      !ready
    )
      return;
    sendingRef.current = true;
    setSending(true);
    setMention(null);
    setError("");
    const requestId = retryRequestId || crypto.randomUUID();
    updateDraft((current) => ({ ...current, retryRequestId: requestId }));
    const sentDraft = JSON.stringify(draftRef.current);
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
        steer,
      );
      // A send can finish after navigating away. Never erase a newer draft,
      // and never persist an unmounted component's old state during cleanup.
      if (
        JSON.stringify(readComposerDraft(thread.id, draftScope)) === sentDraft
      ) {
        clearComposerDraft(thread.id, draftScope);
        if (mountedRef.current) {
          draftRef.current = emptyComposerDraft();
          setDraft(draftRef.current);
        }
      }
    } catch (reason) {
      if (
        reason instanceof RemoteCommandError &&
        ["failed", "error", "cancelled"].includes(reason.command.status)
      ) {
        if (
          JSON.stringify(readComposerDraft(thread.id, draftScope)) === sentDraft
        )
          updateDraft((current) => ({ ...current, retryRequestId: null }));
      }
      setError(errorText(reason));
    } finally {
      sendingRef.current = false;
      if (mountedRef.current) {
        setSending(false);
        textRef.current?.focus();
      }
    }
  };
  const addReference = (reference: Reference) => {
    updateDraft((current) => ({
      ...current,
      retryRequestId: null,
      references: current.references.some(
        (existing) =>
          existing.kind === reference.kind &&
          (reference.kind === "task"
            ? existing.thread_id === reference.thread_id &&
              existing.item_id === reference.item_id
            : existing.url === reference.url),
      )
        ? current.references
        : [...current.references, reference],
    }));
  };
  const selectMention = (selected: Thread) => {
    if (!mention) return;
    addReference({
      kind: "task",
      thread_id: selected.id,
      title: selected.display_title || selected.title,
    });
    updateDraft((current) => ({
      ...current,
      text:
        current.text.slice(0, mention.start) + current.text.slice(mention.end),
      retryRequestId: null,
    }));
    const caret = mention.start;
    setMention(null);
    setTimeout(() => {
      textRef.current?.focus();
      textRef.current?.setSelectionRange(caret, caret);
    }, 0);
  };
  return (
    <div className="composer-area">
      <MessageQueue
        key={thread.id}
        threadId={thread.id}
        queue={queue}
        hostOnline={hostOnline}
        onChange={onQueueChange}
        onNavigate={onNavigate}
      />
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
                if (sendingRef.current) return;
                updateDraft((current) => ({
                  ...current,
                  references: current.references.filter(
                    (_, itemIndex) => itemIndex !== index,
                  ),
                  retryRequestId: null,
                }));
              }}
            />
            {assets.length > 0 && (
              <div className="upload-previews">
                {assets.map((asset) => (
                  <div className="upload-preview" key={asset.id}>
                    <img src={asset.url} alt={asset.name} />
                    <button
                      aria-label={`移除图片 ${asset.name}`}
                      disabled={sending}
                      onClick={() => {
                        updateDraft((current) => ({
                          ...current,
                          assets: current.assets.filter(
                            (value) => value.id !== asset.id,
                          ),
                          retryRequestId: null,
                        }));
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
        {mention && (
          <div className="composer-mention-panel">
            <div className="composer-mention-heading">
              <span>引用任务</span>
              <span>↑ ↓ 选择 · Enter 引用 · Esc 关闭</span>
            </div>
            <div id={suggestionsId} role="listbox" aria-label="引用任务候选">
              {mentionLoading ? (
                <p className="empty-small">搜索中…</p>
              ) : mentionError ? (
                <p className="empty-small">
                  暂时无法搜索，请使用引用按钮重试。
                </p>
              ) : mentionResults.length ? (
                mentionResults.map((value, index) => (
                  <button
                    type="button"
                    role="option"
                    tabIndex={-1}
                    id={`${suggestionsId}-${index}`}
                    aria-selected={index === activeMention}
                    key={value.id}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setActiveMention(index)}
                    onClick={() => selectMention(value)}
                  >
                    <FileCode2 size={16} />
                    <span>
                      <strong>
                        {value.display_title || value.title || "未命名任务"}
                      </strong>
                      <small>{fileName(value.cwd)}</small>
                    </span>
                  </button>
                ))
              ) : (
                <p className="empty-small">
                  没有匹配任务，继续输入也可保留普通 @ 文本。
                </p>
              )}
            </div>
          </div>
        )}
        <textarea
          ref={textRef}
          value={text}
          onChange={(event) => {
            const value = event.target.value;
            updateDraft((current) => ({
              ...current,
              text: value,
              retryRequestId: null,
            }));
            dismissedMention.current = "";
            updateMention(value, event.target.selectionStart);
          }}
          onSelect={(event) =>
            updateMention(
              event.currentTarget.value,
              event.currentTarget.selectionStart,
            )
          }
          onBlur={() => setMention(null)}
          onCompositionStart={() => {
            composingRef.current = true;
            setMention(null);
          }}
          onCompositionEnd={(event) => {
            composingRef.current = false;
            updateMention(
              event.currentTarget.value,
              event.currentTarget.selectionStart,
            );
          }}
          placeholder={
            !historyReady
              ? "正在导入历史，完成后即可继续对话…"
              : queueing
                ? queue?.paused
                  ? "输入下一条消息，继续队列后依次执行…"
                  : "输入下一条消息，当前任务完成后依次执行…"
                : "描述你想完成的工作…"
          }
          aria-label={queueing ? "排队消息" : "发送消息给 Codex"}
          aria-autocomplete="list"
          aria-expanded={!!mention}
          aria-controls={mention ? suggestionsId : undefined}
          aria-activedescendant={
            mention && mentionResults[activeMention]
              ? `${suggestionsId}-${activeMention}`
              : undefined
          }
          rows={1}
          title={
            queueing
              ? "Enter 加入队列 · Shift+Enter 换行"
              : "Enter 发送 · Shift+Enter 换行"
          }
          disabled={sending}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (mention && event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              dismissedMention.current = JSON.stringify(mention);
              setMention(null);
              return;
            }
            if (
              mention &&
              mentionResults.length &&
              ["ArrowDown", "ArrowUp"].includes(event.key)
            ) {
              event.preventDefault();
              setActiveMention(
                (current) =>
                  (current +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    mentionResults.length) %
                  mentionResults.length,
              );
              return;
            }
            if (
              mention &&
              mentionResults[activeMention] &&
              event.key === "Enter" &&
              !event.shiftKey
            ) {
              event.preventDefault();
              selectMention(mentionResults[activeMention]);
              return;
            }
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void send();
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
              onClick={() => {
                setMention(null);
                setPicker("task");
              }}
              disabled={sending}
              title="引用任务，也可在空白后输入 @ 搜索"
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
          </div>
          <div className="composer-runtime-summary">
            {!hideSettings && (
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
                    {pendingSettings ? "下次：" : ""}
                    {nextSettings.model || "默认模型"}
                  </span>
                  <span className="composer-effort">
                    {nextSettings.effort || "默认强度"}
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
                    hideServiceTier
                    disabled={settingsSaving || sending}
                  />
                </div>
              </details>
            )}
            {hideSettings && (
              <span className="composer-model-label">
                {nextSettings.model || "默认模型"} ·{" "}
                {nextSettings.effort || "默认强度"}
              </span>
            )}
            <ThreadServiceTierControl
              thread={thread}
              host={
                host
                  ? { ...host, online: hostOnline }
                  : {
                      id: thread.host_id,
                      name: "",
                      platform: "",
                      online: hostOnline,
                    }
              }
              disabled={settingsSaving || sending}
              onSave={onSettingsChange}
              onBusyChange={setSettingsSaving}
            />
          </div>
          <div className="composer-actions">
            {busy && (
              <button
                className="composer-steer-button"
                type="button"
                title="立即交给正在运行的任务，不加入队列"
                disabled={
                  sending ||
                  uploading ||
                  settingsSaving ||
                  !ready ||
                  (!text.trim() && !assets.length && !references.length)
                }
                onClick={() => void send(true)}
              >
                立即补充
              </button>
            )}
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
              title={queueing ? "加入队列" : "发送消息"}
              aria-label={queueing ? "加入队列" : "发送消息"}
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
              ) : queueing ? (
                <ListPlus size={20} />
              ) : (
                <ArrowUp size={20} />
              )}
            </button>
          </div>
        </div>
      </div>
      <div className="composer-context-line">
        <span title={settingsLabel(thread.effective_settings)}>
          {thread.id.startsWith("new:") ? "发送权限：" : "当前权限："}
          {permissionLabel(
            thread.id.startsWith("new:")
              ? nextSettings
              : thread.effective_settings,
          )}
        </span>
        {pendingSettings && (
          <span
            className="composer-pending-settings"
            title={settingsLabel(nextSettings)}
          >
            下次执行：{settingsLabel(nextSettings)}
          </span>
        )}
        <span className="composer-keyboard-hint">
          {queueing ? "Enter 加入队列" : "Enter 发送"} · Shift+Enter 换行
        </span>
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
