# Codex Anywhere protocol v5

## Local node settings service

`codex-anywhere node web [--state-dir DIR] [--port 5780] [--codex-bin codex]
[--no-open]` starts a separate loopback-only settings server on `127.0.0.1`.
The Windows/Linux launchers bootstrap the Python environment and run that command.
It serves its own packaged HTML/CSS/JavaScript, without depending on the hub's
frontend build. The node remains the same `NodeService` and Codex app-server
implementation used by `node run`; the settings UI does not replace its protocol.

The settings controller creates the node on its background worker's asyncio loop,
and owns its lifetime independently of Flask requests and browser tabs. A paired
node auto-starts with the settings service. Browser close does not stop it; explicit
Stop and local service shutdown gracefully cancel the owned node and Codex runtime.
Native process/runtime epochs, journals, approvals and uncertain outcomes retain
the existing node semantics.

The local service exposes `GET /api/status` -> `{status,csrf_token}` and JSON-only
`POST /api/pair`, `/api/start`, `/api/stop` -> 202 `{status}`. Status includes only
public connection/configuration fields and sanitized errors; tokens, pairing codes
and raw node configuration are never returned. Pair accepts
`{server,name,code,codex_bin?,allow_insecure?:false,replace_pairing?:false}`; Start
accepts `{codex_bin?,allow_insecure?}`, and Stop accepts `{}`. Every mutation needs
the browser's `X-CSRF-Token` and signed HttpOnly SameSite=Strict session cookie.
Exact loopback Host/port, request Origin and cross-site fetch metadata checks
prevent remote pages and DNS rebinding from configuring or stopping the node.
No wildcard bind, CORS access or external assets are provided.

Pair/start/stop requests schedule bounded background operations and report progress
through status polling. Pairing codes are used once, never saved, and failed or
uncertain pairing is not retried automatically. Re-pair requires explicit
`replace_pairing:true` while stopped. Each new pairing uses a fresh
`profiles/<uuid>/` state directory. Atomic `launcher.json` selects the active
relative profile and stores public launcher preferences. Existing root `node.json`
state remains compatible; old profiles and journals are retained rather than
being delivered under a new hub identity. Profile paths cannot escape the
selected base state directory. A separate launcher lock prevents two local
controllers from managing the same base directory; native per-profile node locks
also prevent simultaneous `node run` ownership.

## Thread writer ownership and browser leases (v5)

Nodes advertise the independent `threadOwnership` capability. Upgrade the hub,
browser bundle and nodes together to enable these controls; no database migration
is needed for this feature. The transport welcome still uses `protocol:1`.

Opening a conversation checks ownership without resuming it or starting inference.
Authenticated, CSRF-protected browser endpoints journal commands before delivery:

- `POST /api/threads/<id>/ownership` `{}` -> 202 `{command}` (`thread.ownership`).
  Concurrent pending checks for the same runtime/thread are reused. The result is
  `{state,owners,can_terminate,owner_token,reason?,owner_kind?,...}`. State is
  `managed|available|external|unknown`; owner entries contain `pid`, `name` and
  `started_at`. `managed` means loaded by this node's current app-server, not merely
  present in the hub's cached history. Permission failures and incomplete process
  discovery are `unknown`, never proof that a writer is free.
- `POST /api/threads/<id>/presence` `{client_id,issued_at}` -> 202 `{command}`
  (`thread.presence`). The hub scopes the page identifier to its authenticated
  session and supplies an absolute `expires_at` 90 seconds in the future. Browser
  requests cannot choose a longer lease. The browser renews every 30 seconds only
  while the selected conversation has had user interaction within five minutes.
- `POST /api/threads/<id>/release` `{client_id,issued_at}` -> 202 `{command}`
  (`thread.release`). It removes only this page's lease. Result includes
  `{released,deferred,reason?,ownership?}`. Another page's valid lease prevents
  release. Active turns, outstanding start/control commands and pending approvals
  are retained; release is deferred until work finishes. Switching conversations,
  page exit and five minutes of user inactivity request release. Logout also
  journals releases for that authenticated session's known pages. Failed page-exit
  delivery, crashed browsers and hub outages are covered by the node's lease TTL.
- `POST /api/threads/<id>/takeover`
  `{client_id,issued_at,owner_token?,confirmed?:false}` -> 202 `{command}`
  (`thread.takeover`), result `{ownership}`. A free writer is acquired by resuming
  the same native thread with `excludeTurns:true`; there is no inference. External
  process termination requires explicit confirmation and a short-lived node-issued
  token bound to the inspected process identity and lock file. Arbitrary PIDs are
  never accepted. The node revalidates identity and waits for actual lock release
  before resuming. This ends an entire process and can affect its other threads;
  the browser confirmation names the process and states that impact. Windows
  Restart Manager candidates are labelled `owner_kind:candidate`, rather than
  being presented as proven byte-range lock holders. Ambiguous, inaccessible or
  changed owners cannot be terminated. Successful takeover marks cached history
  stale so the previous owner's latest native history can be loaded.

`issued_at` is a strictly increasing per-page request-order value. The hub preserves
it for the node, which ignores older presence/release deliveries; release wins a
tie. The node uses the hub's absolute expiry, clamped to a 90-second maximum, for
TTL rather than trusting the browser clock. Duplicate page mutation requests reuse
their journaled command. An ignored out-of-order request can return 200
`{command:null,ignored:true}`. Unknown outcomes are never retried automatically.
Takeover requests superseded by a later page operation return 409. The node also
checks takeover expiry and request order before any process termination or resume.
For this capability the welcome carries `server_time` (Unix seconds); nodes map it
to a monotonic reference when comparing hub-supplied expiries, so ordinary hub/node
wall-clock differences do not expire valid leases or revive old replayed commands.

Node payloads use `native_thread_id` in place of the hub thread ID. Presence,
release and takeover additionally carry the session-scoped `client_id`,
`issued_at` and `expires_at` (`0` for release). Ownership changes are forwarded as
durable `anywhere/ownership` `{threadId,ownership}` events. Original Codex events,
including `thread/closed`, remain preserved. Those close/unload events invalidate
the node's loaded-thread and execution caches so a later message resumes normally.

The node sweeps leases independently of browser and hub lifetimes. Threads used
without browser leases have a five-minute idle fallback. It starts its own
app-server with the process-local `thread_unload_delay_secs=0` override and releases
an idle thread through `thread/unsubscribe`, waiting for `thread/closed`. It never
edits the user's Codex configuration, deletes writer locks, or stops the shared
app-server to release a single thread. Unsupported native unloading and release
timeouts are reported explicitly, without claiming the lock was released. Active
work survives browser exit and finishes before release; nodes and runtime
processes remain independent of Flask requests and browser lifetimes. Existing
approval/sandbox settings are preserved on takeover and subsequent resume.

Native Codex can defer persisting a brand-new empty thread until its first turn.
After `thread/start`, the node attempts `thread/name/set` using the user's supplied
title to persist the original thread without inference. If naming is unsupported
or persistence is not confirmed, the node retains that empty thread and reports
release as deferred until its first successful turn; it must not unload an empty
thread that cannot yet be resumed. It never creates a fake turn or silently
replaces the native thread ID.

Lock inspection uses Codex's `CODEX_HOME/thread-writer-locks` namespace. Linux
matches kernel locks to file device/inode and verifies same-account Codex process
identities; Windows uses bounded Restart Manager discovery and stable process
identity checks. These controls require a native Codex version supporting this
writer-lock namespace and the unsubscribe/unload protocol. Nodes without
`threadOwnership` retain their previous browser behavior.

## Runtime settings, native pages and project inventory (v4)

Hello capabilities are independent feature flags: `catalogSync`, `lazyHistory`, `runtimeSettings` and `projectInventory`. The hub sends new commands only to nodes advertising the corresponding feature. Catalog synchronization still requires explicit welcome negotiation. A welcome may contain `capabilities:{catalogSync:{recentLimit:50,intervalSeconds:60,includeProjects:true}}`; `includeProjects` is enabled only when the node advertised `projectInventory`. An older hub cannot accidentally enable background scans on a new node. Upgrade the hub, rebuilt browser and nodes together; transport `protocol:1` remains unchanged.

### Execution settings and readback

`ExecutionSettings` is an object with optional keys `model`, `effort`, `approval_policy`, `sandbox_mode`, `approvals_reviewer` and boolean `network_access`. Unknown keys are rejected. `model` and `effort` come from that node's native model catalog, not a fixed global list. Browser presets use policies `on-request|never`, sandbox modes `read-only|workspace-write|danger-full-access`, and reviewers `user|auto_review`. For native compatibility the API also accepts `untrusted` and `guardian_subagent`; these are not new browser presets. These are separate controls: `never` does not itself grant filesystem or network access. Nodes validate model/effort compatibility and managed constraints before applying overrides. Native legacy effective payloads remain preserved.

- `GET /api/hosts/<id>/capabilities[?cwd=ABSOLUTE_PATH]` -> `{capabilities,updated_at}`. Capabilities is null when unavailable, older than 60 seconds, from another runtime epoch, or cached for a different requested cwd. This GET reads the hub cache only; browsers key their cache by host, runtime epoch and cwd.
- `POST /api/hosts/<id>/capabilities` `{cwd?,refresh?:false}` -> 202 `{command}` (`runtime.capabilities`). Identical pending requests are reused. Result: `{models,defaults,constraints,supported,cwd,runtime_id,fetched_at}`; models preserve native catalog fields, defaults use ExecutionSettings keys, and `fetched_at` is Unix seconds. Constraints may include `approval_policies`, `sandbox_modes`, `approvals_reviewers` and `auto_review_required_models`. Supported has boolean keys `model`, `effort`, `approval_policy`, `sandbox_mode`, `approvals_reviewer`, `network_access`, `auto_review`.
- `PATCH /api/projects/<id>/settings` `{execution_settings:ExecutionSettings}` -> `{project}`. Replace the project's stored defaults; new threads copy them, with explicit creation overrides applied on top. Existing threads are unchanged.
- `PATCH /api/threads/<id>/settings` `{execution_settings:ExecutionSettings,settings_revision?:integer}` -> `{thread}`. Replace the desired settings for the next turn; an outdated revision returns 409 with the current thread. Changing the object increments `settings_revision`. Omitted keys, null values and an empty object remove those stored overrides. This endpoint does not interrupt a turn or write the user's config.toml.
- `POST /api/threads` accepts `execution_settings`; legacy top-level `model` remains compatible. Message creation may also include `execution_settings` for that command. `turn.steer` rejects execution overrides; update desired thread settings for a later turn instead.

Project objects add `execution_settings`. Thread objects add desired `execution_settings`, actual `effective_settings`, `settings_revision` and nullable `effective_settings_revision`. Saved settings are not proof that a runtime accepted them. The node reports `anywhere/execution_settings` `{threadId,commandId,settings,settingsRevision}` and start results include `effective_settings` plus `settings_revision`; older readbacks must not replace a newer applied revision. The node obtains sanitized capabilities from native model/config/requirements reads without returning credentials or complete private configuration files.

The node maps model/effort overrides to native `model` and `turn/start.effort`, and start/resume effort configuration to `config.model_reasoning_effort`. Approval and reviewer overrides use `approvalPolicy` and `approvalsReviewer`; start/resume sandbox uses hyphenated `sandbox`, whereas turn/start uses structured `sandboxPolicy`. New threads default to `on-request` and `workspace-write` when no explicit approval/sandbox override is selected. Resuming an existing native thread preserves its settings unless explicitly overridden. Model and effort are not hardcoded. Native approval requests and sandbox enforcement remain authoritative.

### Finite native history pages

For a `lazyHistory` node, `POST /api/threads/<id>/history` accepts `{cursor?:string,limit?:50,retry?:false}` and journals `thread.history` payload `{native_thread_id,cursor?,limit,_native_fingerprint?}`. Limit is 1–100; `_native_fingerprint` is an internal creation-time catalog checkpoint. A missing cursor requests the latest tail; an older request must match the thread's current `history_cursor` and cannot use a stale snapshot. Current history and active/waiting/starting tasks return 200 `{thread,command:null}`. Pending reads return/reuse 202 `{thread,command}`, including an RPC-completed command whose page has not yet been projected. Failed/unknown reads require explicit `retry:true`; automatic requests return 409. Unsupported old nodes keep the prior full-import path for an initial load and reject native older cursors. Explicit manual `thread.import` remains separate and compatible.

The node reads `thread/read(includeTurns:false)` and cross-thread `thread/items/list` in descending order, with `limit:1` per RPC and no `turnId` filter. It stops after the requested count or a 28 MiB serialized raw-item budget; an item deferred by the byte budget remains the next cursor candidate. One item exceeding the entire budget fails explicitly. Unsupported native pagination also fails explicitly; this automatic path must never fall back to full `includeTurns:true` history hydration. Read operations never call `thread/resume` or start inference. A successfully loaded partial page is sufficient to continue the original native thread; the runtime resumes that same ID only after the user sends a message.

Every page emits durable `anywhere/history_metadata` `{threadId,commandId,generation,thread,snapshotUpdatedAt}`, zero or more `anywhere/history_item` `{threadId,commandId,generation,index,turnId,item,createdAt?}` in descending native order, then `anywhere/history_complete` `{threadId,commandId,generation,snapshotUpdatedAt,itemCount,byteCount,nextCursor,hasMore}`. Metadata includes `_history_fingerprint`; `index` starts at zero. Preserve unknown native item types and fields. The image bridge processes history items through the ordinary asset-job path. `generation` identifies the tail snapshot and remains unchanged through its older pages.

Cursor tokens are opaque, bounded continuations containing native cursor, generation, snapshot version/fingerprint and the prior page's oldest item anchor. Compare native metadata and rollout-file stat fingerprint before and after reading, without parsing a private Codex database or reading complete rollout bodies. A same-second native change invalidates an older cursor. The hub stages raw events and applies a complete page atomically; only a committed complete event advances coverage. It finalizes the read-only command with `historyProjected:true`, even if its RPC result is missing. Validated native pages reconcile covered items to native order by reusing their occupied position slots; live notification arrival order can differ for parallel tools. Hub/native item identities and uncovered positions remain stable; partial-page loads do not delete cached items outside that page. Invalid anchors, duplicate slots and exhausted position gaps still fail the command clearly, preserve raw events and receive ACKs instead of trapping reconnects in a poison-event loop.

Threads add `history_cursor` (string/null), `history_complete` (boolean) and `history_generation` (string/null). `history_loaded:true` means a usable cached page exists; `history_complete:true` means the native continuation has reached its end. Successful older pages update `history_synced_at` while retaining their generation. Later catalog versions/fingerprints remain stale rather than being overwritten by an older snapshot. Native/history fingerprints are internal and are not exposed in the browser thread DTO. Late old pages cannot replace newer pages, a full import or a newly started live turn.

`GET /api/threads/<id>?item_limit=100[&before_position=N]` pages only the hub's cached items. If more native history is needed, POST the stored `history_cursor`, wait for cursor advancement or `history_complete`, then read cached pages again. Paginated GET responses use compact item DTOs: image data URLs are replaced by attachment placeholders and oversized text/structures are bounded, setting `data._truncated:true` when necessary. Original data remains stored. `GET /api/threads/<thread_id>/items/<hub_item_id>` -> `{item}` returns one complete authenticated raw item. The no-pagination GET response remains compatible with older clients. Task-reference navigation can follow existing cached/native pages until the source item is available.

### Background project inventory

When welcome enables `catalogSync.includeProjects:true`, the node scans ordinary and then archived native thread metadata using `thread/list` pages of 100. Per connection it visits at most 1,000 pages and 10,000 normalized project directories; reconnect restarts the bounded inventory. It runs once per connection, alongside periodic recent-50 synchronization, and yields per page to foreground work and outbox backpressure. It does not enumerate project folders, load conversation bodies or run inference.

Durable `anywhere/project_catalog` `{projects:[{cwd}],syncedAt,complete}` events contain at most 100 directory entries; `syncedAt` is Unix seconds. The hub only creates/reuses projects by node and canonical directory. It does not bulk-create historical thread rows and never deletes cached projects omitted from a scan. Unsupported or permission errors stop this optional inventory until reconnect, while recent-thread synchronization continues.

Migration `0005` adds runtime capabilities, project/thread desired and effective settings, settings revisions, native history coverage/cursors/generation and internal snapshot fingerprints. Previously loaded complete histories are backfilled as complete. Run `codex-anywhere db-upgrade` before starting the upgraded single-process hub; keep the existing node state directory and rebuild the browser bundle.

## Automatic discovery baseline (v3; v4 paging supersedes full loads)

Thread and Project browser objects include `last_activity_at` (ISO timestamp).
`GET /api/threads` orders by descending activity before applying pagination,
with descending ID as a stable tie-breaker. Activity uses the latest native
thread/history version and locally journaled `turn.start`/`turn.steer` input;
threads without native versions fall back to `updated_at`, then `created_at`.
`GET /api/projects` orders by the latest activity across all of each project's
cached conversations, with creation time for an empty project and descending ID
for ties. Discovery, inventory and read-only history synchronization do not make
old native activity recent. These fields are derived without a schema migration.

Nodes advertise `capabilities:["catalogSync"]` in hello. Compatible hubs add `capabilities:{catalogSync:{recentLimit:50,intervalSeconds:60}}` to welcome. The node starts automatic discovery only after receiving that explicit welcome capability; a new node connected to an old hub must not emit automatic catalog events. Old nodes without the hello capability retain their existing behavior. On each negotiated connection the node immediately discovers the most recent 50 non-archived Codex threads and repeats every 60 seconds, independently of browsers. Do not run inference or load full histories for discovery.

The node sends durable `anywhere/catalog_sync` events with `{threads:[nativeMetadataWithoutTurns],scanned_count,limit,syncedAt}`. The first scan sends the full bounded catalog; later scans send only metadata whose identity/version/title/cwd changed, including empty updates to record successful scans. Start a fresh comparison on reconnect. `anywhere/catalog_sync_error` `{error}` reports a failed scan without removing existing data. These are host-level events without `threadId`.

The hub automatically upserts projects and thread summaries by `(host_id,native_id)`. Newly discovered threads are `idle`, `imported:true`, `history_loaded:false`; they appear immediately without importing message bodies or images. Do not delete cached conversations/projects when they leave the recent-50 window. Do not replace active tasks or pending approvals. Host objects add `last_sync_at` (ISO/null) and `sync_error` (string/null). Thread objects add `native_updated_at` and `history_native_updated_at` (native Unix seconds/null), `history_synced_at` (ISO/null), and `history_stale` (boolean). A changed native version marks cached history stale; unchanged scans do not trigger repeated downloads. Live turn completions and successful history loads update the cached history version.

Opening a discovered/stale thread automatically POSTs `/api/threads/<id>/history` `{}`. The host must be online. v4 `lazyHistory` nodes use the finite page contract above; older nodes use the original durable `thread.import` command. Repeated calls reuse pending commands, and failed/unknown outcomes require explicit retry. A completed load records its native snapshot version and sync time; a newer catalog version received during loading remains stale for a subsequent refresh. Existing manual import APIs remain compatible.

Browser history reads use `GET /api/threads/<id>?item_limit=100[&before_position=N]`. `item_limit` is an integer from 1 to 200; passing only `before_position` uses a limit of 100. The optional cursor is a non-negative signed 64-bit integer, exclusive of items at that position. Select the newest page by descending position/id, then return its items in ascending position/id order plus `{has_more_items,next_before_position}`. `next_before_position` is the oldest returned position when more items exist, otherwise null. Preserve the original unbounded response without pagination fields when neither pagination argument is present. The browser loads older pages on request and never replaces the list with an unbounded history response. A reference to an older source item can follow the same pagination until that item is available. A server-side import still streams individual items through the durable outbox.

The sidebar prioritizes project/conversation lists. Automatic synchronization and opening a conversation are the default flow. Searching, filtering and manually adding more local or archived history remain secondary actions; workspace banners and manual-import calls to action are not required to use automatically discovered conversations.

On the sidebar's initial load, projects with cached conversations appear before
directories discovered only by inventory, preserving the server's activity order
within each group. A newly discovered historical directory's hub creation time
must not bury projects with conversations. Background snapshots retain the
established order, and an explicitly added project can still be placed first.

## Projects and explicit history import (v2 compatible)

Project directory allowlists are removed entirely. Pairing and hello no longer carry `roots`; node configuration has no project or artifact root options. Any absolute directory accessible to the node's operating-system account can be used. Native Codex approvals and sandbox settings remain unchanged. Hub-owned asset storage containment and image size limits are independent of project directory access and remain enforced.

- `GET /api/projects?host_id=...` -> `{projects:[{id,host_id,name,cwd,execution_settings,thread_count,created_at,updated_at}]}`. A project is a node plus a canonical directory, case-insensitive on Windows.
- `POST /api/projects` `{host_id,cwd,name?,create_directory?:false}` -> 202 `{command}` (`project.open`). Node resolves an existing directory or creates it when explicitly requested. Successful command result is `{project:Project}`.
- `POST /api/hosts/<id>/directories` `{path?:string}` -> 202 `{command}` (`directory.list`). Result `{path,parent,home,locations:[{name,path}],directories:[{name,path}],truncated}` browses the node filesystem, not the hub filesystem; locations are filesystem drives/home, not an allowlist.
- `POST /api/hosts/<id>/catalog` `{cursor?,limit?:50,archived?:false,search?,cwd?}` -> 202 `{command}` (`thread.list`). Result is native `{data:[Thread],nextCursor}` with desktop/app-server, CLI and IDE source kinds explicitly included.
- `POST /api/hosts/<id>/imports` `{native_thread_id,title?,cwd?}` -> 202 `{thread,command}` (`thread.import`). Explicitly add older local history or retry a failed/unknown import. Repeated imports upsert the same `(host_id,native_id)` and reuse pending imports. Active/waiting/starting managed tasks cannot be overwritten. Import creates a placeholder with `status:importing`, `imported:true`, `history_loaded:false`; it becomes available only when the durable completion event is projected.
- `POST /api/threads` accepts `project_id` in place of `cwd`; absolute `cwd` remains supported and is automatically grouped into a project. `GET /api/threads` also accepts `project_id`, `host_id`, `limit` and `offset`, and returns `{threads,has_more,next_offset}`.
- Thread objects add nullable `project_id`, `imported` and `history_loaded`. Item objects add a stable numeric `position`; history is ordered by position.

Node history import is read-only: read metadata with `thread/read(includeTurns:false)`, page turn metadata with `thread/turns/list` (`itemsView:notLoaded`, limit 50, ascending), then page each turn through `thread/items/list` (limit 1, ascending). The item API returns `{turnId,item,startedAtMs,completedAtMs}` wrappers; preserve the original item and assign stable positions in turn/item order, using the turn's `startedAt` for history timestamps. This avoids aggregating multiple image-bearing turns into one JSONL response. If item pagination is unsupported before any item is emitted, restart full turn pagination with limit 1; if turn pagination itself is unavailable, use `thread/read(includeTurns:true)`. Once an item has been emitted, a pagination failure ends the import instead of falling back and duplicating history. Do not resume or generate a turn merely to import history. Persist imported thread/cwd mapping so a later user message can resume that original native thread.

Import emits durable `anywhere/import_metadata` `{threadId,commandId,thread}` (metadata only), then `anywhere/import_item` `{threadId,commandId,turnId,item,position,createdAt?}` for each native item in order, and finally `anywhere/import_complete` `{threadId,commandId,itemCount}`. Metadata contains `id`, `cwd`, `title`, `name`, `preview`, `createdAt` and `updatedAt`, without `turns`. Item positions start at 1; optional item `createdAt` preserves its turn's native start timestamp in Unix seconds. The node asset worker processes import-item events using the same image bridge. Command results contain only an item count/metadata summary; full history is streamed through the outbox rather than a single giant command result. The hub projects these events only for the matching import command; outdated imports cannot change a live task. Original native item payloads and IDs are preserved.

The durable import-complete event also finalizes the read-only import command summary; a missing or late RPC result cannot make already imported history unknown again after a process restart.

These changes require the hub, rebuilt browser application and nodes to be upgraded together. Existing node identity/token state and journals are retained; old `roots` configuration is discarded on load, never enforced. Existing hub threads are backfilled into projects by node and directory during migration. Migration `0004_automatic_sync` adds host sync state, native/history snapshot versions and a thread/position/id item index. After upgrade and capability negotiation, recent-50 metadata is discovered automatically and message bodies load when opened. Cached conversations outside that window remain available. Welcome capability negotiation prevents automatic sync from being enabled against an older hub.

This is the implementation contract. The hub stores a projection; Codex on each host owns the actual runtime. Single administrator, single hub process in application v0.1, using this v4 feature contract. Browser and node use standard WebSockets, not Socket.IO. Hub API timestamps are UTC ISO strings except the native version fields explicitly described above; preserved native payloads keep their native formats, including Unix seconds for thread and turn timestamps. The transport welcome retains `protocol:1`; that transport field does not identify the project/history feature version.

## Browser HTTP

JSON errors: `{ "error": "human readable message" }`. GET `/api/auth/me` returns `{ authenticated, username?, csrf_token }` and creates a session. Every mutating browser request includes `X-CSRF-Token` from that response. POST `/api/auth/login` accepts `{username,password}`, returns the same session shape. POST `/api/auth/logout` clears login. Credentials are session cookies, HttpOnly, SameSite=Lax.

Authenticated cookies also require a live process-local session nonce. Logout revokes that nonce for copied cookies and existing browser WebSockets; expiration closes the socket with code 1008. A hub restart requires a new browser login. Node credentials are independent of browser sessions.

### Application settings

`GET /api/settings` requires an authenticated browser and returns
`{administrator:{username}}`. This settings object can gain additional sections;
it never contains a password or password hash.

`PATCH /api/settings/administrator` accepts
`{username,current_password,new_password?}` and requires both browser authentication
and the usual CSRF token. Usernames are trimmed, contain 1–120 characters and
cannot contain control characters. A supplied new password must contain 8–1024
characters; omitting it retains the current password. Passwords are not trimmed.
An incorrect current password returns 403 without expiring the browser session.
Invalid payloads do not change credentials.

Success returns `{settings:{administrator:{username}},session:Session}`. Saving
commits the account configuration, revokes all existing browser session nonces,
and issues a fresh nonce and CSRF token to the current browser. The browser must
replace its session/CSRF state from this response. Copied cookies, other browser
sessions and their existing WebSockets lose access; node credentials and running
tasks are unaffected.

Migration `0006` adds persistent hub settings. The administrator section stores
only a username and password hash. Startup `ADMIN_USERNAME`/`ADMIN_PASSWORD`
remain the bootstrap credentials until a saved administrator setting exists;
saved credentials take precedence across hub restarts. The settings API does not
rewrite `.env` or return the stored hash. `/settings` serves the browser settings
page through the same login-gated application shell as conversation routes.

### Workspace endpoints

The browser sidebar groups conversations only by machine and project/directory.
It has no date buckets. Each machine retains independent scrolling and pagination;
initial activity ranking and explicit local conversation promotion are preserved.

`GET /api/settings/hosts` requires browser authentication and returns `{hosts}`.
Each entry extends the public host DTO with `data:{projects,threads,messages,
attachments,attachment_bytes}` for the management center's stored projection.
Tokens, file paths, command payloads and native configuration are never returned.

`DELETE /api/hosts/<id>` requires browser authentication, CSRF and the JSON body
`{confirm_name:CURRENT_HOST_NAME}`. Confirmation must match the machine's current
name exactly. It removes that host and its hub-side projects, conversations,
items, commands, interactions, events, reference snapshots and exclusive
attachments in one database transaction. Shared attachments used by other
machines remain available; unassociated uploads are removed only when exclusively
referenced by the deleted machine. File cleanup runs only on ordinary files
inside the configured asset directory, after the database commit. Missing files
are tolerated; cleanup failures are reported through the successful response's
`cleanup:{deleted,missing,retained,failed}` and optional `warning`.

Success returns `{deleted:true,host_id,cleanup,warning?}`. Deleting a machine
revokes its bearer token and detaches its live connection; reconnecting requires
pairing again. This operation does not access the node filesystem, delete native
Codex history/project files, terminate the Codex runtime or retry commands.
An `anywhere/host_deleted` global event with `{host_id}` tells every browser to
remove the machine and its cached rows, including an open affected conversation.
The marker is allocated before old events are purged so replay cursors remain
monotonic. Late node frames, uploads, history replies and browser list responses
cannot recreate the removed machine's projection.

- GET `/api/hosts` -> `{hosts:[{id,name,platform,online,version,runtime_id,last_seen_at,last_sync_at,sync_error,capabilities}]}`. Sync timestamps are ISO/null; errors are string/null and do not remove cached conversations. Capabilities is the node's advertised feature-name list, separate from the runtime catalog endpoint.
- POST `/api/pairings` -> `{code,expires_at}`. Node pairing is separate below.
- GET `/api/threads?q=...&project_id=...&host_id=...&limit=...&offset=...` -> `{threads:[Thread],has_more,next_offset}`. Thread = `{id,host_id,project_id,native_id,title,cwd,status,active_turn_id,last_error,imported,history_loaded,native_updated_at,history_native_updated_at,history_synced_at,history_stale,history_cursor,history_complete,history_generation,execution_settings,effective_settings,settings_revision,effective_settings_revision,created_at,updated_at}`. `status` = idle|active|waiting|error|interrupted|starting|importing. Nullable `last_error` preserves native error details across reloads and clears when a new turn starts. Native version fields are Unix seconds/null, `history_synced_at` is ISO/null, and `history_stale` is boolean.
- POST `/api/threads` `{host_id,title,project_id?,cwd?,model?}` -> HTTP 202 `{thread,command}`. Select a project or supply an absolute directory; an explicit directory is grouped into a project when the node confirms its canonical path. Directory existence/access is checked on the node, never by reading the hub filesystem.
- POST `/api/threads/<id>/history` `{cursor?,limit?:50,retry?:false}` -> 202 `{thread,command}` when starting/reusing a page load; 200 `{thread,command:null}` for current history or a live task. Failed/unknown reads without explicit retry return 409 `{error,thread,command}`; an offline node also returns 409. See the v4 contract for legacy-node compatibility.
- GET `/api/threads/<id>?item_limit=100[&before_position=N]` -> `{thread,items:[Item],interactions:[Interaction],has_more_items,next_before_position}`. With neither pagination parameter, retain the legacy `{thread,items:[Item],interactions:[Interaction]}` response with all cached raw items. Item = `{id,native_id,thread_id,turn_id,type,data,position,created_at}`. Order by stable `position`, then `id`. Paginated `data` uses the v4 compact DTO, with optional `_assets:[Asset]` / `_references:[Reference]` and `_truncated:true`; the authenticated single-item endpoint returns the preserved original. GET history must survive reload. Imported tasks cannot accept messages until `history_loaded` is true.
- POST `/api/threads/<id>/messages` `{text,asset_ids:[],references:[],client_request_id}` -> 202 `{command}`. References are `{kind:'task',thread_id,item_id?}` or `{kind:'web',url,title?,excerpt?}`. Resolved immutable snapshots are stored and model-visible quoted text is built by hub. Web content is user-provided excerpt; hub MUST NOT fetch arbitrary URLs.
- POST `/api/threads/<id>/steer` same message shape; POST `/api/threads/<id>/interrupt` `{}` -> 202 `{command}`.
- GET `/api/commands/<id>` -> `{command:{id,kind,status,result,error,...}}`.
- POST `/api/interactions/<id>/answer` `{response: native_response_object}` -> 202 `{command}`. Interaction = `{id,thread_id,method,params,status}`. `params` preserves Codex server request. Only advertised available decisions may be selected.
- POST `/api/assets` multipart `file` -> `{asset:{id,name,mime,size,url}}`, images only, limit 20 MiB. GET `/api/assets/<id>` returns authenticated bytes.
- POST `/api/references/resolve` `{references:[...]}` -> `{references:[{kind,title,url?,thread_id?,item_id?,snapshot_id,excerpt}]}`. Task links `/tasks/<thread_id>?item=<item_id>`.

## Browser WebSocket

Browser event parameters use the same bounded presentation as paginated items. Large strings, inline images and oversized structures may be summarized with `params._truncated:true` and `params._raw_url:'/api/events/<id>'`. Authenticated `GET /api/events/<id>` returns `{event:originalEvent}`. Raw native events remain unchanged in durable storage. Serialize replay rows one at a time so large events do not accumulate in server memory. Approval interactions retain their full native decision payload in the interactions API.

`/ws/client` uses session cookie; Origin must match request host. Client sends `{type:'subscribe',thread_id?,after:0}`. Server sends `{type:'ready'}` then events `{type:'event',id: integer,thread_id: string|null,method:string,params:object}`. Event IDs are hub-global monotonic cursors. Persist events before live broadcast. Reconnect and subscribe with last cursor. Global subscription (no thread_id) is permitted for the single administrator. UI can re-fetch a thread on lifecycle/item completion; delta events allow incremental rendering.

## Node HTTP and WebSocket

POST `/api/hosts/pair` (no browser auth or CSRF) `{code,name,platform,version}` -> `{host:{id,...},token}`. Pairing codes expire, single use. Store token locally; never log it. Node opens `/ws/node` with `Authorization: Bearer <token>`. For local HTTP allow loopback only unless explicit `--allow-insecure`.

Node sends `{type:'hello',runtime_id:string,version,platform,capabilities?:['catalogSync','lazyHistory','runtimeSettings','projectInventory']}`. runtime_id is the Codex PROCESS epoch, stable across network reconnects, changes on process restart. Hub sends `{type:'welcome',host_id,protocol:1}` with optional `capabilities:{catalogSync:{recentLimit:50,intervalSeconds:60,includeProjects:true}}` only for supported negotiated features. Automatic catalog scans require that welcome confirmation. Heartbeat `{type:'ping'}` -> `{type:'pong'}` every 15 seconds. Single live node owner per host; duplicate rejected. Pairing and hello have no directory grants.

CLI entry points are `node pair --server URL --code CODE --name NAME --state-dir DIR` and `node run --state-dir DIR [--codex-bin codex]`. Neither command accepts project or artifact root options. Python entry points are `pair_node(server, name, state_dir, code, allow_insecure=False)` and `run_node(state_dir, codex_bin='codex', allow_insecure=False)`.

The connection becomes online only after welcome and durable pending command replay finish. HTTP handlers enqueue without writing sockets; the connection owner drains a bounded queue. Queue overflow disconnects that owner, retaining durable commands for reconnect. Frame writes complete with `sendall` and serialize application and control frames. Replay socket I/O runs outside the global command lock and is bound to the original connection.

Hub sends `{type:'command',command:{id,kind,thread_id,payload}}`.

Kinds:

- `runtime.capabilities`: payload `{cwd?,refresh?:false}`. Read the sanitized native model/config/requirements catalog described in v4 without starting a conversation or modifying configuration.
- `thread.history`: payload `{native_thread_id,cursor?,limit?:50,_native_fingerprint?}`. Read a finite native page and emit the v4 history metadata/item/complete sequence; no automatic full-read fallback.
- `directory.list`: payload `{path?}`. Return `{path,parent,home,locations,directories,truncated}`. Missing path starts at the node user's home. Listing is bounded; `locations` supplies navigational drive/home shortcuts.
- `project.open`: payload `{cwd,name?,create_directory?:false}`. Return canonical `{cwd,name}` from the node; the hub upserts the project and exposes `{project:Project}` in the persisted command result. Create missing directories only when explicitly requested.
- `thread.list`: payload `{cursor?,limit?:50,archived?:false,search?,cwd?}`. Call native `thread/list`, passing filters and pagination through. Explicit `sourceKinds` include `cli`, `vscode`, `appServer`, `exec`, `unknown`, `subAgent`, `subAgentReview`, `subAgentCompact`, `subAgentThreadSpawn`, and `subAgentOther`; `modelProviders:[]` includes all providers. Let Codex repair its history index when needed. Do not walk user project directories to discover conversations. Return native catalog metadata without turn histories.
- `thread.import`: payload `{native_thread_id,title?,cwd?}`. Read metadata with `thread/read(includeTurns:false)`, page turn metadata with `thread/turns/list(itemsView:'notLoaded',limit:50,sortDirection:'asc')`, then page individual items with `thread/items/list(limit:1,sortDirection:'asc')`. Unsupported item pagination before any item is emitted permits fallback to full turn pagination with limit 1; unsupported turn pagination permits legacy `thread/read(includeTurns:true)`. Ordinary missing-thread, storage and permission errors remain failures, and no fallback may repeat already emitted items. Emit durable import events, return `{thread:metadataWithoutTurns,itemCount}`, and persist the native ID/cwd mapping. Import must not call `thread/resume` or `turn/start`, or mark the thread as loaded into the runtime.
- `thread.start`: payload `{cwd,model?,title,execution_settings?,settings_revision?}`. Return native thread/start result with optional `effective_settings` and `settings_revision` readback.
- `turn.start` / `turn.steer`: payload `{native_thread_id,input:[native Codex UserInput],expected_turn_id?,execution_settings?,settings_revision?}`. Execution settings and revisions apply to `turn.start` only. Images are already expressed as `{type:'image',url:'data:image/...;base64,...'}`. A new turn resumes the created or imported native thread with `excludeTurns:true` if it is not loaded. Steer and interrupt address the active native turn without waiting for a start/resume RPC. Start results include optional `effective_settings` and `settings_revision` alongside the raw RPC result.
- `turn.interrupt`: payload `{native_thread_id,turn_id}`.
- `interaction.answer`: payload `{rpc_id,runtime_id,response}`. Reply to original Codex server request only if epoch still matches.

Node durably marks command in SQLite before sending RPC, sends `{type:'command_started',id}` then `{type:'command_result',id,result:object}` or `{type:'command_result',id,error:string}`. Replayed completed commands return cached result. A command with uncertain outcome after process death is `unknown` and MUST NOT execute again automatically.

Node forwards Codex notifications and server requests as `{type:'event',event_id:uuid,runtime_id,seq:integer,method,params,rpc_id?}`. Persist these in SQLite outbox before sending. Server ACK `{type:'event_ack',event_id}` ONLY after DB commit, duplicate events deduplicated by event_id. Preserve raw params. Incoming native `threadId` is mapped by hub to hub thread ID. For server requests, rpc_id is the ORIGINAL request ID (int or string), never replace it. Browser approval response is an RPC result, not a new turn. Unknown events retained and displayed in generic tool card. Reconnect does not restart Codex.

Native errors with `willRetry:true` remain in event history without making the task terminal. Late command results and old turn completions must not replace a newer active turn or clear its error. A task remains waiting until all current-process pending or answering interactions settle; ending the turn or process expires both states. User message display metadata is bound to the matching native input, including steer inputs, rather than guessed from the latest command.

Each connection has at most 100 unacknowledged events in flight. An ACK frees a slot without retransmitting the entire window; unacknowledged events retry after at least five seconds. Duplicate ACKs do not wake the sender. Reconnect resets the in-flight window and replays durable unacknowledged events. Native `serverRequest/resolved` expires cached approval IDs, and a queued answer rechecks validity under the runtime write lock before replying.

Node image bridge: POST `/api/node/assets` using bearer auth, multipart `file` and `thread_id` (native thread ID), `item_id`, returns `{asset}`. Publish a durable event `anywhere/assets` with `{threadId,itemId,assets:[Asset],commandId?}`. Extract images from imageView.path, imageGeneration.savedPath / base64 result, MCP/dynamic/function output blocks, local Markdown images and imported user-message image inputs. Resolve relative files against the task cwd; any absolute file readable by the node account can be used. There is no project or artifact directory allowlist. Preserve HTTP/HTTPS image links without downloading them automatically. Asset metadata uses the authenticated hub URL; the hub only serves its own stored asset files.

Output extraction is bounded to 16 images and 40 MiB per native item, with 20 MiB and 25 million pixels per image. Excess output images are skipped with a log warning. Reads themselves are capped even if a file grows after its metadata check. Hub attachment reads and downloads must resolve inside its asset directory and handle missing files without an internal error.

## Runtime and permissions

Run `codex app-server --listen stdio://`, initialize once, then `initialized`. New threads use `approvalPolicy:'on-request'`, `sandbox:'workspace-write'` unless the user explicitly selected other supported settings; resuming existing threads preserves native configuration unless explicitly overridden. Never silently disable approvals. Do not send a hardcoded model or reasoning effort by default. Drain stderr separately. Treat stdout as JSONL. On Codex process exit, fail pending RPCs and mark interactions expired through `anywhere/runtime_stopped`. Starting a new Codex process changes runtime_id. Browser disconnect does not stop node or Codex.

RPC deadlines cover write-lock acquisition, pipe backpressure and response waiting. Notification and approval writes also have deadlines. Process shutdown and exit callbacks have bounded waits; inherited tool pipes cannot prevent the direct Codex process exit from being observed.
