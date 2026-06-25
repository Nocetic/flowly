# Bot — Feature RPC, auto-titles, streaming

How the bot serves the desktop/iOS clients over **both** transports, plus
the auto-title, streaming, and Gmail-credential work.

## One RPC surface, two transports

Every "feature" the clients call — list models, set a channel, approve a
pairing code — is a handler in `flowly/channels/feature_rpc.py` behind a
single dispatch table:

```python
_DISPATCH = {
    "connections.list":      (connections_list, False, False),
    "connections.set":       (connections_set,  True,  True),
    "gmail.set_credentials": (gmail_set_credentials, True, True),
    "model.list":            (model_list, True, False),
    "model.set":             (model_set,  True, True),
    "provider.active":       (provider_active, False, False),
    "pairing.list":          (pairing_list, True, False),
    "pairing.approve":       (pairing_approve, True, False),
    # …
}   # method → (handler, wants_params, restart_aware)
```

`feature_rpc.dispatch(method, params)` returns `(result, needs_restart)`.
Both transports wrap it:

- **Relay** (`flowly/channels/web.py`) serves these handlers directly.
- **Gateway** (`flowly/gateway/server.py`): any method in
  `feature_rpc.FEATURE_METHODS` is routed to `feature_rpc.dispatch`
  (`_handle_feature_rpc`). A `needs_restart` result ACKs **first**, then
  bounces the gateway in the background (`_schedule_feature_restart` →
  `restart_gateway`) — the restart kills the socket, so awaiting would cut
  the reply before it flushed; the client reconnects on its own.

**So adding one entry to `_DISPATCH` lights an RPC up over relay AND
gateway at once.** That's why the iOS model picker / connections panel
"just work" on either transport.

### Hot reload vs restart

`model.set` and `provider.*` call `_provider_reload_cb` when it's wired
(gateway does, in `gateway_cmd.py`), rebuilding the provider live and
returning `willRestart: False` — no process bounce. Channel changes
(`connections.set`) return `willRestart` from `card.needs_gateway_restart`
and the gateway restarts itself.

## Auto-titles

After the **first** user→assistant exchange, the agent loop names the
session from the opening messages so every surface (CLI / desktop / iOS)
shows the same descriptive name instead of a random session-key suffix.

- `flowly/session/title.py::generate_title` — a small LLM call
  (`max_tokens=2048` so reasoning models finish thinking before the
  visible title; `<think>` blocks stripped; an error-shaped or
  `finish_reason=="error"` response is rejected, never used as a title).
- `flowly/agent/loop.py`:
  - `_maybe_autotitle_session` fires once (first user turn, no existing
    title, non-system session). It keeps a **strong reference** to the
    task in `self._title_tasks` — a bare `create_task()` is only weakly
    held by asyncio and was being garbage-collected mid-flight on a busy
    gateway ("works locally, no title on the server"). Logs scheduling /
    success / empty / failure.
  - `_autotitle_session` saves `session.metadata["title"]` and fires
    `_on_session_titled(session_key, title)`.
- Surfaced to clients via `sessions.list` (`feature_rpc.sessions_list`
  reads the title from each session's metadata line and uses it for
  `displayName`).

### Pushing the title to the relay

Gateway clients read the title from `sessions.list`. Relay clients read
titles from **Firestore**, which the bot can't write (no E2E key). So the
title is pushed: `_on_session_titled` → `web.py::send_title_event` sends a
`conversation.title` event to the relay (same plumbing as compaction
events); the **relay** encrypts and persists it. Gateway sessions have no
relay mapping, so the push no-ops for them.

## Real streaming for xAI Grok

`XAIResponsesProvider.chat_stream` (`flowly/providers/xai_responses_provider.py`)
was an MVP stub that yielded the whole reply in one chunk. It now streams
the Responses SSE API (`stream:true`): `response.output_text.delta` events
become live text deltas, then a final chunk carries `tool_calls` + `usage`
parsed from the authoritative `response.completed` payload — matching the
OpenRouter provider's contract that `loop._chat_with_stream` consumes. Any
streaming failure degrades to the blocking `chat()` call, so a turn is
never empty. Covered by `tests/test_xai_stream.py` (fake-httpx SSE replay).

## `gmail.set_credentials` RPC

Gmail is OAuth; the web flow delivers tokens to the bot host over SSH
(server IP + password), which relay/gateway bots don't have. The
transport-native push: `gmail_set_credentials(params)` validates the
credentials (`refresh_token` required) and writes
`~/.flowly/credentials/gmail.json` via `gmail_auth.save_credentials`.
Registered in `_DISPATCH`, so it works over relay + gateway. The
remaining proxy-refresh work (so the Google client secret never reaches
the client) is tracked in the desktop project memory.
