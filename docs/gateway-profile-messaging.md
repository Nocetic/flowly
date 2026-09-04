# Gateway-owned profile messaging

## Contract

Profile messaging is a capability of the authenticated host, not the UI used
to submit a turn. The primary gateway discovers profiles and routes requests;
clients do not supply directory authority. Session keys, transcript storage,
stream events and existing relay envelopes remain unchanged.

The primary gateway binds direct runs to its ProfileHost. Queued user messages
receive the same authority at the agent execution boundary, after the session
lock is acquired. The authority is task-local, immutable and revoked at turn
completion. JSON metadata cannot construct it. Background/system turns do not
automatically inherit user authority.

Managed child gateways retain the authenticated reverse-RPC protocol. Their
controller may be ProfileHost or a legacy local Desktop controller; children
do not independently scan and launch siblings. Primary gateways prefer their
own host and never broadcast a delegation request to UI clients.

## Permission and lifecycle invariants

- Resolve target names to stable profile IDs; validate them against the host.
- Preserve existing positive tool grants and access-policy enforcement.
- Internal consultation remains read-only, apart from bounded follow-ups.
- A missing broker must not be replaced by a fabricated delivery channel.
- Each individual message gets its own target run ID, even within one parent
  correlation. Correlation is for tracing, not a per-message idempotency key.
- Root requests remain serialized per target. Nested requests refuse a busy
  target instead of introducing a cyclic wait between active agents.
- Parent cancellation aborts the target, including cancellation racing its ACK.
  Aborting an offline target never starts it.
- Nested requests carry their source run ID. A source terminal cancels only
  that run's requests, not a newer turn sharing the same session. Older
  controllers without this additive field retain their existing behavior.
- Buffered error/aborted terminal events cannot become successful replies.
- UI disconnect is not runtime ownership; accepted host work must not depend
  on the original UI remaining connected.

## Scope

No email integration, relay-service changes, database migration, session-key
rewrite, profile merge, UI redesign or automatic widening of tool permissions.
Persistent job resumption and long-running task delegation are separate work.
This change routes short request/reply collaboration; it does not promise
delivery or resumption across a gateway process restart.

## Verification gates

- Default direct chat: iOS/web/Desktop session keys, no client directory.
- Queued relay chat: identity minted at execution, not copied from a socket.
- Simultaneous conversations: isolated session, correlation and result routing.
- Named profile: the existing managed-runtime reverse RPC remains compatible.
- Forged directory/source, unsupported targets, self-message and hop limits.
- Tool allow/deny policies are enforced independently of transport.
- Cancellation, early terminal errors, distinct messages and nested busy targets.
- Existing profile lifecycle, group, abort, provider-error and full unit suites.
- Real authenticated WebSocket and agent-loop tests without paid model calls.

All changes stay on `codex/gateway-profile-messaging`; no merge or deployment
is part of this task. Updating a remote host is a separate release action.

### Verification result

The default repository suite passed: **4806 passed, 1 skipped, 12 deselected**.
The deselected tests require real model calls; no paid provider calls or
physical-device tests were performed. The suite reports 12 warnings, including
an existing subprocess teardown warning. The touched existing files retain
the same 41 lint findings as the base checkout; both new Python files pass
lint. The diff whitespace check is clean.

The new wire tests exercise an authenticated WebSocket and queued relay input
through the actual agent loop, tool execution and host broker, with a
deterministic provider and child-runtime RPC double. They also cover a child
reply arriving before its send acknowledgement. These tests do not substitute
for a packaged Desktop/iPhone smoke test against an updated remote gateway.
