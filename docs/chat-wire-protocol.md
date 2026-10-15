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
| `turnOrigin` | Present on every event of a run the server started on its own: `"subagent"` for a subagent's report turn (§4.7), `"subagent_task"` for a subagent's own turn in its conversation `subagent:<threadId>` (§4.8). Absent on a client's own turn. |

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
| `run_started` | A run the server started opened (§4.7). Not a terminal, no text. | `turnOrigin`, `hiddenPrompt`, `steerable` |
| `streaming` | One delta of assistant text. | `delta` |
| `iteration_step` | One tool-protocol step inside the turn. | `iterationIdx`, `role`, `content`, `tool_calls?`, `tool_call_id?`, `name?` |
| `tool_progress` | A call's state changed (queued, running, ended). Not a step. | `iterationIdx`, `revision`, `call` |
| `agent_note` | A subagent's turn only (§4.8): the main agent's message (or, `noteKind: "task"`, a task) it read. Not a step. | `iterationIdx`, `author`, `content`, `noteKind?` |
| `stream_reset` | A subagent's turn only (§4.8): a streamed reply was taken back (it failed and is made again, or the run ends). The live text is `text` from here on. Not a step, not a terminal. | `text` |
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

`subagent.stream` (relay only, §4.8) wraps a subagent's own events for the
relay session that watches it.

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

**4.5 — A named agent's first message has no chat.send.** When a new agent's
home conversation (`desktop:profile-home`) is introduced
(`agent.home.introduce`), the direct gateway runs an ordinary turn whose run
id starts with `agent-intro-`. It emits the usual `agent` deltas,
`iteration_step`, `final`/`error`, but no `state:"user"` event: its trigger is
hidden. A client showing the home conversation adopts such a run when it is
not already following one of its own; any other view ignores it. On `error`,
clients reload history instead of showing the error — the server has written
its static welcome. Details: `docs/engineering/agent-home-setup.md`.

**4.6 — `setupAnswer` on chat.send.** In the home conversation only, a tapped
setup choice sends its visible label as `message` plus
`setupAnswer: {askId, optionId}`. The server applies its meaning inside the
turn lock; a stale or unknown answer is treated as typed text.
The agent's proposed working style is appended to the home transcript as an
assistant row with `kind: "agent_setup_card"` and a `setupCard` payload
(forwarded by `chat.history`). The row is append-only; clients derive whether
it is offered, saved (`savedCardId`) or superseded from `agent.home.get`.

**4.7 — A subagent's report turn has no chat.send.** When subagents finish,
the conversation that started them takes a turn of its own to report their
results. If a client watches that conversation (a bound gateway socket, a
relay session), the server runs it through the same runner `chat.send` uses,
under a fresh run id with a command record `turnOrigin: "subagent"`:

```
run_started ─▶ [streaming | agent delta | iteration_step | tool_progress]* ─▶ final | error
```

- Once the turn holds the conversation's turn lock it sends
  `chat {state:"run_started", runId, sessionKey, turnOrigin:"subagent",
  hiddenPrompt:true, steerable:false}`. Every later event of the run carries
  `turnOrigin` and `sessionKey` (direct `agent` deltas included), so a client
  that missed `run_started` still knows whose run it is.
- There is no `state:"user"` event: the trigger is a system notice, saved as
  a hidden row. `chat.inflight` returns the run with `user: ""` and
  `turnOrigin: "subagent"`.
- The terminal is an ordinary `final` (relay: `streamRunId` = the run) or
  `error`, with `turnOrigin`. Stop is `chat.abort(runId)` as for any run.
- The run is not steerable: `chat.steer` answers `RUN_NOT_ACTIVE`. Clients
  queue what the owner types and send it as the next turn.
- A client adopts such a run when it shows that `sessionKey` and is not
  following a run of its own (an owner's send waiting behind it keeps its
  bubble). A run it does not adopt still ends in a `final` that the client
  appends as a message (it owns no live state).
- Nobody watching: the report goes over the agent bus as before. Local direct
  clients then get one proactive `final` (`proactive: true`) whose `runId` is
  the run id the saved reply carries and whose `sessionKey` is the
  conversation's own key; a client showing that conversation appends it.

Older clients ignore `run_started` and `turnOrigin`; on the relay they see
the deltas as before, on the direct gateway they show the reply on the next
history read.

**4.8 — A subagent's own turn streams to whoever watches it.** A subagent
working for the owner is a thread with its own read-only conversation
`K = subagent:<threadId>`. Each task it gets is a turn of `K` with a run id of
its own: the first (`trigger: "spawn"`, `turn: 1`, run id = `threadId`), then
each follow-up the main agent gives it (`trigger: "followup"`, `turn` 2, 3, …).
While a turn of it runs (run id `R`), the server sends that turn's events —
only to clients that watch the thread:

```
run_started ─▶ [agent delta | stream_reset | iteration_step | tool_progress | agent_note | compaction]* ─▶ final | error
```

- Every event carries `sessionKey: K`, `runId: R` and
  `turnOrigin: "subagent_task"`. A client shows them only in `K`'s own view
  and never counts them as the agent's (or its parent conversation's)
  activity, notification or unread state.
- `run_started`: `hiddenPrompt: false`, `steerable: false`, `prompt` (the task
  the main agent gave — the brief), `promptAuthor: "main_agent"`, `threadId`,
  `turn`, `trigger`, `parentSessionKey`, `title`, `mark`, `model`,
  `withConversation` (the first turn only), `startedAt`; `resumed: true` when
  the run continues after a restart (its earlier steps are in `chat.inflight`,
  not sent again). A follow-up turn has `trigger: "followup"`, its `turn`
  number and the follow-up task as `prompt`; a client starts it as a new turn
  of the same view, after the turns `subagents.history` already shows.
  There is no `state:"user"` event.
- `agent {stream:"assistant", data:{text}}`: what the model said, as
  incremental deltas — token by token (at most about every 50 ms) while the
  thread is watched and the provider streams, else one delta per reply. As in
  an owner's turn, a client appends each delta to the live bubble, and an
  `iteration_step` with `tool_calls` (an empty list included) moves the text
  before it out of the live bubble. `chat.inflight` / the watch snapshot's
  `text` is the partial reply since the last such step.
- `chat {state:"stream_reset", text}`: a streamed reply failed (a provider
  error, a timeout, an empty reply) and is not recorded; the bubble's text is
  `text` from here on (what it held before that reply — usually empty). The
  call is then made again (its deltas follow) or the run ends with its
  terminal. A client that ignores this event shows the failed text glued to
  the retry until the next step boundary or the terminal.
- `iteration_step` / `tool_progress`: an owner's turn's shapes. A tool step
  not `ok` adds `status` (`error`, `timeout`, `interrupted`, `not_run`).
- `chat {state:"agent_note", iterationIdx, author:"main_agent", content}`: a
  message from the main agent the subagent read at a step; `noteKind: "task"`
  when it is a task the main agent gave the running turn (its report covers
  it). `content` is as the model got it (`[Message from the main agent]` /
  `[Another task from the main agent]` and the text).
- `compaction {phase, compactionId, tokensBefore, tokensAfter,
  messagesRemoved, sessionKey: K, runId: R}`: a summary fitting the
  subagent's context (§5's lifecycle).
- One terminal, after the turn is saved (`subagents.history` then has it):
  `final` with `outcome` (`ok`; stopped: `cancelled` | `interrupted` with
  `aborted: true` and empty text), `message.usage` (the run's tokens),
  `model`, `durationMs`, `completedAt`; or `error` with `outcome` (`error` |
  `timeout`), `errorCode`, `errorTitle`, `errorMessage`, `retryable: false`.
- **Watching.** `subagents.watch {threadId}` (allowed to whoever may read the
  parent conversation) binds the caller for `leaseSeconds` (120; renew by
  calling again, about every 90 s) and replies with `inflight` (the turn so
  far, as `chat.inflight`: `user: ""`, `prompt`, `promptAuthor`, `subagent`,
  `iterations` holding `iteration_step` and `agent_note` events in order,
  `toolProgress`), `activeRunId`, `status`, `compaction`. The snapshot and the
  binding are taken together: apply the snapshot, then every event received
  after sending the request, in order. `subagents.unwatch {threadId}` or a
  disconnect ends the binding. `chat.inflight {sessionKey: K}` also binds the
  calling direct-gateway socket, as for any conversation.
- **Direct gateway:** the events above as they are. **Relay:** each inside
  `{"type":"event","event":"subagent.stream","sessionId",
  "data":{"sessionKey":K,"event":"chat"|"agent"|"compaction","data":…}}`
  to the watching session only — a `chat` event there would be stored in the
  conversation of the receiving session; the relay stores nothing of this
  name. **Named agents** (`profiles.rpc` watch/unwatch): `profile.event
  {profile, type:"chat"|"agent"|"compaction", data}` on either transport.
- Clients that never watch get none of these. `system.capabilities`:
  `subagentStreamVersion: 1`, `featureMethods` has `subagents.watch`.

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
