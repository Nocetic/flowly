# Activity: a bot's work, one task at a time

Status: Core implemented on `feat/activity-journal` (2026-09-30). The Desktop
Activity tab follows in its own worktree. iOS and Android have not adopted it
yet.

Code:
- `flowly/activity/` (`steps.py`, `store.py`, `recorder.py`, `recap.py`,
  `journal.py`);
- hooks in `flowly/agent/loop.py`;
- the RPCs in `flowly/channels/feature_rpc.py`;
- the allowlist in `flowly/profile_host_contract.py`;
- `ActivityConfig` in `flowly/config/schema.py`.

Tests: `tests/test_activity_journal.py`.

Decisions taken with the owner:
- the bot's own model writes the summaries;
- only tasks that used a tool or worked for 30 s or more get one;
- summaries are on by default and can be turned off per bot; the task record is
  kept either way.

---

## 1. What the owner sees

The Activity tab lists **tasks**, not conversations and not raw log lines.
The model is Muse's activity view.

```
Today
 (icon) Research Hermes AI agent                          •
        Confirmed Hermes is an AI agent, not the brand
        03:53
```

Tapping a task opens it:
- a status;
- the title, the outcome and the time;
- a short summary in the bot's own voice;
- the steps, each with a title and a one-line note;
- the approvals and questions raised;
- an "Open conversation" button.

A dot marks tasks the owner has not seen that are blocked, failed,
interrupted or waiting on them.

## 2. What a task is, and when it starts and ends

A task is one agent turn. Every turn passes through
`AgentLoop._process_message`: channel and relay turns, routines, goals and
direct calls alike.

- **Start:** once the conversation's turn lock is taken. Time spent queued
  behind an earlier turn of the same conversation is not the task's.
- **End:** in that block's `finally`, whatever the outcome.
- **Clock:** the process's monotonic clock. Nothing depends on streams,
  clients, the relay or the network.

**Active time** excludes the time the turn spent parked on an approval or a
question. The approval and clarify managers report when a prompt opens and
closes, and the recorder subtracts the span. A turn that waited ten minutes
for a yes and then said one sentence did not work for ten minutes.

**Crash safety.** A `start` line is written when the task begins, and every
line carries the process's boot id. If the process dies (kill, power loss, an
update), the `finally` never runs and no `task` line follows. The reader then
shows that start, from an earlier boot, as `interrupted`. The task is never
lost.

These are **not** tasks:
- host-only orchestration sessions (`desktop:profile-inbox:`,
  `desktop:profile-room:`, `desktop:profile-task:`). The Board and groups own
  that history;
- heartbeats (`heartbeat:`), helper-agent sessions (`subagent:`) and system
  turns;
- helper announcements, background process notices, the agent's own
  introduction;
- a turn that returned no reply **and** took no step: the agent chose to stay
  silent, as with a passive group message. It is closed as `discarded` so it
  never reads as interrupted.

Who started a task (`trigger.kind`):
- `owner`: the app, web, CLI, voice;
- `routine`, with the cron job id and its name. The cron runner passes the
  name, and until a summary lands the task is titled with it;
- `goal`, with the goal id;
- `channel`, with Telegram, Slack and the like.

A helper agent appears as one step (`kind: agent`) of the task that spawned
it. Its own steps are not listed yet.

## 3. The record (written by Core, no model involved)

Append-only JSON lines in `<FLOWLY_HOME>/activity/<YYYY-MM>.jsonl`. The file
is chosen by the task's start time, so all of a task's lines share one file.
Every profile has its own home, so every bot has its own journal. There are
four line types:

- `start`: id, sessionKey, trigger, request, startedAt, boot;
- `task`: the whole record, written at the end (below);
- `recap`: the model's summary and its token usage, written later;
- `seen`: the owner has seen everything up to `before` (ms).

| task field | meaning |
|---|---|
| `id` | the run id (a fresh id when the transport gave none) |
| `sessionKey`, `conversationTitle` | where it happened |
| `trigger` | see above |
| `request` | the first 280 characters of what started it, on one line |
| `startedAt`, `endedAt`, `activeMs` | wall-clock stamps and active time |
| `status` | `done` · `stopped` · `failed` · `blocked` |
| `steps[]` | per tool call: `tool`, `kind`, `target`, `ok`, `durationMs`, `blocked?` |
| `prompts[]` | approvals and questions: `kind`, `subject`, `decision` (`allowed`/`denied`/`answered`/`timeout`/`cancelled`/`open`) |
| `model`, `tokens` | the turn's model and provider token counts (`input`, `output`) |
| `error` | short, owner-readable, when it failed |

- **Status:**
  - `stopped`: the turn was aborted or cancelled;
  - `failed`: it errored;
  - `blocked`: its last step failed right after an approval or question was
    refused or timed out;
  - otherwise `done`.
- **Worked out at read time** (§5): `running`, `waiting` and `interrupted`.
- **Cost:** Core has no price list, so it records tokens. Clients price them
  with their model catalogue.
- **Step kinds:** `search`, `web`, `read`, `write`, `exec`, `mcp`, `bot`,
  `agent`, `media`, `memory`, `other`.
- **Step targets:** the query, the site's host, the file's name, the
  program's name (never its arguments), the MCP tool, the other bot. At most
  80 characters. A value that looks like a credential is dropped. Clients
  turn `kind` + `target` into a title in the owner's language, so titles are
  exact and translatable.
- **Prompt subjects:** a tool action's sentence ("Send email to …"). A shell
  command is reduced to its program, like a step's target.

Where the data comes from:
- **Steps:** every tool call passes through `_audit.log_tool_call(...)`,
  and the recorder sits beside it.
- **Prompts:** the approval and clarify managers' notify and close
  callbacks.
- **Tokens:** the same outcome metadata `_note_turn_usage` reads.

## 4. The summary (written by the bot's model, after the task)

After a task ends that used a tool or worked 30 s or more, a background job
asks the turn's own provider and model (`purpose="activity_recap"`) for one
JSON object, in the request's language:

```json
{ "title": "≤ 6 words, imperative",
  "outcome": "≤ 12 words, past tense",
  "summary": "1–3 sentences, first person",
  "steps": [{ "i": 0, "note": "one line on what this step found or did" }] }
```

The input is compact, never the whole conversation:
- the request, the status and the trigger;
- each step's tool, kind, target, success and the first 1,500 characters of
  its result (at most 20 steps; the rest are counted);
- the first 2,000 characters of the reply.

Step results live only in memory for this request; they are never written to
disk.

- **Timing:** it never delays the reply.
- **Failure:** a timeout (30 s), a provider error or unusable JSON leaves the
  task with its fallback title (the request's first line) and no summary.
- **Output check:** the answer is validated:
  - title and outcome are required;
  - lengths are capped;
  - step notes must point at a listed step.
- **Cleaning:** reasoning blocks, code fences, markdown, quotes and citation
  debris (`【…†L1-L3】`, `[^1]`) are removed.
- **Accounting:** the summary's own token usage is recorded as
  `recapTokens`.

Setting: `activity.summaries` (default `true`). It is read when each summary
is about to run, so turning it off needs no restart.

## 5. Reading it

Feature RPCs. They are also in the profile-host allowlist, so named, remote,
iOS and Android bots can all be read:

- `activity.list {limit? 1–100 (30), before? ms}` → newest first:
  `{items, nextBefore, seenBefore}`. Each item is
  `{id, title, outcome, status, trigger, kind, startedAt, endedAt, sessionKey,
  conversationTitle, summarized, unseen}`. `kind` picks the icon: `routine`,
  `research`, `files`, `code`, `connection`, `team`, `media` or `general`.
- `activity.get {id}` → `{task}`: the item plus `summary`, `request`,
  `activeMs`, `steps` (with `note`), `prompts`, `model`, `tokens`,
  `recapTokens`, `error?`.
- `activity.seen {before}` → `{seenBefore}`. It moves the cursor forward,
  never back, for this bot and across devices.

Statuses worked out at read time:
- `running`: this process is still working on the task;
- `waiting`: it is running, and its conversation waits on the owner (the same
  source as `sessions.attention`);
- `interrupted`: it started in another boot and never ended.

**Ownership.** An account-scoped request only sees tasks of conversations
`sessions.list` would show it. This is the same rule `sessions.attention`
applies, now shared.

**No live event yet.** The gateway's broadcast does not scope by owner, so an
`activity.updated` event could leak another account's conversation key.
Clients re-read the list when the tab opens and when a turn ends. A scoped
event can come later.

## 6. Keeping it small

- Retention is 90 days (`activity.retention_days`; `-1` keeps everything).
  Whole month files older than the window are deleted, once per process, on
  the first `activity.list`.
- Reads reuse the merged view until a month file changes.
- The audit log is unchanged. It stays the forensic record; Activity is the
  owner-facing one.

## 7. Next

- The Desktop Activity tab (own worktree), then iOS and Android.
- A daily line on top ("Today: 6 tasks · 2 routines") is a client-side sum of
  `activity.list`.
- Helper agents' own steps as their own group.
- A scoped live event.
