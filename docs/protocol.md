# Codex Anywhere protocol v2

## Projects and local history (v2)

Project directory allowlists are removed entirely. Pairing and hello no longer carry `roots`; node configuration has no project or artifact root options. Any absolute directory accessible to the node's operating-system account can be used. Native Codex approvals and sandbox settings remain unchanged. Hub-owned asset storage containment and image size limits are independent of project directory access and remain enforced.

- `GET /api/projects?host_id=...` -> `{projects:[{id,host_id,name,cwd,thread_count,created_at,updated_at}]}`. A project is a node plus a canonical directory, case-insensitive on Windows.
- `POST /api/projects` `{host_id,cwd,name?,create_directory?:false}` -> 202 `{command}` (`project.open`). Node resolves an existing directory or creates it when explicitly requested. Successful command result is `{project:Project}`.
- `POST /api/hosts/<id>/directories` `{path?:string}` -> 202 `{command}` (`directory.list`). Result `{path,parent,home,locations:[{name,path}],directories:[{name,path}],truncated}` browses the node filesystem, not the hub filesystem; locations are filesystem drives/home, not an allowlist.
- `POST /api/hosts/<id>/catalog` `{cursor?,limit?:50,archived?:false,search?,cwd?}` -> 202 `{command}` (`thread.list`). Result is native `{data:[Thread],nextCursor}` with desktop/app-server, CLI and IDE source kinds explicitly included.
- `POST /api/hosts/<id>/imports` `{native_thread_id,title?,cwd?}` -> 202 `{thread,command}` (`thread.import`). Repeated imports upsert the same `(host_id,native_id)`. Active/waiting managed tasks cannot be overwritten. Import creates a placeholder with `status:importing`, `imported:true`, `history_loaded:false`; it becomes available only when the durable completion event is projected.
- `POST /api/threads` accepts `project_id` in place of `cwd`; absolute `cwd` remains supported and is automatically grouped into a project. `GET /api/threads` also accepts `project_id`, `host_id`, `limit` and `offset`, and returns `{threads,has_more,next_offset}`.
- Thread objects add nullable `project_id`, `imported` and `history_loaded`. Item objects add a stable numeric `position`; history is ordered by position.

Node history import is read-only: read metadata with `thread/read(includeTurns:false)`, page turn metadata with `thread/turns/list` (`itemsView:notLoaded`, limit 50, ascending), then page each turn through `thread/items/list` (limit 1, ascending). The item API returns `{turnId,item,startedAtMs,completedAtMs}` wrappers; preserve the original item and assign stable positions in turn/item order, using the turn's `startedAt` for history timestamps. This avoids aggregating multiple image-bearing turns into one JSONL response. If item pagination is unsupported before any item is emitted, restart full turn pagination with limit 1; if turn pagination itself is unavailable, use `thread/read(includeTurns:true)`. Once an item has been emitted, a pagination failure ends the import instead of falling back and duplicating history. Do not resume or generate a turn merely to import history. Persist imported thread/cwd mapping so a later user message can resume that original native thread.

Import emits durable `anywhere/import_metadata` `{threadId,commandId,thread}` (metadata only), then `anywhere/import_item` `{threadId,commandId,turnId,item,position,createdAt?}` for each native item in order, and finally `anywhere/import_complete` `{threadId,commandId,itemCount}`. Metadata contains `id`, `cwd`, `title`, `name`, `preview`, `createdAt` and `updatedAt`, without `turns`. Item positions start at 1; optional item `createdAt` preserves its turn's native start timestamp in Unix seconds. The node asset worker processes import-item events using the same image bridge. Command results contain only an item count/metadata summary; full history is streamed through the outbox rather than a single giant command result. The hub projects these events only for the matching import command; outdated imports cannot change a live task. Original native item payloads and IDs are preserved.

The durable import-complete event also finalizes the read-only import command summary; a missing or late RPC result cannot make already imported history unknown again after a process restart.

These changes require the hub, rebuilt browser application and nodes to be upgraded together. Existing node identity/token state and journals are retained; old `roots` configuration is discarded on load, never enforced. Existing hub threads are backfilled into projects by node and directory during migration. Upgrading does not automatically import local Codex history.

This is the implementation contract. The hub stores a projection; Codex on each host owns the actual runtime. Single administrator, single hub process in application v0.1, using this v2 feature contract. Browser and node use standard WebSockets, not Socket.IO. Hub API timestamps are UTC ISO strings; preserved native payloads keep their native formats, including Unix seconds for thread and turn timestamps. The transport welcome retains `protocol:1`; that transport field does not identify the project/history feature version.

## Browser HTTP

JSON errors: `{ "error": "human readable message" }`. GET `/api/auth/me` returns `{ authenticated, username?, csrf_token }` and creates a session. Every mutating browser request includes `X-CSRF-Token` from that response. POST `/api/auth/login` accepts `{username,password}`, returns the same session shape. POST `/api/auth/logout` clears login. Credentials are session cookies, HttpOnly, SameSite=Lax.

Authenticated cookies also require a live process-local session nonce. Logout revokes that nonce for copied cookies and existing browser WebSockets; expiration closes the socket with code 1008. A hub restart requires a new browser login. Node credentials are independent of browser sessions.

- GET `/api/hosts` -> `{hosts:[{id,name,platform,online,version,last_seen_at}]}`.
- POST `/api/pairings` -> `{code,expires_at}`. Node pairing is separate below.
- GET `/api/threads?q=...&project_id=...&host_id=...&limit=...&offset=...` -> `{threads:[Thread],has_more,next_offset}`. Thread = `{id,host_id,project_id,native_id,title,cwd,status,active_turn_id,last_error,imported,history_loaded,created_at,updated_at}`. `status` = idle|active|waiting|error|interrupted|starting|importing. Nullable `last_error` preserves native error details across reloads and clears when a new turn starts.
- POST `/api/threads` `{host_id,title,project_id?,cwd?,model?}` -> HTTP 202 `{thread,command}`. Select a project or supply an absolute directory; an explicit directory is grouped into a project when the node confirms its canonical path. Directory existence/access is checked on the node, never by reading the hub filesystem.
- GET `/api/threads/<id>` -> `{thread,items:[Item],interactions:[Interaction]}`. Item = `{id,native_id,thread_id,turn_id,type,data,position,created_at}`. Order by stable `position`. `data` preserves the native Codex item, with optional `_assets:[Asset]` / `_references:[Reference]` added by the platform. GET history must survive reload. Imported tasks cannot accept messages until `history_loaded` is true.
- POST `/api/threads/<id>/messages` `{text,asset_ids:[],references:[],client_request_id}` -> 202 `{command}`. References are `{kind:'task',thread_id,item_id?}` or `{kind:'web',url,title?,excerpt?}`. Resolved immutable snapshots are stored and model-visible quoted text is built by hub. Web content is user-provided excerpt; hub MUST NOT fetch arbitrary URLs.
- POST `/api/threads/<id>/steer` same message shape; POST `/api/threads/<id>/interrupt` `{}` -> 202 `{command}`.
- GET `/api/commands/<id>` -> `{command:{id,kind,status,result,error,...}}`.
- POST `/api/interactions/<id>/answer` `{response: native_response_object}` -> 202 `{command}`. Interaction = `{id,thread_id,method,params,status}`. `params` preserves Codex server request. Only advertised available decisions may be selected.
- POST `/api/assets` multipart `file` -> `{asset:{id,name,mime,size,url}}`, images only, limit 20 MiB. GET `/api/assets/<id>` returns authenticated bytes.
- POST `/api/references/resolve` `{references:[...]}` -> `{references:[{kind,title,url?,thread_id?,item_id?,snapshot_id,excerpt}]}`. Task links `/tasks/<thread_id>?item=<item_id>`.

## Browser WebSocket

`/ws/client` uses session cookie; Origin must match request host. Client sends `{type:'subscribe',thread_id?,after:0}`. Server sends `{type:'ready'}` then events `{type:'event',id: integer,thread_id: string|null,method:string,params:object}`. Event IDs are hub-global monotonic cursors. Persist events before live broadcast. Reconnect and subscribe with last cursor. Global subscription (no thread_id) is permitted for the single administrator. UI can re-fetch a thread on lifecycle/item completion; delta events allow incremental rendering.

## Node HTTP and WebSocket

POST `/api/hosts/pair` (no browser auth or CSRF) `{code,name,platform,version}` -> `{host:{id,...},token}`. Pairing codes expire, single use. Store token locally; never log it. Node opens `/ws/node` with `Authorization: Bearer <token>`. For local HTTP allow loopback only unless explicit `--allow-insecure`.

Node sends `{type:'hello',runtime_id:string,version,platform}`. runtime_id is the Codex PROCESS epoch, stable across network reconnects, changes on process restart. Hub sends `{type:'welcome',host_id,protocol:1}`. Heartbeat `{type:'ping'}` -> `{type:'pong'}` every 15 seconds. Single live node owner per host; duplicate rejected. Pairing and hello have no directory grants.

CLI entry points are `node pair --server URL --code CODE --name NAME --state-dir DIR` and `node run --state-dir DIR [--codex-bin codex]`. Neither command accepts project or artifact root options. Python entry points are `pair_node(server, name, state_dir, code, allow_insecure=False)` and `run_node(state_dir, codex_bin='codex', allow_insecure=False)`.

The connection becomes online only after welcome and durable pending command replay finish. HTTP handlers enqueue without writing sockets; the connection owner drains a bounded queue. Queue overflow disconnects that owner, retaining durable commands for reconnect. Frame writes complete with `sendall` and serialize application and control frames. Replay socket I/O runs outside the global command lock and is bound to the original connection.

Hub sends `{type:'command',command:{id,kind,thread_id,payload}}`.

Kinds:

- `directory.list`: payload `{path?}`. Return `{path,parent,home,locations,directories,truncated}`. Missing path starts at the node user's home. Listing is bounded; `locations` supplies navigational drive/home shortcuts.
- `project.open`: payload `{cwd,name?,create_directory?:false}`. Return canonical `{cwd,name}` from the node; the hub upserts the project and exposes `{project:Project}` in the persisted command result. Create missing directories only when explicitly requested.
- `thread.list`: payload `{cursor?,limit?:50,archived?:false,search?,cwd?}`. Call native `thread/list`, passing filters and pagination through. Explicit `sourceKinds` include `cli`, `vscode`, `appServer`, `exec`, `unknown`, `subAgent`, `subAgentReview`, `subAgentCompact`, `subAgentThreadSpawn`, and `subAgentOther`; `modelProviders:[]` includes all providers. Let Codex repair its history index when needed. Do not walk user project directories to discover conversations. Return native catalog metadata without turn histories.
- `thread.import`: payload `{native_thread_id,title?,cwd?}`. Read metadata with `thread/read(includeTurns:false)`, then page `thread/turns/list` with `itemsView:'full'`, `sortDirection:'asc'`. Only unsupported pagination permits fallback to `thread/read(includeTurns:true)`; ordinary missing-thread, storage and permission errors remain failures. Emit durable import events, return `{thread:metadataWithoutTurns,itemCount}`, and persist the native ID/cwd mapping. Import must not call `thread/resume` or `turn/start`, or mark the thread as loaded into the runtime.
- `thread.start`: payload `{cwd,model?,title}`. Return raw thread/start result.
- `turn.start` / `turn.steer`: payload `{native_thread_id,input:[native Codex UserInput],expected_turn_id?}`. Images already expressed as `{type:'image',url:'data:image/...;base64,...'}`. A new turn resumes the created or imported native thread with `excludeTurns:true` if it is not loaded. Steer and interrupt address the active native turn without waiting for a start/resume RPC. Return raw RPC result.
- `turn.interrupt`: payload `{native_thread_id,turn_id}`.
- `interaction.answer`: payload `{rpc_id,runtime_id,response}`. Reply to original Codex server request only if epoch still matches.

Node durably marks command in SQLite before sending RPC, sends `{type:'command_started',id}` then `{type:'command_result',id,result:object}` or `{type:'command_result',id,error:string}`. Replayed completed commands return cached result. A command with uncertain outcome after process death is `unknown` and MUST NOT execute again automatically.

Node forwards Codex notifications and server requests as `{type:'event',event_id:uuid,runtime_id,seq:integer,method,params,rpc_id?}`. Persist these in SQLite outbox before sending. Server ACK `{type:'event_ack',event_id}` ONLY after DB commit, duplicate events deduplicated by event_id. Preserve raw params. Incoming native `threadId` is mapped by hub to hub thread ID. For server requests, rpc_id is the ORIGINAL request ID (int or string), never replace it. Browser approval response is an RPC result, not a new turn. Unknown events retained and displayed in generic tool card. Reconnect does not restart Codex.

Native errors with `willRetry:true` remain in event history without making the task terminal. Late command results and old turn completions must not replace a newer active turn or clear its error. A task remains waiting until all current-process pending or answering interactions settle; ending the turn or process expires both states. User message display metadata is bound to the matching native input, including steer inputs, rather than guessed from the latest command.

Each connection has at most 100 unacknowledged events in flight. An ACK frees a slot without retransmitting the entire window; unacknowledged events retry after at least five seconds. Duplicate ACKs do not wake the sender. Reconnect resets the in-flight window and replays durable unacknowledged events. Native `serverRequest/resolved` expires cached approval IDs, and a queued answer rechecks validity under the runtime write lock before replying.

Node image bridge: POST `/api/node/assets` using bearer auth, multipart `file` and `thread_id` (native thread ID), `item_id`, returns `{asset}`. Publish a durable event `anywhere/assets` with `{threadId,itemId,assets:[Asset],commandId?}`. Extract images from imageView.path, imageGeneration.savedPath / base64 result, MCP/dynamic/function output blocks, local Markdown images and imported user-message image inputs. Resolve relative files against the task cwd; any absolute file readable by the node account can be used. There is no project or artifact directory allowlist. Preserve HTTP/HTTPS image links without downloading them automatically. Asset metadata uses the authenticated hub URL; the hub only serves its own stored asset files.

Output extraction is bounded to 16 images and 40 MiB per native item, with 20 MiB and 25 million pixels per image. Excess output images are skipped with a log warning. Reads themselves are capped even if a file grows after its metadata check. Hub attachment reads and downloads must resolve inside its asset directory and handle missing files without an internal error.

## Runtime and permissions

Run `codex app-server --listen stdio://`, initialize once, then `initialized`. Default new threads use `approvalPolicy:'on-request'`, `sandbox:'workspace-write'`; never silently disable approvals. Do not send a hardcoded model by default. Drain stderr separately. Treat stdout as JSONL. On Codex process exit, fail pending RPCs and mark interactions expired through `anywhere/runtime_stopped`. Starting a new Codex process changes runtime_id. Browser disconnect does not stop node or Codex.

RPC deadlines cover write-lock acquisition, pipe backpressure and response waiting. Notification and approval writes also have deadlines. Process shutdown and exit callbacks have bounded waits; inherited tool pipes cannot prevent the direct Codex process exit from being observed.
