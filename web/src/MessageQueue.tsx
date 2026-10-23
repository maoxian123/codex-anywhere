import { useState } from "react";
import { LoaderCircle, Pencil, Trash2 } from "lucide-react";
import { api, errorText, post } from "./api";
import { ErrorMessage, References } from "./components";
import type { Command, MessageQueueState, QueuedMessage } from "./types";

export default function MessageQueue({
  threadId,
  queue,
  hostOnline,
  onChange,
  onNavigate,
}: {
  threadId: string;
  queue?: MessageQueueState;
  hostOnline: boolean;
  onChange?: () => void | Promise<void>;
  onNavigate: (id: string, item?: string) => void;
}) {
  const [editing, setEditing] = useState<{
    id: string;
    revision: number;
    text: string;
  } | null>(null);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const messages = queue?.messages || [];
  const orphanEdit =
    editing && !messages.some((message) => message.id === editing.id);
  const path = `/threads/${encodeURIComponent(threadId)}/queue`;
  const change = async (id: string, action: () => Promise<unknown>) => {
    if (pending) return;
    setPending(id);
    setError("");
    try {
      await action();
      setEditing((current) => (current?.id === id ? null : current));
      await onChange?.();
    } catch (reason) {
      setError(errorText(reason));
      // A conflicting revision or dispatched message must refresh the list,
      // while keeping an unsaved edit available for the user to copy.
      void Promise.resolve(onChange?.()).catch(() => {});
    } finally {
      setPending("");
    }
  };
  if (!messages.length && !error && !orphanEdit) return null;
  return (
    <section className="message-queue" aria-label="待发送消息">
      <div className="message-queue-heading">
        <strong>待发送 · {messages.length}</strong>
        <span>
          {queue?.paused
            ? "队列已暂停"
            : hostOnline
              ? "当前轮完成后依次执行"
              : "设备离线，队列已保留"}
        </span>
        {queue?.paused && (
          <button
            className="queue-resume-button"
            disabled={!!pending || !hostOnline}
            onClick={() =>
              void change("resume", () =>
                post<{ command?: Command | null; queue: MessageQueueState }>(
                  `${path}/resume`,
                  {},
                ),
              )
            }
          >
            {pending === "resume" && (
              <LoaderCircle size={13} className="spin" />
            )}
            继续队列
          </button>
        )}
      </div>
      {queue?.paused && (
        <p className="message-queue-note">
          {queue.reason || "上一轮已中断或失败，确认后继续执行队列。"}
        </p>
      )}
      {error && <ErrorMessage>{error}</ErrorMessage>}
      {orphanEdit && (
        <div className="message-queue-content">
          <p className="message-queue-note">
            这条消息已开始执行或被取消，无法继续修改。未保存的内容可复制后重新发送。
          </p>
          <textarea
            aria-label="未保存的排队消息"
            value={editing.text}
            readOnly
            rows={3}
          />
          <button
            className="queue-resume-button"
            onClick={() => {
              setEditing(null);
              setError("");
            }}
          >
            关闭
          </button>
        </div>
      )}
      <ol className="message-queue-list">
        {messages.map((message, index) => {
          const activeEdit = editing?.id === message.id;
          const stale = activeEdit && editing.revision !== message.revision;
          return (
            <li key={message.id}>
              <span className="message-queue-position">{index + 1}</span>
              <div className="message-queue-content">
                {activeEdit ? (
                  <>
                    <textarea
                      aria-label={`编辑第 ${index + 1} 条排队消息`}
                      value={editing.text}
                      disabled={!!pending}
                      onChange={(event) =>
                        setEditing({ ...editing, text: event.target.value })
                      }
                      rows={3}
                    />
                    {stale && (
                      <p className="message-queue-note">
                        这条消息已在其他页面更新，请取消编辑后重新打开。
                      </p>
                    )}
                    <div className="message-queue-edit-actions">
                      <button
                        disabled={
                          !!pending ||
                          stale ||
                          (!editing.text.trim() &&
                            !message.assets.length &&
                            !message.references.length)
                        }
                        onClick={() =>
                          void change(message.id, () =>
                            api<{ message: QueuedMessage }>(
                              `${path}/${encodeURIComponent(message.id)}`,
                              {
                                method: "PATCH",
                                body: JSON.stringify({
                                  text: editing.text.trim(),
                                  revision: editing.revision,
                                }),
                              },
                            ),
                          )
                        }
                      >
                        保存
                      </button>
                      <button
                        disabled={!!pending}
                        onClick={() => setEditing(null)}
                      >
                        取消编辑
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="message-queue-text">
                    {message.text || "图片或引用消息"}
                  </p>
                )}
                {!!message.assets.length && (
                  <div className="message-queue-assets">
                    {message.assets.map((asset) => (
                      <span key={asset.id} title={asset.name}>
                        图片 · {asset.name}
                      </span>
                    ))}
                  </div>
                )}
                <References
                  references={message.references}
                  onNavigate={onNavigate}
                />
              </div>
              <div className="message-queue-actions">
                {!activeEdit && (
                  <button
                    className="icon-button"
                    aria-label={`编辑第 ${index + 1} 条排队消息`}
                    disabled={!!pending}
                    onClick={() => {
                      setError("");
                      setEditing({
                        id: message.id,
                        revision: message.revision,
                        text: message.text,
                      });
                    }}
                  >
                    <Pencil size={14} />
                  </button>
                )}
                <button
                  className="icon-button"
                  aria-label={`取消第 ${index + 1} 条排队消息`}
                  disabled={!!pending}
                  onClick={() =>
                    void change(message.id, () =>
                      api<{ cancelled: boolean }>(
                        `${path}/${encodeURIComponent(message.id)}`,
                        {
                          method: "DELETE",
                          body: JSON.stringify({ revision: message.revision }),
                        },
                      ),
                    )
                  }
                >
                  {pending === message.id ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Trash2 size={14} />
                  )}
                </button>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
