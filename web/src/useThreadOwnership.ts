import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, post } from "./api";
import { waitForCommand } from "./commands";
import {
  sendManualThreadRelease,
  ThreadActionIdentity,
} from "./thread-presence";
import type { Command, ThreadOwnership } from "./types";

export function writerConflict(error: string) {
  return /already has an active writer|active writer|writer lock/i.test(error);
}

export function useThreadOwnership(threadId: string, enabled: boolean) {
  const [actions] = useState(
    () => new ThreadActionIdentity(crypto.randomUUID()),
  );
  const [ownership, setOwnership] = useState<ThreadOwnership | null>(null);
  const [error, setError] = useState("");
  const [checkError, setCheckError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [checking, setChecking] = useState(false);
  const current = useRef({ threadId, enabled });
  current.current = { threadId, enabled };
  const request = useRef(0);
  const checkAbort = useRef<AbortController | null>(null);

  const check = useCallback(async () => {
    const target = current.current;
    if (!target.enabled || !target.threadId) return;
    const serial = ++request.current;
    checkAbort.current?.abort();
    const controller = new AbortController();
    checkAbort.current = controller;
    setChecking(true);
    try {
      const { command } = await api<{ command: Command }>(
        `/threads/${encodeURIComponent(target.threadId)}/ownership`,
        { method: "POST", body: "{}", signal: controller.signal },
      );
      const value = await waitForCommand<ThreadOwnership>(
        command,
        controller.signal,
      );
      if (serial === request.current && !controller.signal.aborted) {
        setOwnership(value);
        setCheckError("");
      }
    } catch (reason) {
      if (serial === request.current && !controller.signal.aborted)
        setCheckError(errorText(reason));
    } finally {
      if (serial === request.current && !controller.signal.aborted)
        setChecking(false);
    }
  }, []);

  useEffect(() => {
    setOwnership(null);
    setError("");
    setCheckError("");
    setNotice("");
    setPending(false);
    setChecking(false);
    if (!enabled || !threadId) return;
    void check();
    const onShow = () => void check();
    window.addEventListener("pageshow", onShow);
    return () => {
      window.removeEventListener("pageshow", onShow);
      request.current += 1;
      checkAbort.current?.abort();
    };
  }, [threadId, enabled, check]);

  const release = useCallback(async () => {
    const id = current.current.threadId;
    if (!current.current.enabled || !id) return;
    setPending(true);
    setError("");
    setNotice("");
    try {
      const result = await sendManualThreadRelease(id, actions.payload());
      const value = result.command
        ? await waitForCommand<{
            released: boolean;
            deferred: boolean;
            reason?: string;
            error?: string;
            retained?: boolean;
            stale?: boolean;
            ownership?: ThreadOwnership;
          }>(result.command)
        : undefined;
      if (current.current.threadId !== id) return;
      if (value?.ownership) setOwnership(value.ownership);
      if (
        value?.error ||
        (value?.released === false &&
          !value.deferred &&
          !value.retained &&
          !value.stale)
      )
        throw new Error(
          value.error || value.reason || "释放托管失败，请重新检查占用状态。",
        );
      setNotice(
        value?.deferred
          ? value.reason || "当前任务仍在运行或等待处理，暂不释放托管。"
          : value?.retained
            ? value.reason || "节点暂时保留托管，请重新检查占用状态。"
            : value?.stale
              ? value.reason || "操作请求已过期，请重新检查托管状态。"
              : value?.released
                ? "已释放托管。下次发送消息或接管时将重新启用托管。"
                : "释放请求已提交，请重新检查会话占用状态。",
      );
      setError("");
      await check();
    } catch (reason) {
      if (current.current.threadId === id) setError(errorText(reason));
    } finally {
      if (current.current.threadId === id) setPending(false);
    }
  }, [actions, check]);
  const takeover = useCallback(
    async (confirmed = false) => {
      const id = current.current.threadId;
      if (!id || !current.current.enabled) return;
      setPending(true);
      setError("");
      setNotice("");
      try {
        const { command } = await post<{ command: Command }>(
          `/threads/${encodeURIComponent(id)}/takeover`,
          {
            ...actions.payload(),
            ...(ownership?.owner_token
              ? { owner_token: ownership.owner_token }
              : {}),
            ...(confirmed ? { confirmed: true } : {}),
            client_request_id: crypto.randomUUID(),
          },
        );
        const value = await waitForCommand<{ ownership?: ThreadOwnership }>(
          command,
        );
        if (current.current.threadId !== id) return;
        if (value.ownership) setOwnership(value.ownership);
        setNotice("会话已交由本项目托管。请确认草稿后手动发送消息。");
        await check();
      } catch (reason) {
        if (current.current.threadId === id) {
          setError(errorText(reason));
          void check();
        }
      } finally {
        if (current.current.threadId === id) setPending(false);
      }
    },
    [actions, ownership?.owner_token, check],
  );

  const update = useCallback((value: ThreadOwnership) => {
    if (current.current.enabled) {
      request.current += 1;
      checkAbort.current?.abort();
      setChecking(false);
      setOwnership(value);
    }
  }, []);
  return {
    ownership,
    error: error || checkError,
    notice,
    pending: pending || checking,
    check,
    takeover,
    release,
    update,
  };
}
