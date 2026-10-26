# Native questions and reminders — 2026-10-26

## Behavior

Native Codex questions display selectable options and descriptions, with custom
input only when permitted and masked input for secret questions. Every question
must be answered before explicit submission; recommendations are not preselected
and arrival does not submit a choice or steal focus. Multiple concurrent requests
have isolated controls. Offline input is retained, duplicate submissions are
guarded, and authoritative refresh distinguishes an expired request from a pending
request that can be manually retried after reconnect.

A global attention button and tab-title count cover all machines and conversations,
including conversations outside the loaded sidebar, archived conversations and
unlinked native requests. Its paginated dialog can answer directly or open the
conversation. WebSocket events, foreground refresh and a periodic fallback update
the count. Optional desktop notifications require an explicit browser permission
action; their content is generic and deduplicated. The page must remain open.

The hub validates question IDs, UTF-8 text and permitted choices before journaling
an answer. Native RPC IDs, response strings and arrays are preserved. Nonblocking
questions do not falsely mark ongoing work as waiting; queue and writer protection
still considers outstanding native requests. Resolution also clears unlinked
requests and notifications without a thread ID. No node restart, migration or
runtime dependency change is required.

## Validation

- Windows Python suite: **864 passed, 2 skipped**; Ruff check passed.
- New backend unit/API tests: **41 passed**. New integration tests: **20 passed**,
  including an actual local fake JSON-RPC subprocess through CodexRuntime, Node
  persistence/outbox, Hub, answer API and the original native response. Integer 0,
  integer and string RPC IDs, blocking/nonblocking requests, reconnection, queue
  protection, expiry, invalid input and concurrent answers are covered.
- Windows frontend suite: **267 passed**; final TypeScript/Vite build, Prettier and
  `git diff --check` passed. Tests cover option/free-text/secret input, native ID
  preservation, offline and stale-answer handling, global/context-free questions,
  draft preservation, notification permission/privacy/deduplication, stale network
  responses, pagination bounds and the existing application regressions.
- Protocol checked against local Codex CLI **0.163.0** generated experimental JSON
  schemas and [official OpenAI app-server documentation](https://learn.chatgpt.com/docs/app-server#toolrequestuserinput).
- No real inference, native OS notification delivery or browser geometry test is
  claimed. Short-screen CSS uses outer scrolling to keep options and submission
  reachable. Production Linux checks cover service health, installed package bytes,
  database revision and served static assets only; no production tests are run.
