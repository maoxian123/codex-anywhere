import type { Asset, Reference } from "./types";

export type ComposerDraft = {
  text: string;
  assets: Asset[];
  references: Reference[];
  retryRequestId: string | null;
};

const prefix = "codex-anywhere:composer-draft:v1:";
const memory = new Map<string, ComposerDraft>();
const listeners = new Map<string, Set<(draft: ComposerDraft) => void>>();
const keyFor = (threadId: string, scope: string) =>
  `${prefix}${JSON.stringify([scope, threadId])}`;

export const emptyComposerDraft = (): ComposerDraft => ({
  text: "",
  assets: [],
  references: [],
  retryRequestId: null,
});

/** Drafts stay in this browser tab and are isolated by signed-in user and thread. */
export function readComposerDraft(
  threadId: string,
  draftScope = "",
): ComposerDraft {
  const key = keyFor(threadId, draftScope);
  if (memory.has(key)) return memory.get(key)!;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return emptyComposerDraft();
    const value = JSON.parse(raw) as Partial<ComposerDraft>;
    return {
      text: typeof value.text === "string" ? value.text : "",
      assets: Array.isArray(value.assets) ? value.assets : [],
      references: Array.isArray(value.references) ? value.references : [],
      retryRequestId:
        typeof value.retryRequestId === "string" ? value.retryRequestId : null,
    };
  } catch {
    return memory.get(key) || emptyComposerDraft();
  }
}

export function writeComposerDraft(
  threadId: string,
  draft: ComposerDraft,
  draftScope = "",
): void {
  if (!draft.text && !draft.assets.length && !draft.references.length) {
    clearComposerDraft(threadId, draftScope);
    return;
  }
  const key = keyFor(threadId, draftScope);
  try {
    sessionStorage.setItem(key, JSON.stringify(draft));
    memory.delete(key);
  } catch {
    memory.set(key, draft);
    // The current page still keeps a draft if browser storage is unavailable.
  }
  listeners.get(key)?.forEach((listener) => listener(draft));
}

export function clearComposerDraft(threadId: string, draftScope = ""): void {
  const key = keyFor(threadId, draftScope);
  memory.delete(key);
  try {
    sessionStorage.removeItem(key);
  } catch {
    // Storage can be disabled by the browser.
  }
  listeners.get(key)?.forEach((listener) => listener(emptyComposerDraft()));
}

/** Keep a remounted composer in sync with a send that is still completing. */
export function subscribeComposerDraft(
  threadId: string,
  listener: (draft: ComposerDraft) => void,
  draftScope = "",
): () => void {
  const key = keyFor(threadId, draftScope);
  const subscribers = listeners.get(key) || new Set();
  subscribers.add(listener);
  listeners.set(key, subscribers);
  return () => {
    subscribers.delete(listener);
    if (!subscribers.size) listeners.delete(key);
  };
}

export type TaskMention = { start: number; end: number; query: string };

/** An ordinary email, decorator in backticks, or embedded @ stays plain text. */
export function taskMentionAtCaret(
  text: string,
  caret: number,
): TaskMention | null {
  const before = text.slice(0, caret);
  const codeMarkers = before.match(/`+|~{3,}/g) || [];
  if (codeMarkers.length % 2) return null;
  const match = /(?:^|\s)@([\p{L}\p{N}_-]*)$/u.exec(before);
  if (!match) return null;
  return { start: caret - match[1].length - 1, end: caret, query: match[1] };
}
