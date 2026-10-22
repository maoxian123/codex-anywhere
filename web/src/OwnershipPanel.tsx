import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  ChevronDown,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { ErrorMessage, Modal } from "./components";
import type { ThreadOwnership } from "./types";

const labels = {
  managed: "已托管",
  available: "可接管",
  external: "其他进程占用",
  unknown: "状态待确认",
};
const descriptions = {
  managed: "当前会话由本节点托管，可以继续对话，也可以手动释放。",
  available: "当前没有其他进程持有此会话，可以直接接管。",
  external: "其他 Codex 进程正在使用此会话。请先查看进程信息，再决定是否接管。",
  unknown: "暂时无法确认当前占用情况，请重新检查或在设备上退出原 Codex 进程。",
};
const translatedReasons: Record<string, string> = {
  "Windows identified one Codex resource user, not a proven lock holder. Ending this entire process may interrupt its other conversations":
    "Windows 检测到一个使用会话文件的 Codex 进程，但无法证明它是实际持锁者。结束整个进程可能中断其承载的其他会话。",
  "Windows found multiple resource users; the actual writer cannot be identified safely":
    "Windows 检测到多个使用会话文件的进程，无法安全确认实际持锁者。",
  "The writer owner could not be fully verified; exit Codex locally and inspect again":
    "无法完整验证占用进程，请在设备上退出原 Codex 进程后重新检查。",
  "Only verified native Codex processes belonging to the node account can be ended":
    "只能结束已验证身份、且属于当前节点账户的原生 Codex 进程。",
};
function ownerStartedAt(value: string | number) {
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export default function OwnershipPanel({
  ownership,
  pending,
  error,
  notice,
  onCheck,
  onTakeover,
  onRelease,
}: {
  ownership: ThreadOwnership | null;
  pending: boolean;
  error: string;
  notice: string;
  onCheck: () => void;
  onTakeover: (confirmed?: boolean) => Promise<void>;
  onRelease: () => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const [position, setPosition] = useState({ left: 16, top: 56 });
  const [confirmation, setConfirmation] = useState<ThreadOwnership | null>(
    null,
  );
  const wasConfirming = useRef(false);
  const transferable =
    ownership?.state === "external" &&
    ownership.can_terminate &&
    !!ownership.owner_token;
  const label = ownership ? labels[ownership.state] : "检查托管";
  const failure = error || ownership?.release_error || "";
  const reason = ownership?.reason
    ? translatedReasons[ownership.reason] || ownership.reason
    : "";
  useEffect(() => {
    if (wasConfirming.current && !confirmation) triggerRef.current?.focus();
    wasConfirming.current = Boolean(confirmation);
  }, [confirmation]);
  useLayoutEffect(() => {
    if (!open) return;
    const positionPopover = () => {
      const trigger = triggerRef.current?.getBoundingClientRect();
      if (!trigger) return;
      const width = Math.min(360, window.innerWidth - 32);
      setPosition({
        left: Math.max(
          16,
          Math.min(trigger.right - width, window.innerWidth - width - 16),
        ),
        top: trigger.bottom + 8,
      });
    };
    positionPopover();
    window.addEventListener("resize", positionPopover);
    window.addEventListener("scroll", positionPopover, true);
    return () => {
      window.removeEventListener("resize", positionPopover);
      window.removeEventListener("scroll", positionPopover, true);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    popoverRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const outside = (event: Event) => {
      const target = event.target as Node | null;
      if (
        target &&
        !triggerRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      )
        setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div className="ownership-control">
      <button
        ref={triggerRef}
        className={`ownership-trigger ownership-${ownership?.state || "unchecked"} ${failure ? "ownership-needs-attention" : ""}`}
        aria-label={`会话托管：${label}${failure ? "，需处理" : notice ? "，有提示" : ""}`}
        aria-expanded={open}
        aria-controls={panelId}
        aria-haspopup="dialog"
        title={failure || notice || "查看会话托管与接管操作"}
        onClick={() => setOpen((value) => !value)}
      >
        {pending ? (
          <LoaderCircle className="spin" size={14} aria-label="处理中" />
        ) : (
          <ShieldCheck size={14} aria-hidden="true" />
        )}
        <span>{label}</span>
        {failure ? (
          <AlertCircle size={13} aria-hidden="true" />
        ) : notice ? (
          <span className="ownership-notice-dot" aria-hidden="true" />
        ) : null}
        <ChevronDown
          className="ownership-chevron"
          size={12}
          aria-hidden="true"
        />
      </button>
      <span className="sr-only" role="status" aria-live="polite">
        {failure
          ? `托管需处理：${failure}`
          : notice
            ? `托管提示：${notice}`
            : ""}
      </span>
      {open &&
        createPortal(
          <div
            ref={popoverRef}
            id={panelId}
            className="ownership-popover"
            role="dialog"
            aria-label="会话托管"
            style={position}
          >
            <div className="ownership-popover-heading">
              <h2>会话托管</h2>
              <button
                className="icon-button"
                aria-label="关闭托管详情"
                onClick={() => {
                  setOpen(false);
                  triggerRef.current?.focus();
                }}
              >
                <X size={15} />
              </button>
            </div>
            <p className="ownership-state-description">
              {ownership
                ? descriptions[ownership.state]
                : "检查当前会话是否由其他 Codex 进程持有。"}
            </p>
            {ownership?.owners.length ? (
              <div className="ownership-processes">
                <p>
                  {ownership.owner_kind === "candidate"
                    ? "使用会话文件的进程 · 尚未确认持锁"
                    : "占用进程"}
                </p>
                <ul>
                  {ownership.owners.map((owner) => (
                    <li key={owner.pid}>
                      <strong>{owner.name}</strong>
                      <span>PID {owner.pid}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {reason && <p className="ownership-reason">{reason}</p>}
            {notice && <p className="ownership-notice">{notice}</p>}
            {failure && <ErrorMessage>{failure}</ErrorMessage>}
            <div className="ownership-popover-actions">
              <button
                className="button button-small"
                disabled={pending}
                onClick={onCheck}
              >
                <RefreshCw size={13} />
                重新检查
              </button>
              {ownership?.state === "managed" && (
                <button
                  className="button button-small"
                  disabled={pending}
                  onClick={onRelease}
                >
                  释放托管
                </button>
              )}
              {ownership?.state === "available" && (
                <button
                  className="button button-small"
                  disabled={pending}
                  onClick={() => void onTakeover()}
                >
                  接管会话
                </button>
              )}
              {transferable && (
                <button
                  className="button button-small"
                  disabled={pending}
                  onClick={() => {
                    setOpen(false);
                    setConfirmation(ownership);
                  }}
                >
                  结束进程并接管
                </button>
              )}
            </div>
            <p className="ownership-auto-release">
              离开当前会话或连续 5
              分钟没有操作后释放托管；正在执行的任务会在完成后释放。
            </p>
          </div>,
          document.body,
        )}
      {confirmation && (
        <Modal title="结束占用进程并接管" onClose={() => setConfirmation(null)}>
          <div className="form-stack">
            <p>
              {confirmation.owner_kind === "candidate"
                ? "以下进程正在使用该会话文件，系统无法证明它是唯一持锁者。"
                : "将结束以下持有该会话的进程："}
            </p>
            <ul>
              {confirmation.owners.map((owner) => (
                <li key={owner.pid}>
                  {owner.name} · PID {owner.pid}
                  {owner.started_at
                    ? ` · 启动时间 ${ownerStartedAt(owner.started_at)}`
                    : ""}
                </li>
              ))}
            </ul>
            <p>
              结束整个 Codex
              进程可能中断它承载的其他会话及正在执行的任务。接管会在确认进程身份并等待锁释放后继续；失败的消息不会自动重发。
            </p>
            <div className="modal-actions">
              <button
                className="button button-quiet"
                onClick={() => setConfirmation(null)}
              >
                取消
              </button>
              <button
                className="button button-primary"
                disabled={
                  pending ||
                  !transferable ||
                  confirmation.owner_token !== ownership?.owner_token
                }
                onClick={() => {
                  setConfirmation(null);
                  void onTakeover(true);
                }}
              >
                确认结束并接管
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
