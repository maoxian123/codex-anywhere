import { useEffect, useState } from "react";
import { api, errorText, post } from "./api";
import { waitForCommand } from "./commands";
import type { Command, Host, RuntimeCapabilities } from "./types";

const cache = new Map<string, { value: RuntimeCapabilities; at: number }>();
const inFlight = new Map<string, Promise<RuntimeCapabilities>>();
const CACHE_LIMIT = 24;
const MAX_AGE = 60000;

export async function fetchRuntimeCapabilities(
  hostId: string,
  cwd: string,
  refresh = false,
  runtimeId?: string | null,
) {
  const key = JSON.stringify([hostId, cwd, runtimeId ?? null]);
  const pending = inFlight.get(key);
  if (pending) return pending;
  const cached = cache.get(key);
  if (!refresh && cached && Date.now() - cached.at < MAX_AGE)
    return cached.value;
  const request = (async () => {
    const path = `/hosts/${encodeURIComponent(hostId)}/capabilities`;
    let value: RuntimeCapabilities | null = null;
    if (!refresh)
      value = (
        await api<{ capabilities: RuntimeCapabilities | null }>(
          `${path}?cwd=${encodeURIComponent(cwd)}`,
        )
      ).capabilities;
    if (!value) {
      const { command } = await post<{ command: Command }>(path, {
        cwd,
        ...(refresh ? { refresh: true } : {}),
      });
      value = await waitForCommand<RuntimeCapabilities>(command);
    }
    if (!value || !Array.isArray(value.models))
      throw new Error("节点返回的模型能力无效，请刷新后重试。");
    cache.delete(key);
    cache.set(key, { value, at: Date.now() });
    while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value!);
    return value;
  })().finally(() => inFlight.delete(key));
  inFlight.set(key, request);
  return request;
}

export function useRuntimeCapabilities(host: Host | undefined, cwd: string) {
  const enabled = Boolean(host?.capabilities?.includes("runtimeSettings"));
  const key = JSON.stringify([host?.id, cwd, host?.runtime_id ?? null]);
  const [state, setState] = useState<{
    key: string;
    value: RuntimeCapabilities | null;
    loading: boolean;
    error: string;
  }>({ key: "", value: null, loading: false, error: "" });
  const [refreshRequest, setRefreshRequest] = useState({
    key: "",
    revision: 0,
  });
  const revision = refreshRequest.key === key ? refreshRequest.revision : 0;
  useEffect(() => {
    if (!enabled || !host?.online || !cwd) return;
    let active = true;
    setState({ key, value: null, loading: true, error: "" });
    void fetchRuntimeCapabilities(host.id, cwd, revision > 0, host.runtime_id)
      .then((value) => {
        if (active) setState({ key, value, loading: false, error: "" });
      })
      .catch((reason) => {
        if (active)
          setState({
            key,
            value: null,
            loading: false,
            error: errorText(reason),
          });
      });
    return () => {
      active = false;
    };
  }, [key, enabled, host?.online, host?.id, cwd, revision]);
  const current =
    state.key === key
      ? state
      : { value: null, loading: enabled && Boolean(host?.online), error: "" };
  return {
    ...current,
    reason: !enabled
      ? "此节点未提供运行设置能力，请升级节点后使用。"
      : !host?.online
        ? "设备离线，连接后可调整运行设置。"
        : "",
    refresh: () =>
      setRefreshRequest((value) => ({ key, revision: value.revision + 1 })),
  };
}
