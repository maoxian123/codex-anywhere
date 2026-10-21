import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorText } from "./api";
import {
  RemoteCommandError,
  waitForCommand,
  waitForImportedThread,
} from "./commands";
import type { Command, Host, Thread } from "./types";

export function useAutomaticHistory({
  selectionId,
  thread,
  host,
  refresh,
  onError,
}: {
  selectionId: string;
  thread?: Thread;
  host?: Host;
  refresh: (id?: string) => Promise<void>;
  onError: (error: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const attempts = useRef(new Set<string>());
  const blocked = useRef(new Set<string>());
  const job = useRef<{ id: string; controller: AbortController } | null>(null);
  const currentId = useRef(selectionId);
  currentId.current = selectionId;
  useEffect(() => {
    attempts.current.clear();
    setBusy(false);
    onError("");
    return () => {
      job.current?.controller.abort();
      job.current = null;
    };
  }, [selectionId, onError]);
  useEffect(() => {
    if (
      !thread ||
      selectionId !== thread.id ||
      !host?.online ||
      thread.status !== "idle" ||
      (thread.history_loaded !== false && !thread.history_stale) ||
      job.current
    )
      return;
    const nativeVersion = JSON.stringify([
      thread.id,
      thread.native_updated_at ?? null,
    ]);
    const key = JSON.stringify([
      nativeVersion,
      thread.history_synced_at ?? null,
      thread.history_native_updated_at ?? null,
      host.last_sync_at ?? null,
    ]);
    if (attempts.current.has(key) || blocked.current.has(nativeVersion)) return;
    attempts.current.add(key);
    const controller = new AbortController();
    job.current = { id: thread.id, controller };
    setBusy(true);
    onError("");
    void (async () => {
      try {
        const result = await api<{ thread: Thread; command: Command | null }>(
          `/threads/${encodeURIComponent(thread.id)}/history`,
          { method: "POST", body: "{}", signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        if (result.command) {
          await waitForCommand(result.command, controller.signal);
          await waitForImportedThread(thread.id, controller.signal);
        }
        if (!controller.signal.aborted && currentId.current === thread.id)
          await refresh(thread.id);
      } catch (reason) {
        if (controller.signal.aborted || currentId.current !== thread.id)
          return;
        if (
          (reason instanceof ApiError && reason.status === 409) ||
          (reason instanceof RemoteCommandError &&
            reason.command.status === "unknown")
        )
          blocked.current.add(nativeVersion);
        onError(errorText(reason));
      } finally {
        if (job.current?.controller === controller) {
          job.current = null;
          if (currentId.current === thread.id) setBusy(false);
        }
      }
    })();
  }, [
    selectionId,
    thread?.id,
    thread?.status,
    thread?.history_loaded,
    thread?.history_stale,
    thread?.native_updated_at,
    thread?.history_synced_at,
    thread?.history_native_updated_at,
    host?.online,
    host?.last_sync_at,
    busy,
    refresh,
    onError,
  ]);
  return busy;
}
