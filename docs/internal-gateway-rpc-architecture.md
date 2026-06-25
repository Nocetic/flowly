# Internal Gateway RPC Architecture

How Flowly's first-party clients — the TUI, the desktop app, the iOS app, and the
Chrome extension — talk to the running agent. This is the **internal** client ↔
gateway protocol over a single WebSocket (plus a few companion HTTP routes). It is
**not** the public web-platform REST API (`useflowlyapp.com/api/*`); that is a
separate system. Everything here lives in `flowly/gateway/`, `flowly/channels/`,
and `flowly/tui/client.py`.

> Scope note. Two transports carry the **same** protocol: the **direct gateway**
> (a local aiohttp WebSocket, LAN/self-host) and the **cloud relay** (an outbound
> connection to `relay.useflowlyapp.com` that proxies remote browsers/iOS). A
> reader who understands the direct gateway understands 90% of the relay; §12
> covers the deltas.

---

## Table of contents

1. [TL;DR](#tldr)
2. [Architecture at a glance](#architecture-at-a-glance)
3. [The server](#the-server)
4. [Connection lifecycle](#connection-lifecycle)
5. [Wire protocol — message envelopes](#wire-protocol--message-envelopes)
6. [Authentication model](#authentication-model)
7. [RPC dispatch — the two-tier router](#rpc-dispatch--the-two-tier-router)
8. [Method catalogue — native gateway RPCs](#method-catalogue--native-gateway-rpcs)
9. [Method catalogue — feature RPCs (shared surface)](#method-catalogue--feature-rpcs-shared-surface)
10. [The chat turn — ACK → stream → final](#the-chat-turn--ack--stream--final)
11. [Event reference](#event-reference)
12. [Direct gateway vs cloud relay](#direct-gateway-vs-cloud-relay)
13. [The canonical client (`tui/client.py`)](#the-canonical-client-tuiclientpy)
14. [Companion HTTP routes](#companion-http-routes)
15. [The MCP control plane](#the-mcp-control-plane)
16. [Session persistence & rehydration](#session-persistence--rehydration)
17. [Push notifications](#push-notifications)
18. [Connection management & broadcasting](#connection-management--broadcasting)
19. [Failure modes & resilience](#failure-modes--resilience)
20. [Security model summary](#security-model-summary)
21. [Extending: adding a new method](#extending-adding-a-new-method)
22. [File map](#file-map)

---

## TL;DR

```
TUI / Desktop ───ws://127.0.0.1:18790/ws──┐
                                          ├─▶ GatewayServer ──▶ AgentLoop (one shared loop)
Browser / iOS ──wss://relay…/relay────────┘        │              SessionManager, Board,
   (outbound, JWT)                                  │              Artifacts, Memory, Coaching
                                                    ▼
                          one JSON-over-WS protocol:
        request  {type:"rpc",   id, method, params}
        reply    {type:"rpc",   id, result|error}
        event    {type:"event", event, data}            (server-pushed, no id)
        ping/pong {type:"ping"|"pong", timestamp}
```

- **One socket, many concerns.** RPC calls (request/reply, correlated by `id`) and
  server-pushed events (streaming tokens, tool turns, approvals, coaching tips,
  board/artifact changes) are multiplexed over the same WebSocket.
- **Two-tier dispatch.** Incoming methods are first matched against the
  transport-agnostic **feature RPC** surface (`flowly.channels.feature_rpc`,
  ~51 methods shared verbatim by gateway + relay), then against the gateway's
  **native** handlers (`chat.*`, `coaching.*`, `board.*`, `artifacts.*`, …).
- **Auth follows the bind.** Bound to loopback (the default) → no auth (only local
  processes can reach it). Bound to a routable address → static token + single-use
  WebSocket ticket, plus a DNS-rebinding origin guard.
- **The TUI client is the reference implementation** — id-correlated futures, a
  typed event stream, exponential-backoff reconnect, and an app-level
  ping/watchdog. Mirror it to build any client.

---

## Architecture at a glance

A single long-lived `AgentLoop` owns the conversation. The `GatewayServer` is a
thin aiohttp app that exposes that loop (and the session store, board, artifact
store, memory governance, coaching manager) over a WebSocket. Clients never run
the agent themselves — they send `chat.send` and consume the streamed result.

- **Direct gateway** (`flowly/gateway/server.py`): an aiohttp server the local
  clients dial. Default `127.0.0.1:18790`.
- **Cloud relay** (`flowly/channels/web.py`): the bot dials *out* to the relay
  (like a Telegram bot), and the relay proxies remote clients to it. Same RPC
  frames, wrapped with a `sessionId` for routing.
- **Shared handlers** (`flowly/channels/feature_rpc.py`): config, providers,
  memory, KG, cron, artifacts, sessions, audit, assistants, skills, pairing,
  push. Both transports call the exact same functions, so a remote iOS client and
  a local desktop client see identical shapes.

---

## The server

`GatewayServer` (`flowly/gateway/server.py`) — an **aiohttp** application.

- **Construction.** `__init__(host="127.0.0.1", port=18790, on_chat_message,
  on_voice_message, on_cron_run/reload/health, auth_token=None,
  control_token=None, sessions=…, board=…, artifacts=…, coaching=…, …)`. The host
  wires its subsystems in as references/callbacks; the server holds no agent
  logic of its own. `on_chat_message` is the callback that actually drives the
  LLM — without it, the server is read-only.
- **Default bind.** `127.0.0.1:18790`. Loopback-only unless explicitly bound to
  `0.0.0.0` / a routable IP for self-hosted remote access.
- **CORS.** Permissive (`Access-Control-Allow-Origin: *`) — safe because it is
  loopback-bound; an `OPTIONS` short-circuits with 204 + CORS headers so a Vite
  dev server (`localhost:5173`) can talk to it during development.
- **Body cap.** REST bodies are capped at 1 MB; the WebSocket allows 40 MB frames
  (a 25 MB attachment inflates to ~33 MB base64 + JSON overhead).
- **Routes.** One WebSocket (`GET /ws`) plus companion HTTP routes (health,
  ws-ticket, media, cron, provider, board, artifacts, extension status, voice).
  See §14.

---

## Connection lifecycle

The WebSocket handler is `_handle_ws` (`server.py`). Per connection:

1. **Upgrade.** `ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=40*1024*1024)`
   then `await ws.prepare(request)`. Pre-handshake disconnects are swallowed at
   DEBUG. On a remote bind the upgrade is auth-gated (§6).
2. **Identify.** A `?clientId=<token>` query param (validated `[A-Za-z0-9_-]{1,64}`)
   lets a client *reattach* after a transient drop; absent/invalid → a fresh UUID.
   Registered in `self._ws_clients[client_id] = ws`. A second connection claiming
   the same `clientId` replaces the first (sleep/wake, network blips).
3. **Receive loop.** `async for raw_msg in ws:` handling only `WSMsgType.TEXT`.
   Each frame is JSON-parsed (`Invalid JSON` error frame on failure) and routed by
   `data["type"]`:
   - `"rpc"` → `_handle_ws_rpc(ws, client_id, data)` (§7)
   - `"ping"` → reply `{"type":"pong","timestamp": …}`
   - `"tool_result"` → only honoured from a *registered extension client*
     (the Chrome extension answering a `tool_request`); ignored otherwise.
   `WSMsgType.ERROR`/`CLOSE` breaks the loop.
4. **Disconnect.** Remove from `_ws_clients`. If it was the extension: clear it
   from `_extension_clients`, resolve any pending `tool_request` futures with an
   error, and fail over `_extension_active` to another extension if present. If a
   coaching session was keyed to this client (`coaching:<client_id>`), auto-stop
   it with **background finalization** so the summary/KG/artifact still get
   written.

A background **tick loop** broadcasts `{"type":"event","event":"tick"}` to all
clients every ~10 s as a liveness signal (only while `on_chat_message` is wired).

---

## Wire protocol — message envelopes

All frames are JSON text. Four shapes:

**RPC request** (client → server):
```json
{ "type": "rpc", "id": "<uuid>", "method": "chat.send", "params": { } }
```
`id` is a client-chosen unique string; `method` is dotted (`namespace.verb`);
`params` is method-specific.

**RPC reply — success** (server → client):
```json
{ "type": "rpc", "id": "<same-id>", "result": { } }
```

**RPC reply — error** (server → client):
```json
{ "type": "rpc", "id": "<same-id>", "error": { "code": "INVALID_REQUEST", "message": "Empty message" } }
```
Error codes seen: `INVALID_REQUEST`, `UNKNOWN_METHOD`, `INTERNAL`, plus
handler-specific ones. Built by `_ws_rpc_error`.

**Server-pushed event** (server → client, unsolicited — **no `id`**):
```json
{ "type": "event", "event": "chat", "data": { } }
```
Over the relay, events additionally carry a `sessionId` (the browser UUID) for
routing: `{ "type":"event", "sessionId":"…", "event":"chat", "data":{…} }`.

**Heartbeat** (either direction):
```json
{ "type": "ping", "timestamp": 1234567890 }
{ "type": "pong", "timestamp": 1234567890 }
```

Correlation: a client keeps a map `id → Future`; the reader resolves the future
when a `{type:"rpc", id}` reply arrives. Events bypass that map and go to an event
queue. This is exactly how `tui/client.py` works (§13).

---

## Authentication model

Defined in `flowly/gateway/auth.py` + the ws-ticket handler in `server.py`. The
governing idea: **auth follows the bind mode.**

- **Loopback exception.** If the gateway is bound to a loopback host
  (`127.0.0.1`, `localhost`, `::1`), `_require_auth = False` — no token is
  checked. Rationale: only processes already on the machine can reach loopback, so
  the TUI/desktop connect with zero friction.
- **Remote bind → token required.** Bound to `0.0.0.0` / a routable IP,
  `_require_auth = True`. The static token is `generate_gateway_token()` =
  `secrets.token_urlsafe(32)` (256-bit), persisted at `config.json` →
  `gateway.token` and advertised to local control clients via
  `~/.flowly/gateway-api.json` (`{host, port, token}`, mode 0600). The token
  survives a rebind back to loopback (it's just ignored there), so re-enabling
  remote access doesn't invalidate paired clients.
- **REST auth (middleware).** Every non-public REST route requires the token via
  `X-Flowly-Token: <token>` or `Authorization: Bearer <token>`; checked with a
  constant-time compare. Mismatch → `401 {"error":"unauthorized"}`. `/health` is
  public (clients probe it to learn whether a token is needed).
- **WebSocket auth (self-gated).** The `/ws` upgrade authenticates itself, not via
  middleware. Preferred: a **single-use ticket** minted at
  `POST /api/auth/ws-ticket` (TTL 30 s, consumed on first use), passed as
  `/ws?ticket=<t>`. Fallback for simple clients: the raw token as `/ws?token=<t>`.
- **DNS-rebinding guard.** Non-browser clients (Electron `file://`, the TUI — no
  `Origin` header) are always allowed. A browser client (`http(s)` Origin) must
  have its Origin host equal the request Host, so a malicious web page can't
  cross-origin onto a gateway on a private IP.
- **`/health` handshake.** Returns `{"status":"ok", "auth_required": <bool>,
  "capabilities": ["tool_events", …]}`. The desktop probes this before its first
  WS connect to decide whether to prompt for a token.

---

## RPC dispatch — the two-tier router

`_handle_ws_rpc` (`server.py`) reads `{method, id, params}` and routes in **two
tiers**, feature-surface first:

```python
if method == "health":
    reply({"ok": True})
elif method in feature_rpc.FEATURE_METHODS:          # tier 1 — shared surface
    await self._handle_feature_rpc(ws, rpc_id, method, params)
elif method == "sessions.list": …                    # tier 2 — native handlers
elif method == "chat.send":    …
…
else:
    error("INVALID_REQUEST", f"unknown method: {method}")
```

**Why feature-first:** the shared `feature_rpc` handlers (served identically by the
relay) define the canonical shapes; checking them first means the unified
(superset) result wins over any older native handler of the same name. Native
handlers cover the things that need direct access to the live agent loop / WS
(streaming chat, coaching, exec approvals, extension tool bridge).

**The feature surface** is a single table in `feature_rpc.py`:
```python
_DISPATCH: dict[str, tuple[handler, wants_params, restart_aware]] = { … }
FEATURE_METHODS = frozenset(_DISPATCH)

async def dispatch(method, params) -> tuple[result, needs_restart]:
    fn, wants_params, restart = _DISPATCH[method]
    result = fn(params) if wants_params else fn()
    if isawaitable(result): result = await result
    needs_restart = bool(restart and result.get("willRestart"))
    return result, needs_restart
```

`restart_aware` is the key mechanism for config mutations: a handler that changes
something the running agent caches (provider, model, a connection token) returns
`{"willRestart": true}`. The transport **ACKs the RPC first, then bounces the
gateway in the background** — the client gets its reply before the socket drops,
then reconnects to the restarted agent. (See `provider.set`, `model.set`,
`config.set`, `connections.set` in §9.)

---

## Method catalogue — native gateway RPCs

Handled directly in `_handle_ws_rpc` (need the live agent loop, the WS, or
streaming). "LLM?" marks methods that run a model.

| Method | Params | Result | LLM? |
|---|---|---|---|
| `health` | — | `{ok:true}` | — |
| `chat.send` | `{message, sessionKey?, attachments?, idempotencyKey?, cwd?, voiceMode?}` | `{runId, status:"accepted"}` then a stream of events (§10) | **yes** |
| `chat.abort` | `{runId}` | `{ok:true}` | — |
| `chat.history` | `{sessionKey}` | `{sessionKey, messages:[…], thinkingLevel?}` | read |
| `chat.compact` | `{sessionKey, instructions?}` | summariser result | **yes** |
| `chat.clear` | `{sessionKey}` | `{ok}` | write |
| `chat.retry` | `{sessionKey}` | `{ok, text, removed}` | **yes** |
| `chat.undo` | `{sessionKey}` | `{ok, text, removed}` | write |
| `subagents.list` | `{status?}` | `{tasks:[{runId,label,task,model,status,duration,…}]}` | read |
| `subagents.cancel` | `{runId}` | cancellation result | — |
| `exec.approval.list` | — | `{approvals:[{id,command,sessionKey,expiresAt,supportsAlways}]}` | read |
| `exec.approval.resolve` | `{id, decision:"allow-once\|allow-always\|deny"}` | `{ok:true}` | — |
| `exec.policy.get` | — | `{security, ask, allowlist:[…]}` | read |
| `exec.policy.set` | `{security?, ask?}` | policy `+ {willRestart}` | write |
| `exec.policy.allowlist.remove` | `{pattern}` | policy `+ {removed}` | write |
| `coaching.start` | `{context?, language?, frequency?, sessionId?}` | `{sessionId, status, …}` then `coaching.*` events | **yes** |
| `coaching.segment` | `{text, source?, screenshot_b64?, sessionId?}` | result | **yes** |
| `coaching.askNow` | `{screenshot_b64?, sessionId?}` | result | **yes** |
| `coaching.stop` | `{sessionId?}` | `{sessionId, …}` | finalize |
| `coaching.state` / `coaching.snapshot` | `{sessionId?}` | `{active, …}` | read |
| `coaching.update` | `{context?, frequency?, language?, sessionId?}` | result | write |
| `extension.register` | — | `{ok:true}` | — |

> `sessions.list` / `sessions.delete` / `chat.history` and the `artifacts.*`,
> `assistants.*`, `audit.*`, `board.*` families *also* appear on the native side
> historically, but the **feature surface** (§9) now owns the canonical shapes and
> wins via feature-first dispatch.

The **extension tool bridge** is unusual: when a tool needs the browser (e.g.
read the active tab), the server pushes `{type:"tool_request", id, action, params}`
to the active extension client and parks a future in `_extension_pending`; the
extension answers with a top-level `{type:"tool_result", id, result}` frame
(not an RPC), which resolves the future.

---

## Method catalogue — feature RPCs (shared surface)

`flowly/channels/feature_rpc.py` — pure, mostly-stateless handlers that read/write
`~/.flowly/` (config.json, sqlite stores, jsonl sessions, the artifact store). The
**same functions** back both transports, so local and remote clients are at parity.
`restart` = the `willRestart` ACK-then-bounce pattern (§7).

| Method | Params → Result (abridged) | restart |
|---|---|---|
| `connections.list` | — → `{connections:[{key,label,enabled,connected,probeStatus,values,fields,needsRestart}]}` | |
| `connections.set` | `{key, values?, clear?}` → `{ok, willRestart}` | ✓ |
| `gmail.set_credentials` | `{credentials}` → `{ok, willRestart}` | ✓ |
| `config.get` / `config.set` | — / `{config?\|patch?, restart?}` → raw config / `{ok, willRestart}` | set: ✓ |
| `memory.entries` | — → `{memory:[{date,content}], user}` (MEMORY.md / USER.md) | |
| `memory.update_user` | `{content}` → `{ok}` | |
| `memory.gov_list` / `review` / `stats` | `{status?}` → `{items\|stats}` (governance DB) | |
| `memory.accept` / `reject` / `correct` / `feedback` | `{id, …}` → `{item}` | |
| `kg.graph` | — → `{entities:[…], triples:[{subject,predicate,object,valid_from,valid_to,confidence,current}]}` | |
| `kg.delete_entity` | `{id}` → `{ok}` | |
| `persona.list` | — → `{personas:[…], active}` | |
| `provider.active` | — → `{provider:{key,source,apiBase}, model}` | |
| `provider.list` | — → `{providers:[{key,name,keyable,hasKey,isActive}], active, resolved}` | |
| `provider.set` / `set_key` / `set_flowly_account` | `{…}` → `{ok, …, willRestart}` | ✓ |
| `model.list` | `{forceRefresh?}` → `{provider, models:[{id,name,contextWindow,tags}]}` | |
| `model.set` | `{model}` → `{ok, model, willRestart}` | ✓ |
| `skills.list` | — → `{skills:[{slug,name,description,category,source,installed}]}` | |
| `assistants.list` / `write` / `delete` | `{…}` → `{assistants\|success\|deleted}` | |
| `artifacts.list/get/update/delete/pin/versions` | `{…}` → `{artifact(s)\|versions\|ok}` | |
| `sessions.list` | — → `{sessions:[{key,fileName,sizeBytes,modifiedAt,channel,chatId,title,updatedAt}]}` | |
| `sessions.read` | `{key}` → `{messages:[…jsonl…]}` | |
| `audit.list` / `stats` / `clear` | `{filters?}` → `{entries,total,has_more}\|{stats}\|{success}` | |
| `cron.list/add/update/remove/run/output` | `{…}` → `{jobs\|job\|ok\|outputs}` | |
| `pairing.list` / `approve` | `{channel, code?}` → `{requests}\|{ok,approved}` | |
| `push.register` / `unregister` | `{pushId, pushSecret, gatewayId?, platform?}` → `{ok}` | |

`dispatch(method, params)` raises `FeatureRpcError("UNKNOWN_METHOD"|…)` for a bad
method or a structured handler error; any other exception propagates so the
transport maps it to `INTERNAL`.

---

## The chat turn — ACK → stream → final

`chat.send` is asynchronous and streamed. The lifecycle (direct gateway):

1. **ACK.** The server validates, registers a background task keyed by `runId`
   (the `idempotencyKey`, else a fresh UUID), and immediately replies
   `{type:"rpc", id, result:{runId, status:"accepted"}}`. The client now has a
   `runId` to correlate the stream and to `chat.abort`.
2. **Token deltas.** As the model streams, the agent's stream callback emits
   `{type:"event", event:"agent", data:{runId, stream:"assistant", data:{text:"<delta>"}}}`.
   (The relay instead emits `event:"chat", data:{state:"streaming", runId, delta}` —
   same information, different shape; §12.)
3. **Iteration steps.** Each time the loop appends an `assistant_with_tool_calls`
   or a `tool_result`, an `iteration_step` event fires with the structured tool
   metadata, so the desktop's tool-turn panel fills in live:
   ```json
   {"type":"event","event":"chat","data":{"state":"iteration_step","runId":"…",
     "iterationIdx":N,"role":"assistant|tool","content":"…",
     "tool_calls":[{id,type,function:{name,arguments}}]|null,
     "tool_call_id":"…"|null,"name":"…"|null}}
   ```
4. **Final.** When the turn (and all tool loops) complete:
   ```json
   {"type":"event","event":"chat","data":{"state":"final","runId":"…","sessionKey":"…",
     "model":"<actual-model-after-fallback>",
     "message":{"role":"assistant","content":[{"type":"text","text":"<full text>"}],
       "usage":{"prompt_tokens":…,"completion_tokens":…,"total_tokens":…,
                "cache_read_tokens":…,"cache_write_tokens":…}}}}
   ```
   `usage` drives the context-window indicator; `model` reports what actually ran
   (after any fallback/rotation).
5. **Abort / error.** `chat.abort` → `{state:"aborted", runId}`; an exception →
   `{state:"error", runId, errorMessage}`.

Events are **broadcast to all connected clients** (one shared loop), so a second
desktop window or the TUI sees the same turn live.

---

## Event reference

Every server-pushed event (`{type:"event", event, data}`; relay adds `sessionId`):

| Event | Data | Fired when |
|---|---|---|
| `agent` (stream) | `{runId, stream:"assistant", data:{text}}` | each token delta (direct gateway) |
| `chat` `streaming` | `{state:"streaming", runId, delta}` | each token delta (relay) |
| `chat` `iteration_step` | `{state:"iteration_step", runId, iterationIdx, role, content, tool_calls?, tool_call_id?, name?}` | a tool-call / tool-result message is appended |
| `chat` `final` | `{state:"final", runId, sessionKey, model, message:{content, usage}}` | turn complete |
| `chat` `aborted` | `{state:"aborted", runId, sessionKey}` | `chat.abort` |
| `chat` `error` | `{state:"error", runId, sessionKey, errorMessage}` | exception in the turn |
| `tool.start` | `{toolCallId, name, args, sessionKey}` | a tool begins |
| `tool.complete` | `{toolCallId, name, success, durationMs, preview, sessionKey}` | a tool ends |
| `exec.approval.requested` | `{id, command, sessionKey, expiresAt, supportsAlways}` | a command needs approval |
| `coaching.tip` | `{sessionId, text, confidence, timestamp, seq}` | coach emits a tip |
| `coaching.transcript` | `{sessionId, text, source:"mic"\|"system", seq}` | transcript segment |
| `coaching.finalized` | `{sessionId, …summary}` | coaching session ends |
| `coaching.gate_decision` | `{sessionId, timestamp, …}` | diagnostics (gate panel) |
| `artifact.created` / `updated` / `deleted` | artifact dict / `{id}` | artifact lifecycle |
| `subagent.started` / `completed` | `{runId, label, task, model[, status, error]}` | delegate tool |
| `compaction` | `{phase:"started"\|"completed", tokensBefore, tokensAfter, messagesRemoved}` | context auto-compaction |
| `tick` | `{}` | every ~10 s (liveness) |
| `agent_state` | `{state:"active"\|"idle"}` | turn-level presence (extension clients only) |

Capabilities advertised in `/health` (`capabilities`) tell a client which event
families to expect (e.g. `tool_events`).

---

## Direct gateway vs cloud relay

Two transports, **one protocol**. The relay exists so remote clients (browser,
iOS) can reach a bot behind NAT without port-forwarding.

| | Direct gateway | Cloud relay |
|---|---|---|
| Where | `flowly/gateway/server.py` | `flowly/channels/web.py` |
| Socket | clients dial **in** to `ws://host:18790/ws` | the bot dials **out** to `wss://relay…/relay?token=<jwt>` |
| Clients | TUI, desktop (local) | browser, iOS (remote) |
| Auth | loopback-free / token+ticket (§6) | bot mints a 24 h **agent JWT** (`{type:"agent", serverId, gatewayAuthToken, exp}`), signed with the relay JWT secret; relay validates + routes by `serverId` |
| Routing | one shared loop, broadcast to all | per-browser `sessionId`; the bot keeps `sessionKey → relay_id` so events route back to the right browser |
| Frame delta | `event:"agent"` stream deltas; no `sessionId` | `event:"chat", state:"streaming"` deltas; every frame carries `sessionId` |
| Resilience | client reconnects | bot **queues outbound** payloads (bounded ~50, FIFO) while the WS is down and **flushes on reconnect**; images compressed to fit the relay's ~10–15 MB frame cap |

Crucially, **feature RPCs run the same handlers on both** — `_handle_feature_rpc`
in the gateway and in `web.py` both call `feature_rpc.dispatch()`. Config you set
from the iOS app and config you set from the desktop hit the same `config.json`.

Client choice is static: the TUI and desktop always use the direct local gateway;
remote browser/iOS always use the relay.

---

## The canonical client (`tui/client.py`)

`GatewayClient` is the reference client — any new client (iOS, a custom tool)
should mirror it.

- **Connect.** URL is `ws://host:port/ws[?token=…]`, or a `url_provider()`
  coroutine that returns a fresh URL each reconnect (the relay client uses this to
  mint a fresh JWT). Opens the aiohttp WS with `heartbeat=20, autoping=True`, then
  starts a reader task, a heartbeat watchdog, and a supervisor (reconnect) task.
- **RPC call.** `_rpc(method, params)` sends `{type:"rpc", id:uuid4, method, params}`
  and returns the id; `_await_reply(id, timeout)` parks a future in
  `self._pending[id]` and awaits it. The reader resolves the future when the
  matching `{type:"rpc", id, result|error}` arrives.
- **Events.** `events()` is an async iterator over an inbox queue. `_dispatch`
  routes inbound frames: `pong` clears the watchdog flag, `rpc` resolves the
  pending future, `event` is parsed into a **typed dataclass**
  (`StreamDelta`, `ChatFinal{usage}`, `ToolStart/ToolComplete`, `ApprovalRequest`,
  `SubagentStarted/Completed`, `CompactionEvent`, …) and enqueued; unknown events
  pass through raw.
- **Reconnect.** A supervisor awaits the reader's exit, then reconnects with
  exponential backoff `(1,2,4,8,15,30,30,…)`, re-resolving the URL each time. A
  **rapid-drop guard** gives up after 3 drops within 10 s (so a bad token doesn't
  spin forever) by emitting `ConnectionLost`.
- **Heartbeat.** App-level, not just aiohttp's: every 2 s it checks silence; after
  30 s quiet with no pending pong it sends `{type:"ping", timestamp}`; if no pong
  within ~8 s it force-closes the socket (code 4000) to trigger a reconnect.

---

## Companion HTTP routes

Non-WS routes on the same aiohttp app (auth per §6; `/health` public):

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | handshake → `{status, auth_required, capabilities}` |
| `/api/auth/ws-ticket` | POST | mint a single-use WS upgrade ticket → `{ticket, ttl_seconds:30}` |
| `/api/media` | GET | `?id=<basename>` → a saved attachment as a base64 `dataUrl` (history image previews; basename-only id, media-dir containment, image allowlist, 25 MB cap) |
| `/api/voice/message` | POST | voice inbound webhook → `{response}` |
| `/api/cron/run` · `/reload` · `/health` | POST/POST/GET | trigger a job · reload jobs · cron status |
| `/api/provider/active` · `/reload` | GET/POST | report · live-reload the active LLM provider |
| `/api/extension/status` | GET | Chrome-extension connection status |
| `/api/board` · `/api/board/action` | GET/POST | board snapshot · mutation (polling fallback for the WS RPCs) |
| `/api/artifacts` · `/{id}` · `/{id}/versions` | GET | artifact reads (polling fallback) |

These HTTP forms duplicate some RPCs so a client that can't hold a socket (or a
script) can still read board/artifacts/provider state and fetch media.

---

## The MCP control plane

A **separate, opt-in** authed surface (`flowly/mcp/server/control.py`), registered
only when both an `on_send` callback and a `control_token` are supplied. It lets
an MCP "write plane" (e.g. `flowly mcp serve --allow-writes`) push into the agent:

| Route | Auth | Purpose |
|---|---|---|
| `POST /control/messages/send` | Bearer (`control_token`) | inject a message → `{sent, target}` |
| `GET /control/approvals` | Bearer | list pending exec approvals |
| `POST /control/approvals/resolve` | Bearer | resolve one → `{resolved, id}` |

Distinct from `/ws` RPC: a different token (the MCP control token, advertised in
`gateway-api.json`), a small HTTP surface, and a different consumer (MCP clients,
not the first-party apps).

---

## Session persistence & rehydration

Conversations are persisted as **JSONL** under `~/.flowly/sessions/<key>.jsonl`
(`flowly/session/manager.py`, index in `session/indexer.py`):

- **Line 1** is a metadata record (`created_at`, `updated_at`, title, `metadata`).
- **Lines 2+** are messages. `extend_with_turn_messages(user_content, new_messages,
  final_content, usage, media)` appends a completed turn at once, preserving the
  LLM-protocol fields (`tool_calls` on assistant; `tool_call_id`+`name` on tool),
  per-message `usage`, and `media` (attachment paths).
- **`get_history()`** projects each stored message to the bare protocol shape via
  `_project_for_llm` (an allowlist: `role`, `content`, and for assistants
  `tool_calls` **verbatim**) and runs `_repair_tool_sequence` to trim orphaned
  tool-call/tool-result pairs (so a crashed-mid-turn session resumes without a
  provider 400).

`chat.history` reconstructs the UI view: text content wrapped as
`[{type:"text", text}]`, tool fields carried through, `usage` attached (so the
context-window bar hydrates without re-running the model), and attachment previews
rebuilt lazily — the message keeps `mediaId`s and the client fetches full images
via `/api/media?id=…`. `sessions.list` reads the per-file metadata for the picker.

---

## Push notifications

`flowly/push/relay_push.py` — an **account-free** push path so a closed app can be
woken (e.g. a cron result):

1. The device registers anonymously with the relay and gets an opaque
   `(pushId, pushSecret)`.
2. It hands those to the bot via the `push.register` feature RPC; the bot stores
   them locally (`~/.flowly/push_subs.json`).
3. To notify, the bot POSTs to the relay's `/api/push/send` with
   `Authorization: Bearer <pushSecret>` (`notify_devices(title, body, …)`); the
   relay fans out to APNs/etc.

No Flowly account is involved — the device drives the registration, the bot only
stores the opaque pair.

---

## Connection management & broadcasting

- `self._ws_clients: dict[client_id → ws]` is the connection registry;
  `_extension_clients` / `_extension_active` track the Chrome extension;
  `_extension_pending` holds in-flight `tool_request` futures; `_active_tasks`
  tracks running `chat.send` background tasks by `runId`.
- **Broadcast** (events): iterate `self._ws_clients.values()` and send to each.
  Extension-only events iterate `_extension_clients`. Single-client replies go to
  the originating `ws`.
- **Reattach:** a reconnecting client reuses its `clientId` and the new socket
  replaces the old registry entry; coaching sessions keyed by `client_id` survive
  the blip.

---

## Failure modes & resilience

- **Restart-aware mutations** (§7): config/provider/model changes ACK first, then
  bounce the gateway, so the client never loses its reply to a socket drop.
- **Client reconnect** (§13): exponential backoff + rapid-drop give-up + URL
  re-resolution; an app-level ping/watchdog detects a half-open socket aiohttp's
  own heartbeat misses.
- **Relay outbound queue** (§12): payloads survive a relay blip and flush in order
  on reconnect; oversized images are compressed to fit the frame cap.
- **Tool-sequence repair** on history load prevents provider 400s after a
  crash-mid-turn.
- **Idempotency:** `chat.send` carries an `idempotencyKey` used as the `runId`, so
  a retried send doesn't double-run a turn.
- **Coaching auto-finalize** on disconnect so a dropped client still gets its
  summary/KG/artifact.

---

## Security model summary

- **Loopback = trusted, remote = authed.** Default loopback bind needs no token;
  remote bind needs the static token + a short-TTL single-use WS ticket, with a
  DNS-rebinding origin guard for browser clients.
- **Tokens are real secrets**, kept out of the OSS repo and out of chat surfaces;
  the gateway token lives in `config.json`/`gateway-api.json` (mode 0600).
- **Media is contained**: `/api/media` accepts a basename only, resolves inside
  the media dir (symlink-safe), enforces an image allowlist and a 25 MB cap.
- **The extension tool bridge only trusts registered extension clients** for
  `tool_result` frames.
- **The MCP control plane is a separate, opt-in, separately-tokened surface.**

---

## Extending: adding a new method

- **Prefer a feature RPC.** If the method is a pure read/write of `~/.flowly/`
  state (config, a store, sessions), add a handler in `feature_rpc.py` and one row
  to `_DISPATCH` (`(handler, wants_params, restart_aware)`). It is then served by
  **both** the gateway and the relay automatically, and appears in
  `FEATURE_METHODS`. Return `{"willRestart": true}` if the agent must reload.
- **Use a native handler** only when you need the live agent loop, streaming, the
  WS itself, or per-connection state (a new `chat.*`/`coaching.*`-style method):
  add an `elif` in `_handle_ws_rpc` and an `_ws_rpc_*` method.
- **Events:** broadcast `{type:"event", event:"<name>", data:{…}}` to
  `self._ws_clients`; add a typed dataclass + parse branch in `tui/client.py`'s
  `_dispatch` so the reference client surfaces it.
- Keep params/result shapes identical across transports — the whole point of the
  feature surface is one shape everywhere.

---

## File map

| Concern | File |
|---|---|
| Gateway server, `/ws`, native RPCs, HTTP routes, events | `flowly/gateway/server.py` |
| Token + ws-ticket auth, loopback/origin checks | `flowly/gateway/auth.py` |
| Shared feature-RPC surface + `_DISPATCH` table | `flowly/channels/feature_rpc.py` |
| Cloud relay transport (outbound JWT, sessionId routing, outbound queue) | `flowly/channels/web.py` |
| Reference client (connect, RPC correlation, events, reconnect, heartbeat) | `flowly/tui/client.py` |
| MCP control plane (`/control/*`) | `flowly/mcp/server/control.py` |
| Session persistence (jsonl, projection, repair) | `flowly/session/manager.py`, `flowly/session/indexer.py` |
| Anonymous push relay | `flowly/push/relay_push.py` |
| Gateway config (host/port/token) | `flowly/config/schema.py` (`GatewayConfig`) |
