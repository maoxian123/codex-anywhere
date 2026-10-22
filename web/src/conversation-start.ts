import { api, ApiError, post } from "./api";
import { waitForCommand } from "./commands";
import type {
  Command,
  ExecutionSettings,
  MessagePayload,
  Project,
  Thread,
  ThreadDetail,
} from "./types";

type PendingStart = {
  request: {
    project_id: string;
    client_request_id: string;
    title: string;
    execution_settings: ExecutionSettings;
  };
  thread?: Thread;
  command?: Command;
};
const pendingMemory = new Map<string, PendingStart>();
const storageKey = (projectId: string, scope: string) =>
  `anywhere:new-start:${encodeURIComponent(scope)}:${projectId}`;

export function pendingConversation(
  projectId: string,
  scope: string,
): PendingStart | null {
  const key = storageKey(projectId, scope);
  try {
    const stored = sessionStorage.getItem(key);
    if (stored) {
      const value = JSON.parse(stored) as PendingStart;
      if (
        value.request?.project_id === projectId &&
        typeof value.request.client_request_id === "string"
      )
        return value;
    }
  } catch {
    /* A restricted browser still retains the request in this tab. */
  }
  return pendingMemory.get(key) || null;
}

function savePending(
  projectId: string,
  scope: string,
  value: PendingStart | null,
) {
  const key = storageKey(projectId, scope);
  if (value) pendingMemory.set(key, value);
  else pendingMemory.delete(key);
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch {
    /* Preserve the in-memory copy when storage is unavailable. */
  }
}

/** Forget only this page's pending creation; the original native task is kept. */
export function abandonPendingConversation(projectId: string, scope: string) {
  savePending(projectId, scope, null);
}

/** Only poll journaled commands; an uncertain creation reuses its original key. */
export async function startConversation(
  project: Project,
  settings: ExecutionSettings,
  payload: MessagePayload,
  scope: string,
  onPending: (thread?: Thread) => void,
): Promise<{ thread: Thread; command: Command }> {
  const pending = pendingConversation(project.id, scope) || {
    request: {
      project_id: project.id,
      client_request_id: payload.client_request_id,
      title:
        payload.text
          .split(/\r?\n/)
          .find((line) => line.trim())
          ?.trim()
          .slice(0, 80) || (payload.asset_ids.length ? "图片任务" : "新对话"),
      execution_settings: settings,
    },
  };
  savePending(project.id, scope, pending);
  if (!pending.thread || !pending.command) {
    let result: { thread: Thread; command: Command };
    try {
      result = await post<{ thread: Thread; command: Command }>(
        "/threads",
        pending.request,
      );
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        pendingConversation(project.id, scope)?.request.client_request_id ===
          pending.request.client_request_id
      )
        savePending(project.id, scope, null);
      throw error;
    }
    pending.thread = result.thread;
    pending.command = result.command;
    if (
      pendingConversation(project.id, scope)?.request.client_request_id ===
      pending.request.client_request_id
    )
      savePending(project.id, scope, pending);
  }
  onPending(pending.thread);
  await waitForCommand(pending.command);
  // The command result can precede the durable thread-started projection.
  const deadline = Date.now() + 30000;
  let thread = pending.thread;
  while (true) {
    thread = (
      await api<ThreadDetail>(
        `/threads/${encodeURIComponent(thread.id)}?item_limit=1`,
      )
    ).thread;
    if (thread.last_error || thread.status === "error")
      throw new Error(
        thread.last_error || "会话启动失败，请打开会话查看详情。",
      );
    if (thread.native_id && thread.status !== "starting") break;
    if (Date.now() >= deadline)
      throw new Error(
        "会话仍在启动，草稿已保留。再次发送会检查原有会话的进度。",
      );
    await new Promise<void>((resolve) => setTimeout(resolve, 450));
  }
  const { command } = await post<{ command: Command }>(
    `/threads/${encodeURIComponent(thread.id)}/messages`,
    payload,
  );
  await waitForCommand(command);
  if (
    pendingConversation(project.id, scope)?.request.client_request_id ===
    pending.request.client_request_id
  )
    savePending(project.id, scope, null);
  return { thread, command };
}
