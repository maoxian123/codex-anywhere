import { useState } from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { ErrorMessage, Modal, Spinner } from "./components";
import type { ThreadOwnership } from "./types";

const labels = {
  managed: "本项目托管",
  available: "可接管",
  external: "其他进程占用",
  unknown: "占用状态未确定",
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
  const [confirmation, setConfirmation] = useState<ThreadOwnership | null>(
    null,
  );
  const transferable =
    ownership?.state === "external" &&
    ownership.can_terminate &&
    !!ownership.owner_token;
  return (
    <section className="ownership-panel" aria-label="会话托管">
      <div className="ownership-summary">
        <ShieldCheck size={15} />
        <strong>{ownership ? labels[ownership.state] : "检查会话占用"}</strong>
        {pending && <Spinner label="处理中" />}
        <div className="ownership-actions">
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
              onClick={() => setConfirmation(ownership)}
            >
              结束进程并接管
            </button>
          )}
        </div>
      </div>
      {ownership?.owners.length ? (
        <p>
          {ownership.owner_kind === "candidate"
            ? "检测到使用该会话文件的 Codex 进程："
            : "占用进程："}
          {ownership.owners
            .map((owner) => `${owner.name}（PID ${owner.pid}）`)
            .join("、")}
        </p>
      ) : null}
      {ownership?.reason && <p>{ownership.reason}</p>}
      <p className="ownership-hint">
        离开当前会话或连续 5
        分钟没有操作后释放托管；正在执行的任务会在完成后释放。
      </p>
      {notice && <p role="status">{notice}</p>}
      {(error || ownership?.release_error) && (
        <ErrorMessage>{error || ownership?.release_error}</ErrorMessage>
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
                  pending || confirmation.owner_token !== ownership?.owner_token
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
    </section>
  );
}
