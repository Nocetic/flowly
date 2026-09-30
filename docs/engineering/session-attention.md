# A conversation that needs its owner says so

Status: Core implemented on `worktree-agent-activity` (2026-09-30). Desktop
adoption follows in its own worktree. iOS, Android and web have not adopted
it yet.

Code: `flowly/session/attention.py` (the one reader),
`flowly/channels/feature_rpc.py` (`sessions.list` rows, `sessions.attention`),
`flowly/profile_host.py` (per-bot cache, `profiles.status`),
`flowly/cli/gateway_cmd.py` (capability flag).

Tests: `tests/test_session_attention.py`.

---

## 1. What the owner sees

Every chat row already tells two of three states: a shimmer while a turn is
running, a dot when it finished and has not been read. The third was
missing: **the turn stopped and is waiting for you**. It now has its own
label next to the row, and a named bot in the sidebar carries the same label
when any of its conversations waits.

## 2. What counts as "waiting"

A turn can stop for the owner in four ways. Each is held by its own
registry, keyed by conversation:

| kind         | registry                                        | ends when            |
|--------------|-------------------------------------------------|----------------------|
| `approval`   | exec approvals (`flowly/exec/approval_manager`) | decided or timed out |
| `question`   | clarify (`flowly/clarify/manager`)              | answered or timed out|
| `plan`       | plan store, the conversation's current plan in `awaiting_approval` | decided |
| `connection` | MCP chat setup requests                         | its turn ends        |

The order of the table is the order of urgency: a blocked command or an
unanswered question stops the work outright; a plan or a connection is a
decision the owner can take later.

## 3. One reader, nothing stored

`pending_inputs()` reads the four registries when called and returns

```json
{ "desktop:abc": { "kind": "connection", "since": 1727690000000, "count": 1, "subject": "higgsfield" } }
```

`kind` is the most urgent kind in that conversation, `since` (ms) when the
oldest wait began, `count` how many waits. Nothing is persisted: a wait that
ends is gone from the next read, so there is no flag to clear and nothing to
drift. A registry that fails is logged and skipped; it never hides the
others.

`subject` (optional) says what the shown wait is about, when that is safe in
a list: the service a connection is for (`higgsfield`), or the sentence a
tool action already uses for people ("Send email to …"). It comes from the
oldest wait of the shown kind, is one line and at most 80 characters. A shell
or Codex command is never a subject: it belongs on the approval card, not in
a sidebar. Clients show `subject` beside the state ("Wants to connect ·
Higgsfield") and fall back to the state alone.

## 4. Wire

- **`sessions.list`**: a waiting row carries `needsInput: {kind, since,
  count}`. Rows that wait on nothing carry no field. Through
  `profiles.rpc` the row passes unchanged, so a named bot's own conversation
  list shows the same labels.
- **`sessions.attention`** (feature RPC, no params): `{sessions: {key:
  needsInput}}`. The cheap form for callers that only need this. An
  account-scoped request sees only the keys `sessions.list` would show it.
- **`profiles.status`** (and each entry of `profiles.statuses`): a connected
  bot carries `needsInput: {sessionKey, kind, since, count, subject?}`, its most
  urgent wait (kind first, then the longest), `count` summed across its
  conversations. A stopped bot has no turn, so nothing waits.
- **Profile event `needsInput`** (a `profile.event` envelope, routed like
  `connection` to the directory and the bot's readers): `{needsInput: … |
  null}` whenever a bot's most urgent wait changes. Clients update the bot
  badge from it without polling.

Existing events keep their meaning. `exec.approval.*`, `agent.clarify.*`
and `plan.*` still drive the cards themselves; a client that wants the
per-row label only needs to re-read `sessions.list` (or
`sessions.attention`) when one arrives, or when a turn ends.

## 5. How the profile host stays right

The host never guesses from events. An event only says that something *may*
have changed; the host then asks the runtime (`sessions.attention`) and
keeps what it answers:

- Triggers: `exec.approval.requested/closed`,
  `agent.clarify.requested/closed`, `plan.approval.requested`,
  `plan.updated`, and a chat terminal (`final`, `aborted`, `error`), which is
  also what ends a connection request, since those have no event of their own.
  Also once when a runtime connects, for waits that began before.
- Debounced (0.3 s) and single-flight per runtime: a burst of events is one
  read; an event during a read schedules exactly one more.
- Only a runtime that advertises `session-attention-v1`, is open and is still
  the current runtime for that bot is asked. The read never starts a runtime
  and does not count as use, so it cannot keep an idle bot from being
  released.
- The answer is filtered to the conversations `sessions.list` shows through
  the host (`desktop:`, `web:`, `ios:`, `android:`, minus host-only inbox,
  room and task turns, which the host answers itself). A client is never told
  a bot needs the owner in a conversation it cannot open.
- Closing a runtime cancels its read and clears its answer.

Desktop runs its local named bots with its own supervisor, not this host.
It uses the same runtime contract: `session-attention-v1` in the ready
payload and `sessions.attention` on the runtime link.

## 6. Compatibility

Older clients ignore the new field and event. Older runtimes do not
advertise the capability, are never asked, and their bots simply show no
label. The default (primary) profile is not a hosted runtime; clients read
its rows' `needsInput` from their own `sessions.list`.
