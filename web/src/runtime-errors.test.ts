import test from "node:test";
import assert from "node:assert/strict";
import {
  commandFailure,
  eventFailure,
  nativeErrorText,
} from "./runtime-errors.ts";
import type { HubEvent } from "./types.ts";

test("native runtime errors retain explanation and additional details", () => {
  assert.equal(
    nativeErrorText({
      message: "model unavailable",
      additionalDetails: "Choose a supported model.",
    }),
    "model unavailable\nChoose a supported model.",
  );
  assert.equal(nativeErrorText({ message: "same", details: "same" }), "same");
  assert.equal(nativeErrorText(null), undefined);
});

test("terminal commands without server explanation still produce actionable errors", () => {
  assert.match(
    commandFailure({ id: "c1", kind: "turn.start", status: "unknown" })!,
    /未知.*避免重复发送/,
  );
  assert.match(
    commandFailure({ id: "c2", kind: "thread.start", status: "failed" })!,
    /执行失败/,
  );
  assert.equal(
    commandFailure({ id: "c3", kind: "thread.start", status: "completed" }),
    undefined,
  );
});

test("both live native errors and completed-turn errors are extracted", () => {
  const event = (
    method: string,
    params: Record<string, unknown>,
  ): HubEvent => ({ type: "event", id: 1, thread_id: "t1", method, params });
  assert.equal(
    eventFailure(event("error", { error: { message: "Provider error" } })),
    "Provider error",
  );
  assert.equal(
    eventFailure(
      event("turn/completed", {
        turn: { error: { message: "Context limit" } },
      }),
    ),
    "Context limit",
  );
  assert.equal(
    eventFailure(
      event("anywhere/command_completed", {
        command: { id: "c1", status: "failed", error: "Offline" },
      }),
    ),
    "Offline",
  );
  assert.equal(
    eventFailure(event("item/completed", { item: { text: "error" } })),
    undefined,
  );
});

test("transient native errors that Codex will retry never become persistent operation errors", () => {
  const event: HubEvent = {
    type: "event",
    id: 1,
    thread_id: "t1",
    method: "error",
    params: { willRetry: true, error: { message: "Transient upstream error" } },
  };
  assert.equal(eventFailure(event), undefined);
  assert.equal(
    eventFailure({ ...event, params: { ...event.params, willRetry: false } }),
    "Transient upstream error",
  );
});
