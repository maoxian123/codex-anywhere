import { api, post } from "./api";
import { commandFailure } from "./runtime-errors";
import type { Command, Thread, ThreadDetail } from "./types";

function aborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException("请求已取消", "AbortError");
}
function pause(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    aborted(signal);
    const cancel = () => {
      clearTimeout(timer);
      reject(new DOMException("请求已取消", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", cancel);
      resolve();
    }, ms);
    signal?.addEventListener("abort", cancel, { once: true });
  });
}
/** Poll an already journaled command. Never resubmit an uncertain operation. */
export async function waitForCommand<T>(
  command: Command,
  signal?: AbortSignal,
  timeout = 180000,
): Promise<T> {
  const deadline = Date.now() + timeout;
  let value = command;
  while (true) {
    aborted(signal);
    const failure = commandFailure(value);
    if (failure) throw new Error(failure);
    if (["completed", "succeeded", "success"].includes(value.status))
      return value.result as T;
    if (Date.now() >= deadline)
      throw new Error(
        "等待远程设备超时，已提交的操作可能仍在运行。请刷新状态后再决定下一步。",
      );
    await pause(450, signal);
    aborted(signal);
    try {
      value = (
        await api<{ command: Command }>(
          `/commands/${encodeURIComponent(value.id)}`,
          { signal },
        )
      ).command;
    } catch (error) {
      aborted(signal);
      throw error;
    }
  }
}
export async function remoteCommand<T>(
  path: string,
  payload: unknown,
  signal?: AbortSignal,
): Promise<T> {
  aborted(signal);
  const { command } = await post<{ command: Command }>(path, payload);
  aborted(signal);
  return waitForCommand<T>(command, signal);
}

/** Command completion means the node journal is written; import projection may lag. */
export async function waitForImportedThread(
  id: string,
  signal?: AbortSignal,
  timeout = 180000,
): Promise<Thread> {
  const deadline = Date.now() + timeout;
  while (true) {
    aborted(signal);
    let detail: ThreadDetail;
    try {
      detail = await api<ThreadDetail>(`/threads/${encodeURIComponent(id)}`, {
        signal,
      });
    } catch (error) {
      aborted(signal);
      throw error;
    }
    aborted(signal);
    if (detail.thread.last_error || detail.thread.status === "error")
      throw new Error(
        detail.thread.last_error || "会话历史导入失败，请检查节点状态。",
      );
    if (
      detail.thread.history_loaded === true &&
      detail.thread.status !== "importing"
    )
      return detail.thread;
    if (Date.now() >= deadline)
      throw new Error(
        "等待历史同步超时。已提交的导入仍可能继续，请刷新任务状态。",
      );
    await pause(450, signal);
  }
}
