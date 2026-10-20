import type { Command, HubEvent } from "./types";
import { object, string } from "./utils";

export function nativeErrorText(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  const error = object(value);
  const message = string(error.message || error.error || error.reason).trim();
  const details = string(error.additionalDetails || error.details).trim();
  return (
    [message, details && details !== message ? details : ""]
      .filter(Boolean)
      .join("\n") || undefined
  );
}

export function commandFailure(command: Command): string | undefined {
  const message = nativeErrorText(command.error);
  if (message) return message;
  if (command.status === "unknown")
    return "指令执行结果未知。请检查会话状态，避免重复发送。";
  if (["failed", "error"].includes(command.status))
    return "远程指令执行失败，请检查节点与会话状态。";
  return undefined;
}

export function eventFailure(event: HubEvent): string | undefined {
  if (event.method === "error") return nativeErrorText(event.params.error);
  if (event.method === "turn/completed")
    return nativeErrorText(object(event.params.turn).error);
  if (event.method === "anywhere/command_completed") {
    const nested = object(event.params.command);
    return commandFailure({
      id: string(event.params.command_id || event.params.id || nested.id),
      kind: string(nested.kind),
      status: string(event.params.status || nested.status),
      error: nativeErrorText(event.params.error || nested.error),
    });
  }
  return undefined;
}
