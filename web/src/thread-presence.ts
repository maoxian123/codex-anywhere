import { api } from "./api";
import type { Command } from "./types";

export type ThreadActionPayload = { client_id: string; issued_at: number };

/** Identify explicit user actions without acquiring or renewing browser leases. */
export class ThreadActionIdentity {
  private lastIssued = 0;
  constructor(
    readonly clientId: string,
    private readonly now = Date.now,
  ) {}

  payload(): ThreadActionPayload {
    this.lastIssued = Math.max(this.lastIssued + 0.001, this.now() / 1000);
    return { client_id: this.clientId, issued_at: this.lastIssued };
  }
}

export function sendManualThreadRelease(
  threadId: string,
  payload: ThreadActionPayload,
) {
  return api<{ command?: Command }>(
    `/threads/${encodeURIComponent(threadId)}/release`,
    { method: "POST", body: JSON.stringify({ ...payload, manual: true }) },
  );
}
