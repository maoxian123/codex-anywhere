import { api } from "./api";
import type { Command } from "./types";

export const PRESENCE_INTERVAL = 30_000;
export const CONVERSATION_IDLE = 5 * 60_000;

export type LeasePayload = { client_id: string; issued_at: number };
type SendLease = (
  action: "presence" | "release",
  payload: LeasePayload,
  keepalive: boolean,
) => Promise<unknown>;

/** A browser tab renews interest, never resumes a native thread on its own. */
export class ThreadPresence {
  private lastActivity: number;
  private lastPresence = -Infinity;
  private lastIssued = 0;
  private released = true;
  private hidden = false;
  private manualPaused = false;
  constructor(
    readonly clientId: string,
    private readonly send: SendLease,
    private readonly now = Date.now,
    private readonly issueTime?: () => number,
  ) {
    this.lastActivity = now();
  }
  payload(): LeasePayload {
    this.lastIssued =
      this.issueTime?.() ??
      Math.max(this.lastIssued + 0.001, this.now() / 1000);
    return { client_id: this.clientId, issued_at: this.lastIssued };
  }
  async ensure() {
    this.lastActivity = this.now();
    this.hidden = false;
    this.manualPaused = false;
    return this.renew();
  }
  private async renew() {
    this.released = false;
    this.lastPresence = this.now();
    return this.send("presence", this.payload(), false);
  }
  activity() {
    this.lastActivity = this.now();
    if (!this.hidden && !this.manualPaused && this.released)
      void this.renew().catch(() => {});
  }
  heartbeat() {
    if (this.hidden || this.manualPaused) return;
    if (this.now() - this.lastActivity >= CONVERSATION_IDLE) {
      if (!this.released) void this.release().catch(() => {});
    } else if (
      !this.released &&
      this.now() - this.lastPresence >= PRESENCE_INTERVAL
    ) {
      void this.renew().catch(() => {});
    }
  }
  nextCheck() {
    if (this.hidden || this.manualPaused || this.released)
      return PRESENCE_INTERVAL;
    return Math.max(
      1,
      Math.min(
        PRESENCE_INTERVAL,
        CONVERSATION_IDLE - (this.now() - this.lastActivity),
      ),
    );
  }
  async release(keepalive = false, manual = false) {
    if (manual) this.manualPaused = true;
    this.released = true;
    return this.send("release", this.payload(), keepalive);
  }
  pagehide() {
    this.hidden = true;
    return this.release(true);
  }
  pageshow() {
    this.hidden = false;
    if (this.manualPaused) return Promise.resolve();
    return this.ensure();
  }
}

export function sendThreadLease(
  threadId: string,
  action: "presence" | "release",
  payload: LeasePayload,
  keepalive: boolean,
) {
  return api<{ command?: Command }>(
    `/threads/${encodeURIComponent(threadId)}/${action}`,
    { method: "POST", body: JSON.stringify(payload), keepalive },
  );
}
