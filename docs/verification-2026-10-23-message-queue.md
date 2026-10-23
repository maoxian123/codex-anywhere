# Follow-up message queue — 2026-10-23

## Behavior

Running conversations accept messages into a durable per-conversation FIFO. Enter
and the send button enqueue; “立即补充” retains native steering. Pending messages
appear above the composer with their order, attachment/reference metadata, text
editing and cancellation. Paused queues require explicit continuation. Accepted
queued messages clear the draft without waiting for a native RPC, allowing another
message to be composed immediately. Closing or refreshing the page retains the
server-side queue. Different conversations remain independent.

Only a confirmed idle conversation without unsettled commands or current approvals
can dispatch its next entry. Failure, interruption and unknown outcomes preserve a
pause in the same transaction as their native projection. Later desktop activity
cannot resume that queue. Explicit resume never retries an earlier unknown command.
Desired execution settings are selected when dispatching; attachments and reference
snapshots remain unchanged. The native wire contract and node runtime are unchanged.

## Validation

- Windows Python suite: **803 passed, 2 skipped**; Ruff check passed.
- Queue unit/integration coverage: **66 passed**, including FIFO, browser-independent
  reconnection, duplicate requests/completions, early completion before RPC results,
  pending/answering approvals, edit/cancel races, unknown-outcome acknowledgement,
  atomic pause persistence without a dispatch opportunity, settings read at dispatch,
  frozen attachment/reference snapshots, and fairness beyond 100 pending threads.
- Windows frontend suite: **228 passed**; TypeScript/Vite production build and
  Prettier checks passed. Coverage includes queued sends with and without ownership
  support, immediate steering, canceled request replay, editing conflicts, offline
  editing/cancellation, paused queue labels and preservation of unsaved edits.
- `git diff --check` passed. No screenshot or browser geometry verification is
  claimed; this tool session has no available browser.
- Tests use fake Codex peers and make no live inference requests. No Linux test suite
  was run. The Linux production Hub is the deployment target; deployment checks are
  limited to the existing service, package bytes, database revision and HTTP/static
  asset health. Production tests are prohibited by the deployment guide.

The existing completion-based 120-second node release policy is unchanged. No node
restart, database migration or runtime dependency change is required for this release.
