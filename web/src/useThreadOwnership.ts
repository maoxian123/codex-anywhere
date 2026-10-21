import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, post } from "./api";
import { waitForCommand } from "./commands";
import { sendThreadLease, ThreadPresence } from "./thread-presence";
import type { Command, ThreadOwnership } from "./types";

export function writerConflict(error: string) {
  return /already has an active writer|active writer|writer lock/i.test(error);
}

export function useThreadOwnership(threadId: string, enabled: boolean) {
  const [clientId] = useState(() => crypto.randomUUID());
  const [ownership, setOwnership] = useState<ThreadOwnership | null>(null);
  const [error, setError] = useState("");
  const [checkError, setCheckError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [checking, setChecking] = useState(false);
  const lastIssued = useRef(0);
  const current = useRef({ threadId, enabled });
  current.current = { threadId, enabled };
  const presence = useRef<ThreadPresence | null>(null);
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
    const lease = new ThreadPresence(
      clientId,
      (action, payload, keepalive) =>
        sendThreadLease(threadId, action, payload, keepalive),
      () => Date.now(),
      () => {
        lastIssued.current = Math.max(
          lastIssued.current + 0.001,
          Date.now() / 1000,
        );
        return lastIssued.current;
      },
    );
    presence.current = lease;
    void lease.ensure().catch(() => {});
    void check();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      if (stopped) return;
      lease.heartbeat();
      timer = setTimeout(tick, lease.nextCheck());
    };
    timer = setTimeout(tick, lease.nextCheck());
    const activity = () => lease.activity();
    // Scroll events also come from the timeline following native output. Count
    // the user's wheel/touch/key/drag intent, not programmatic scrolling.
    const events = [
      "pointerdown",
      "keydown",
      "input",
      "wheel",
      "touchstart",
      "touchmove",
    ];
    events.forEach((event) =>
      document.addEventListener(event, activity, {
        passive: true,
        capture: true,
      }),
    );
    const drag = (event: Event) => {
      if ((event as PointerEvent).buttons) lease.activity();
    };
    document.addEventListener("pointermove", drag, {
      passive: true,
      capture: true,
    });
    const onHide = () => void lease.pagehide().catch(() => {});
    const onShow = () => {
      void lease.pageshow().catch(() => {});
      void check();
    };
    window.addEventListener("pagehide", onHide);
    window.addEventListener("pageshow", onShow);
    return () => {
      stopped = true;
      clearTimeout(timer);
      events.forEach((event) =>
        document.removeEventListener(event, activity, true),
      );
      document.removeEventListener("pointermove", drag, true);
      window.removeEventListener("pagehide", onHide);
      window.removeEventListener("pageshow", onShow);
      void lease.pagehide().catch(() => {});
      if (presence.current === lease) presence.current = null;
      request.current += 1;
      checkAbort.current?.abort();
    };
  }, [threadId, enabled, clientId, check]);

  const ensurePresence = useCallback(async () => {
    await presence.current?.ensure();
  }, []);
  const release = useCallback(async () => {
    const lease = presence.current;
    if (!lease) return;
    const id = current.current.threadId;
    setPending(true);
    setError("");
    setNotice("");
    try {
      const result = (await lease.release(true, true)) as { command?: Command };
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
          ? value.reason || "已请求释放托管，当前任务完成后将释放。"
          : value?.retained
            ? value.reason || "其他页面仍在使用此会话，当前托管将继续保留。"
            : value?.stale
              ? value.reason || "页面租约已更新，请重新检查托管状态。"
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
  }, [check]);
  const releaseOnExit = useCallback(async () => {
    await presence.current?.pagehide().catch(() => {});
  }, []);
  const takeover = useCallback(
    async (confirmed = false) => {
      const id = current.current.threadId;
      const lease = presence.current;
      if (!lease || !current.current.enabled) return;
      setPending(true);
      setError("");
      setNotice("");
      try {
        await lease.ensure();
        const { command } = await post<{ command: Command }>(
          `/threads/${encodeURIComponent(id)}/takeover`,
          {
            ...lease.payload(),
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
    [ownership?.owner_token, check],
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
    ensurePresence,
    releaseOnExit,
    update,
  };
}
