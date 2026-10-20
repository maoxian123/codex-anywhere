import type { Session } from "./types";

let csrfToken = "";
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}
export function setSession(session: Session) {
  csrfToken = session.csrf_token;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (!(init.body instanceof FormData) && init.body !== undefined)
    headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method))
    headers.set("X-CSRF-Token", csrfToken);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError("无法连接管理中心，请检查网络后重试。", 0);
  }
  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/auth/login" &&
      typeof window !== "undefined"
    )
      window.dispatchEvent(new Event("anywhere:session-expired"));
    const error = (await response.json().catch(() => ({}))) as {
      error?: string;
    };
    throw new ApiError(
      error.error || `请求失败（${response.status}）`,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function post<T>(path: string, data: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(data) });
}
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请重试。";
}
