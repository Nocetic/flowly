# The chat wire protocol

The bot is the only writer of chat state. Desktop, iOS and the TUI are readers
that rebuild a live transcript from the events below. Those events travel over
two transports — the **relay** (cloud, `web:` session keys) and the **direct
gateway** (local/remote WebSocket, `desktop:` / `ios:` keys) — carrying the
same shapes, because one bot process serves both and no surface may see a
different conversation than another.

This document is the contract. It exists because the rules below used to live
only as assumptions scattered across four repositories, and a single new
message type that violated one of them (see *The compaction separator*) broke
three clients in three different ways at once.

**If you change an event's shape or add a producer of `state:"final"`, update
this file and `tests/test_chat_wire_conformance.py` in the same commit.**

---

## 1. Envelope

Every server-pushed event is:

```jsonc
{"type": "event", "event": "<name>", "data": { … }}
```

The relay adds `"sessionId"` alongside `event` for its own routing. Clients
never read `sessionId`; they read `data.sessionKey`.

RPC replies are `{"type": "rpc", "id": "<id>", "result": …}` and are addressed
to ONE caller. They are never conversation state.

---

## 2. Identity

| Field | Meaning |
| --- | --- |
| `sessionKey` | The conversation. `<channel>:<chat_id>`, e.g. `web:abc123`, `desktop:default`. The unit of routing, locking, compaction and history. |
| `runId` | One turn's lifecycle: the stream, its tool turns, its Stop, its terminal event. |
| `streamRunId` | Relay only. Present when the durable message id (`runId`) differs from the lifecycle id; then `streamRunId` is the lifecycle one. |

**Invariants**

- `sessionKey` MUST be present on every conversation-scoped event. Without it
  the relay can only reach the socket that started the turn — a client that
  reconnected mid-turn silently loses the rest of its own reply.
- A `runId` identifies exactly one turn. It is never reused, and an event
  carrying an unknown `runId` MUST NOT be adopted onto whatever run happens to
  be visible — with one legacy exception, below.
- Clients MAY map a terminal whose `runId` was never seen streaming onto the
  visible run (older bots minted a fresh id for the final). This fallback is
  why rule 4.2 exists.

---

## 3. Events

### `chat` — the conversation itself

Discriminated by `data.state`.

| `state` | Meaning | Key fields |
| --- | --- | --- |
| `streaming` | One delta of assistant text. | `delta` |
| `iteration_step` | One tool-protocol step inside the turn. | `iterationIdx`, `role`, `content`, `tool_calls?`, `tool_call_id?`, `name?` |
| `final` | **Terminal.** The turn's durable assistant message. | `message.content[]`, `model`, `usage`, `completedAt`, `durationMs`, `aborted?`, `attachments?`, `toolMessages?` |
| `aborted` | Legacy early terminal. The authoritative `final, aborted:true` still follows. | — |
| `error` | **Terminal.** Provider/agent failure as a product state. | `errorCode`, `errorTitle`, `errorMessage`, `retryable` |

### `compaction` — history being summarised

`data`: `phase` (`started` | `completed` | `failed`), `sessionKey`,
`compactionId`, `tokensBefore`, `tokensAfter`, `messagesRemoved`.

`compactionId` identifies one cycle: every phase of it carries the same value,
so a client can tell whether the `completed` it just received closes the
`started` it is showing. It is also the document key the relay uses for the
transcript boundary row, which makes writing that row idempotent.

On `phase:"completed"` this event is what draws the transcript's context
boundary — see §4.2. Lifecycle rules in §5.

### `goal.updated` — durable standing-goal state

`data`: `sessionKey` and the complete `goal` snapshot, including its stable
`goalId`, monotonic `revision`, lifecycle `status`, budget, wait barrier,
completion contract, subgoals and gates.

This is additive state, not a chat turn: it never carries `state:"final"` and
never settles a `runId`. The bot emits it after the related visible `chat`
terminal so a goal cannot begin auxiliary evaluation before the user sees the
reply. The relay fans it out through the same owner-scoped conversation topic
as chat and plan events. `goal.get` is the canonical reconnect source; a client
may ignore this event entirely and still recover the current snapshot later.

### Others

`exec.approval.requested`, `agent.clarify.requested`, `plan.updated`,
`plan.approval.requested`, `goal.updated`, `tool.*`, `cron.*`, `agent_state`,
`tick`.

Only `chat`, `plan.updated`, `plan.approval.requested`, `goal.updated` and
`compaction` are **conversation-scoped** — the relay fans those out to every
socket viewing the conversation. Everything else is request-scoped and goes
to one socket.
Adding a name to that set (`CONVERSATION_EVENT_NAMES` in the relay's
`stream-routing.ts`) means it will reach other devices: only do it for events
that describe the conversation, never for control traffic.

---

## 4. Turn lifecycle

```
chat.send ─▶ [streaming | iteration_step]* ─▶ final | error
                                    ▲                │
                              (Stop) └── aborted ────┘
```

**4.1 — Exactly one terminal per run.** `final` and `error` are terminal.
After a terminal, later `streaming` deltas for that `runId` are late and MUST
be dropped; a client that reopens the bubble on a late chunk leaks one turn's
text into the next.

**4.2 — Only a turn terminal may wear a terminal state.** `state:"final"`
means "this turn is over". Nothing else may ride it, and no consumer should
have to inspect a message's TEXT to find out whether a terminal is real.

> **The compaction separator, and why it is no longer a message.**
> `[context-optimized]` was a transcript divider published mid-turn — while
> the run that triggered compaction was still streaming — through the ordinary
> reply path, so it arrived as `state:"final"`. Treated as the terminal it
> claimed to be, it: finalised the live run (every later delta dropped as
> "late", so the turn looked dead while the bot streamed on), played the
> end-of-reply chime early, cleared the relay's `pending` flag (killing the
> "replying" shimmer), overwrote the chat-list preview with the literal marker,
> and pushed a notification reading "[context-optimized]".
>
> Guarding it by string worked, but made the contract *"every surface must
> remember to recognise this text"* — one forgotten guard reproduces all five
> symptoms, and a model that genuinely replied with that exact string would
> hang the turn.
>
> **The bot no longer emits it at all.** The divider is written by whichever
> transport persists the conversation, keyed off the typed `compaction` event
> (§5): the relay writes a boundary row carrying `kind:"context_boundary"` and
> `compactionId`, plus the legacy `content` string so existing clients render
> it unchanged. Direct-gateway clients never received the marker anyway — they
> read the same typed event.
>
> The string guards stay in the clients and the relay for bots older than this
> change. They are compatibility, not contract.

**4.3 — Stop is cooperative.** `chat.abort` marks the run aborted. The turn
still delivers `final` with `aborted: true` — that is the authoritative
terminal. Clients keep the partial text and tool cards until it lands.

**4.4 — Re-entry is served by `chat.inflight`.** A client that reconnects
mid-turn calls it and receives the in-flight run plus current `plan`,
`compaction` and `goal` snapshots — enough to rebuild the user bubble, partial
reply, live tool panel and durable auxiliary state. Streaming is otherwise
fire-and-forget: deltas that arrived while disconnected are gone. This RPC is
served identically over both transports; `goal.get` remains the dedicated
canonical lookup when only standing-goal state is needed.

---

## 5. Compaction lifecycle

**5.1 — Every `started` is closed** by exactly one `completed` or `failed`.
An unclosed `started` leaves the UI shimmering forever and (historically) hid
the reply bubble.

**5.2 — One cycle per session at a time.** Three paths can compact: the
pre-turn check, the post-turn background pass, and manual `/compact`. They
serialise on the session lock. **A path that finds the lock already held MUST
NOT announce its own `started`** — whoever holds the lock owns the UI cycle.
Two overlapping announce pairs make the notice flap between phases, which is
what a user sees as a broken shimmer.

**5.3 — Content proves the cycle ended.** Clients clear a stuck `started` when
a delta or final arrives, because a socket that churned during a minutes-long
summarisation may never receive the `completed`.

**5.4 — Compaction never invents or destroys history.** A failed summary
leaves the session untouched (the turn gets an emergency trim of its working
copy only). Messages appended while summarising ride across the commit.

---

## 6. Rules for adding to this protocol

1. **Do not overload `state:"final"`.** It means "this turn is over". Anything
   else needs its own `state` value or its own event name.
2. **Tag conversation events with `sessionKey`.** No exceptions; the relay
   cannot route without it and clients filter on it.
3. **Additive fields only.** The relay serves bots of mixed versions and
   clients update on their own schedule. A new field must be optional, and
   omitting it must produce exactly the old wire shape.
4. **Both ends, one commit.** A producer change without its consumer guard is
   how the marker bug shipped. `tests/test_chat_wire_conformance.py` asserts
   the producer side; the client repos carry the mirror assertions.
5. **Every external call is bounded.** Providers hang; a call inside a session
   lock inside a user's turn blocks the message and ignores Stop. Bound it and
   let the failure path handle it.
