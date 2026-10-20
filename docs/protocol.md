# Codex Anywhere protocol v1

This is the implementation contract. The hub stores a projection; Codex on each host owns the actual runtime. Single administrator, single hub process in v0.1. Browser and node use standard WebSockets, not Socket.IO. All times are UTC ISO strings.

## Browser HTTP

JSON errors: `{ "error": "human readable message" }`. GET `/api/auth/me` returns `{ authenticated, username?, csrf_token }` and creates a session. Every mutating browser request includes `X-CSRF-Token` from that response. POST `/api/auth/login` accepts `{username,password}`, returns the same session shape. POST `/api/auth/logout` clears login. Credentials are session cookies, HttpOnly, SameSite=Lax.

Authenticated cookies also require a live process-local session nonce. Logout revokes that nonce for copied cookies and existing browser WebSockets; expiration closes the socket with code 1008. A hub restart requires a new browser login. Node credentials are independent of browser sessions.

- GET `/api/hosts` -> `{hosts:[{id,name,platform,online,roots:[string],version,last_seen_at}]}`.
- POST `/api/pairings` -> `{code,expires_at}`. Node pairing is separate below.
- GET `/api/threads?q=...` -> `{threads:[Thread]}`. Thread = `{id,host_id,native_id,title,cwd,status,active_turn_id,last_error,created_at,updated_at}`. `status` = idle|active|waiting|error|interrupted|starting. Nullable `last_error` preserves native error details across reloads and clears when a new turn starts.
- POST `/api/threads` `{host_id,title,cwd,model?}` -> HTTP 202 `{thread,command}`. `cwd` must be inside node-advertised roots.
- GET `/api/threads/<id>` -> `{thread,items:[Item],interactions:[Interaction]}`. Item = `{id,native_id,thread_id,turn_id,type,data,created_at}`. `data` preserves the native Codex item, with optional `_assets:[Asset]` / `_references:[Reference]` added by the platform. GET history must survive reload.
- POST `/api/threads/<id>/messages` `{text,asset_ids:[],references:[],client_request_id}` -> 202 `{command}`. References are `{kind:'task',thread_id,item_id?}` or `{kind:'web',url,title?,excerpt?}`. Resolved immutable snapshots are stored and model-visible quoted text is built by hub. Web content is user-provided excerpt; hub MUST NOT fetch arbitrary URLs.
- POST `/api/threads/<id>/steer` same message shape; POST `/api/threads/<id>/interrupt` `{}` -> 202 `{command}`.
- GET `/api/commands/<id>` -> `{command:{id,kind,status,result,error,...}}`.
- POST `/api/interactions/<id>/answer` `{response: native_response_object}` -> 202 `{command}`. Interaction = `{id,thread_id,method,params,status}`. `params` preserves Codex server request. Only advertised available decisions may be selected.
- POST `/api/assets` multipart `file` -> `{asset:{id,name,mime,size,url}}`, images only, limit 20 MiB. GET `/api/assets/<id>` returns authenticated bytes.
- POST `/api/references/resolve` `{references:[...]}` -> `{references:[{kind,title,url?,thread_id?,item_id?,snapshot_id,excerpt}]}`. Task links `/tasks/<thread_id>?item=<item_id>`.

## Browser WebSocket

`/ws/client` uses session cookie; Origin must match request host. Client sends `{type:'subscribe',thread_id?,after:0}`. Server sends `{type:'ready'}` then events `{type:'event',id: integer,thread_id: string|null,method:string,params:object}`. Event IDs are hub-global monotonic cursors. Persist events before live broadcast. Reconnect and subscribe with last cursor. Global subscription (no thread_id) is permitted for the single administrator. UI can re-fetch a thread on lifecycle/item completion; delta events allow incremental rendering.

## Node HTTP and WebSocket

POST `/api/hosts/pair` (no browser auth or CSRF) `{code,name,platform,roots:[absolute_path],version}` -> `{host:{id,...},token}`. Pairing codes expire, single use. Store token locally; never log it. Node opens `/ws/node` with `Authorization: Bearer <token>`. For local HTTP allow loopback only unless explicit `--allow-insecure`.

Node sends `{type:'hello',runtime_id:string,version,roots:[absolute_path],platform}`. runtime_id is the Codex PROCESS epoch, stable across network reconnects, changes on process restart. Hub sends `{type:'welcome',host_id,protocol:1}`. Heartbeat `{type:'ping'}` -> `{type:'pong'}` every 15 seconds. Single live node owner per host; duplicate rejected.

The connection becomes online only after welcome and durable pending command replay finish. HTTP handlers enqueue without writing sockets; the connection owner drains a bounded queue. Queue overflow disconnects that owner, retaining durable commands for reconnect. Frame writes complete with `sendall` and serialize application and control frames. Replay socket I/O runs outside the global command lock and is bound to the original connection.

Hub sends `{type:'command',command:{id,kind,thread_id,payload}}`.

Kinds:
- `thread.start`: payload `{cwd,model?,title}`. Return raw thread/start result.
- `turn.start` / `turn.steer`: payload `{native_thread_id,input:[native Codex UserInput],expected_turn_id?}`. Images already expressed as `{type:'image',url:'data:image/...;base64,...'}`. A new turn resumes the node-owned thread with `excludeTurns:true` if it is not loaded. Steer and interrupt address the active native turn without waiting for a start/resume RPC. Return raw RPC result.
- `turn.interrupt`: payload `{native_thread_id,turn_id}`.
- `interaction.answer`: payload `{rpc_id,runtime_id,response}`. Reply to original Codex server request only if epoch still matches.

Node durably marks command in SQLite before sending RPC, sends `{type:'command_started',id}` then `{type:'command_result',id,result:object}` or `{type:'command_result',id,error:string}`. Replayed completed commands return cached result. A command with uncertain outcome after process death is `unknown` and MUST NOT execute again automatically.

Node forwards Codex notifications and server requests as `{type:'event',event_id:uuid,runtime_id,seq:integer,method,params,rpc_id?}`. Persist these in SQLite outbox before sending. Server ACK `{type:'event_ack',event_id}` ONLY after DB commit, duplicate events deduplicated by event_id. Preserve raw params. Incoming native `threadId` is mapped by hub to hub thread ID. For server requests, rpc_id is the ORIGINAL request ID (int or string), never replace it. Browser approval response is an RPC result, not a new turn. Unknown events retained and displayed in generic tool card. Reconnect does not restart Codex.

Native errors with `willRetry:true` remain in event history without making the task terminal. Late command results and old turn completions must not replace a newer active turn or clear its error. A task remains waiting until all current-process pending or answering interactions settle; ending the turn or process expires both states. User message display metadata is bound to the matching native input, including steer inputs, rather than guessed from the latest command.

Each connection has at most 100 unacknowledged events in flight. An ACK frees a slot without retransmitting the entire window; unacknowledged events retry after at least five seconds. Duplicate ACKs do not wake the sender. Reconnect resets the in-flight window and replays durable unacknowledged events. Native `serverRequest/resolved` expires cached approval IDs, and a queued answer rechecks validity under the runtime write lock before replying.

Node output image bridge: POST `/api/node/assets` using bearer auth, multipart `file` and `thread_id` (native thread ID), `item_id`, returns `{asset}`. Publish a durable event `anywhere/assets` with `{threadId,itemId,assets:[Asset]}`. Extract images from imageView.path, imageGeneration.savedPath / base64 result, MCP image blocks and local Markdown images. Only read files inside configured project roots and Codex artifact roots (explicit node --asset-root). Never serve arbitrary remote paths. Asset metadata uses hub authenticated URL.

Output extraction is bounded to 16 images and 40 MiB per native item, with 20 MiB and 25 million pixels per image. Excess output images are skipped with a log warning. Reads themselves are capped even if a file grows after its metadata check. Hub attachment reads and downloads must resolve inside its asset directory and handle missing files without an internal error.

## Runtime and permissions

Run `codex app-server --listen stdio://`, initialize once, then `initialized`. Default new threads use `approvalPolicy:'on-request'`, `sandbox:'workspace-write'`; never silently disable approvals. Do not send a hardcoded model by default. Drain stderr separately. Treat stdout as JSONL. On Codex process exit, fail pending RPCs and mark interactions expired through `anywhere/runtime_stopped`. Starting a new Codex process changes runtime_id. Browser disconnect does not stop node or Codex.

RPC deadlines cover write-lock acquisition, pipe backpressure and response waiting. Notification and approval writes also have deadlines. Process shutdown and exit callbacks have bounded waits; inherited tool pipes cannot prevent the direct Codex process exit from being observed.
