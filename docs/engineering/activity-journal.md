# Activity: the work an agent does, one task at a time

Status: shipped to local `main` on all four surfaces (Core, Desktop, iOS,
Android), 2026-10-02. Not yet released.

Public, owner-facing doc: `content/docs/features/activity.md`.
Client docs: Desktop `docs/engineering/activity.md`, iOS
`docs/engineering/activity-ios.md`, Android `docs/engineering/android-activity.md`.

This document is the contract. Clients follow it; when it changes, change it
here first.

---

## 0. In one paragraph

The agent loop tells a **recorder** about every turn and every tool call. A turn
reaches the disk only once it does **work** (a routine or goal, or a working
step); a conversation writes nothing. Each recorded turn names the **task** it
belongs to. After a turn ends, the agent's own model writes the task's title,
outcome, summary and step notes and, in the same answer, judges whether the turn
was work at all and whether it **continues** recent work in the same
conversation. A **reader** groups turns into tasks at read time. Four feature
RPCs (`activity.list/get/step/seen`) serve the apps. A step opened in full is
read back from the conversation's own transcript, so tool arguments and results
are never copied into the journal.

## 1. File map

| File | Role |
| --- | --- |
| `flowly/activity/steps.py` | One tool call → `{tool, kind, target}`; what counts as work (`is_work`); task kinds (`TASK_KINDS`, `task_kind_of`) |
| `flowly/activity/recorder.py` | The in-process recorder: turn lifecycle, steps, prompts, live state, step details in memory |
| `flowly/activity/store.py` | The append-only month files: append, merge, cache, prune |
| `flowly/activity/journal.py` | Reading: which turns show, grouping turns into tasks, statuses, titles, kinds, the RPC payloads, continuation lookup |
| `flowly/activity/recap.py` | The model request: prompt, validation, judgements |
| `flowly/activity/transcript.py` | Reading one tool call back from a conversation file |
| `flowly/agent/loop.py` | Hooks: `_activity_trigger`, `_activity_begin`, `_activity_finish`, `_activity_schedule_recap`, `_activity_recap`; `note_tool` beside `_audit.log_tool_call` |
| `flowly/channels/feature_rpc.py` | `activity_list`, `activity_get`, `activity_step`, `activity_seen`, `_activity_now` |
| `flowly/profile_host_contract.py` | The four RPCs in the profile-host allowlist (named, remote, mobile agents) |
| `flowly/config/schema.py` | `ActivityConfig` (`summaries`, `retention_days`) |
| `tests/test_activity_journal.py` | ~110 tests: rules, grouping, legacy lines, recap, RPCs, the real loop |

## 2. What becomes a task

### 2.1 Turns the recorder never sees

`ActivityRecorder.records(session_key)` is false for host orchestration and
housekeeping sessions — `desktop:profile-inbox:`, `desktop:profile-room:`,
`desktop:profile-task:`, `heartbeat:`, `subagent:` (the Board, groups and
helpers keep their own history). `_activity_trigger` returns None (no task) for
the `system` channel, the agent's own introduction, and messages from
`subagent`/`process`/`system`/`goal` senders that are not a goal turn.

### 2.2 Who started it (`trigger.kind`)

| kind | when | extra |
| --- | --- | --- |
| `owner` | channels `web desktop ios android cli api voice` | — |
| `routine` | channel `cron` or session `cron:*` | `jobId`, `name` (from `msg.metadata["_activity_routine"]`, ≤ 80 chars) |
| `goal` | a synthetic goal turn | `goalId` |
| `channel` | Telegram, Slack, … | `channel` |

### 2.3 Work, by rule (no model)

A turn is **work** when:

- `trigger.kind ∈ {routine, goal}` (`BACKGROUND_TRIGGERS`) — work from `begin`; or
- it takes a step for which `steps.is_work(step)` is true.

`is_work` is an exclusion list, so a tool added later counts as work without a
change:

- every `memory_*` tool (by **name** — `memory_search` is described as a
  `search` step but is still recall);
- `CONVERSATION_TOOLS`: `knowledge_graph`, `session_search`, `sessions_list`,
  `clarify`, `agent_setup_ask`, `plan`, `skills_list`, `skill_view`.

Channel turns follow the same rule as owner turns. They are deliberately **not**
always candidates: for many owners Telegram is simply where they talk to their
agent, and judging every greeting would cost a model request per message.

### 2.4 Candidates

A turn that did no work but replied with ≥ `LONG_REPLY_CHARS` (1,500)
characters is a **candidate**: its record is written at the end with
`work: false`, and it shows only if the model judges it work (`verdict: true`).
Length, not time — time says more about the model's speed than about the
output.

### 2.5 What is written when

| moment | written |
| --- | --- |
| `begin`, background trigger | `start` line (`work: true`) |
| `begin`, owner/channel | nothing |
| first working step (`note_tool`) | `start` line (once, `_mark_working`) |
| `end`, work or candidate | `task` line |
| `end`, neither | nothing — the turn leaves no trace |
| `end`, `silent` and no work (passive group message, dropped dispatch) | nothing |
| after `end`, summaries on | `recap` line (from a background job) |
| owner looked | `seen` line |

A crash after the `start` line leaves a start with no `task`: the reader shows
it as `interrupted`. A crash before it loses nothing that was worth showing.

### 2.6 Model judgements (from the recap answer)

- `work: false` hides a turn that is work by rule — never a routine's or a
  goal's. `work: true` admits a candidate.
- `continues: true` joins the turn to the earlier task (§4).
- Missing or non-boolean judgements count as absent. Judgements from an answer
  whose words failed validation are discarded too.

### 2.7 Visibility (`journal._shown`)

```
discarded or no startedAt         → hidden
trigger ∈ {routine, goal}         → shown
verdict == False                  → hidden
work by rule (or legacy rule)     → shown
otherwise                         → shown only if verdict == True
```

Legacy lines (written before 2026-10-01) carry no `work`. `_did_work` then
recomputes it from their steps with `is_work`. A legacy unended start with no
steps is hidden (what it was doing is unknown). Nothing on disk is rewritten.

## 3. The journal on disk

`<FLOWLY_HOME>/activity/<YYYY-MM>.jsonl` — one file per month, chosen by the
turn's **start** time, so all lines of a turn share a file. Each profile has its
own home, so each agent has its own journal. Lines are appended with
`O_APPEND` under a process lock; nothing is rewritten in place. Readers merge
lines by turn id, so a torn last line from a crash never corrupts earlier
turns.

### 3.1 Line types (keyed by the turn's id = run id)

| type | fields |
| --- | --- |
| `start` | `id`, `taskId`, `sessionKey`, `trigger`, `request` (≤ 280, one line), `startedAt`, `work: true`, `boot` |
| `task` | the whole record (below) |
| `recap` | `recap {title, outcome, summary, steps[{i, note}]}`, `recapTokens {input, output}`, `work` (bool, the verdict), `taskId` (only when the turn continues earlier work) |
| `seen` | `before` (ms) — no id |

### 3.2 The `task` record

| field | meaning |
| --- | --- |
| `id` | run id (a fresh uuid when the transport gave none) |
| `taskId` | the task it starts in: its own id, or `goal:<goalId>` (`task_id_for`) |
| `sessionKey` | where it happened |
| `conversationTitle` | the session's title, unless still provisional (the owner's first message standing in) |
| `trigger` | §2.2 |
| `request` | first 280 chars, whitespace folded |
| `startedAt`, `endedAt` | wall clock, ms |
| `activeMs` | monotonic time minus time parked on approvals/questions |
| `status` | `done` · `stopped` (aborted) · `failed` (error) · `blocked` (last step failed right after a refusal/timeout) |
| `work` | work by rule; `false` for a candidate |
| `steps[]` | `tool`, `kind`, `target`, `ok`, `durationMs`, `blocked?`, `callId?` |
| `prompts[]` | `kind` (approval/question), `subject` (≤ 80), `decision` (allowed/denied/answered/timeout/cancelled/open) |
| `model`, `tokens` | the turn's model and `{input, output}` |
| `error` | short, only when failed |

Step `kind`: `search web read write exec mcp bot agent media memory other`.
Step `target`: the query, a site's host, a file's name, a program's name (never
its arguments), the MCP tool, the other agent — ≤ 80 chars; anything that looks
like a credential is dropped (`steps._SECRET`). `callId` (≤ 128) names the tool
call in the transcript. **Arguments and results are never written.** At most 200
steps per turn.

### 3.3 Statuses worked out at read time

- `running` — the turn is in `recorder.active_ids()` (work only);
- `waiting` — running, and its session has a pending approval or question
  (`_pending_inputs`, the same source as `sessions.attention`);
- `interrupted` — a start with no end, not running in this process.

A task's status is its newest running turn's, else its newest turn's.
`NOTEWORTHY = {blocked, failed, waiting, interrupted}` carry `unseen` when their
stamp (last turn's `endedAt` or `startedAt`) is after the `seen` cursor.

## 4. Tasks across turns

Turns name a task; the reader groups by it (`journal._tasks`, cached by the
identity of the store's merged view).

- **Goals:** `taskId = "goal:<goalId>"` from `begin` — every goal turn is one
  task.
- **Routines:** each run is its own task. Never joined, never joins.
- **Owner/channel turns:** start as their own task. After the turn ends, the
  recap job looks for `journal.earlier_work(sessionKey, turn_id, started_at)`:
  the latest shown task of the same session, not a routine's or goal's, whose
  last turn ended ≤ `CONTINUATION_WINDOW_MS` (30 min) before this turn started
  (ties broken by start time). The model sees that task's title, outcome and
  summary (or its request when it has none) and answers `continues`. On `true`,
  the recap line carries `taskId = <earlier task>` and the turn joins it.
- **One conversation's recaps run in order** (`_activity_recap_last[session]`):
  a follow-up is always judged against the summary of the work before it.
  Different conversations never wait on each other.

A task:

- `id` = its first turn's id (or `goal:…`); a turn's own id still resolves to
  its task in `activity.get` and `activity.step`;
- `startedAt` = first turn's; the list is sorted and paged by it, so a task keeps
  its place;
- title, outcome and summary = the **newest** recap (written with the task so far
  in view);
- steps = all turns' steps, each with its own turn's note; tokens and
  `recapTokens` summed; `activeMs` summed; `request` = first turn's; `error` =
  last turn's when it failed; `model` = latest.

While a follow-up runs it is its own row; it folds into the earlier task once its
recap lands (seconds after it ends). Goals never do this — they are grouped from
the start.

## 5. The recap (title, summary, judgements)

`loop._activity_schedule_recap` → `_activity_recap` → `recap.recap_turn`.

- **When:** after every recorded turn, off the reply's path, in a background
  task. Skipped when `activity.summaries` is false (read each time).
- **Model:** the turn's own provider and model, `purpose="activity_recap"`,
  `max_tokens` 2048, temperature 0.2, timeout 30 s.
- **Input** (compact, never the conversation): the earlier task or "task so far"
  for a goal (§4), the request, status, trigger, up to 20 steps with tool, kind,
  target, outcome and the first 1,500 chars of each result, the first 2,000
  chars of the reply. Step results are held in memory only for this request.
- **Output** (one JSON object, in the request's language):
  `work`, `continues`, `title` (≤ 6 words, imperative, the whole task's when it
  continues), `outcome` (≤ 12 words), `summary` (1–3 sentences, first person),
  `steps[{i, note}]` (one or two sentences per step with the specifics that
  matter).
- **Validation:** reasoning blocks, fences, markdown, citation debris and wrapping
  quotes removed; title and outcome required or the whole answer is dropped
  (judgements included); caps title 80, outcome 140, summary 600, note 320; a
  note must point at a listed step.
- **Failure:** timeout, provider error or unusable JSON → no words, no
  judgements. A work turn stays its own task; a candidate stays hidden.
- **Titles never fall back to the owner's message.** Without a recap the list
  item's `title` is the routine's name or `""`; clients then show the request as
  a quoted placeholder (§7.2).

## 6. Live state and step details

The recorder keeps, per running work turn, in memory only:

- its steps so far (`live_steps()` → `{turn id: [steps]}`, copies) — the journal
  reads a running turn's steps from here (`_turn_steps`), which feeds the
  detail's steps, `latestStep` and the kind while it runs;
- each step's call: `{"args": JSON text ≤ 16,000, "result": text ≤ 64,000}`
  (`step_detail(turn, index)`), kept for the last 32 ended turns too
  (`RECENT_DETAIL_TURNS`).

`journal.get_step(task, index)` counts steps across the task's turns, then reads
the call from memory (`detail: "live"`), else from the conversation transcript
via `transcript.find_call(sessionKey, callId)` (`"transcript"`), else reports
`"none"` (no call id, conversation deleted or compacted). `find_call` takes the
session file lock and runs `require_session_file` exactly like `sessions.read`,
then scans for the assistant `tool_calls[].id` and the `tool` message with that
`tool_call_id`.

## 7. The wire

All four are feature RPCs (`feature_rpc.dispatch`), so they work over the local
gateway, the relay and the profile host. Account-scoped callers only see tasks of
conversations `sessions.list` would show them (`_session_visibility`).

### 7.1 `activity.list {limit? 1–100 (30), before? ms}`

→ `{items, nextBefore, seenBefore}`, newest first by task `startedAt`. Prunes
old months once per process. Each item:

```json
{
  "id": "run-1", "title": "Compare flights to Rome", "outcome": "Found three fares",
  "status": "done", "trigger": {"kind": "owner"}, "kind": "research",
  "startedAt": 1790000000000, "endedAt": 1790000060000,
  "sessionKey": "desktop:abc", "conversationTitle": "Rome trip",
  "summarized": true, "unseen": false,
  "request": "find me cheap flights to rome",
  "latestStep": {"tool": "web_fetch", "kind": "web", "target": "fares.example.com"}
}
```

- `kind` — the icon kind: `routine`, `goal`, else the first of `TASK_KINDS`
  (`message calendar image video voice writing code connection team research
  browse files`) any working step belongs to (`task_kind_of`), else `general`.
  The most consequential wins (what it sent outranks what it read).
- `request` — the first turn's request, ≤ 140 chars (`REQUEST_PREVIEW_CHARS`).
- `latestStep` — what it is doing now while it runs, else its last step; `null`
  before its first step.

### 7.2 `activity.get {id}`

→ `{task}`: the item plus `summary`, full `request` (≤ 280), `activeMs`,
`steps[{tool, kind, target, ok, durationMs, blocked, note?}]`, `prompts`,
`model`, `tokens`, `recapTokens`, `error?`. A turn id resolves to its task.
`NOT_FOUND` when unknown or not visible.

### 7.3 `activity.step {id, index}`

→ `{step: {tool, kind, target, ok, durationMs, blocked, note, detail, args?, result?}}`.
`index` 0–10,000 across the task's steps as `activity.get` lists them. `args` is
the call's JSON arguments text, `result` the tool's text — exactly what the
chat's tool panel renders. `detail` ∈ `live | transcript | none`; with `none`
there is no `args`/`result`.

### 7.4 `activity.seen {before}`

→ `{seenBefore}`. Moves the per-agent cursor forward, never back, across devices.

### 7.5 Client contract (all three apps)

- **Icons:** fold `kind` into three drawings — `research` + `browse` → globe in a
  magnifier; `routine` → clock with a turning arrow; everything else (unknown
  kinds and `media` from older agents included) → four nodes on an open chain.
  24 grid, 1.75 stroke, round caps/joins. Draw the shapes opaque into a
  mask/layer and colour once (§9).
- **Running chain:** node *i* fills over a 2.4 s cycle starting 0.6·i s late:
  ease in to 20 % of the cycle, ease out by 45 %, then rest. Reduced motion:
  the first node stays filled.
- **Headline:** `title`, else the quoted `request` (italic, muted — a
  placeholder), else `conversationTitle`, else the app's "Untitled task".
- **Line under it:** `outcome`, else while `running` the live step in present
  tense ("Searching for …"; "Working on it" before the first step), else the
  last step in past tense.
- **Refresh:** the list every 2 s while any item is running or waiting, else
  every 10 s; an open running/waiting task every 2 s until it ends. A failed
  read keeps what is on screen.
- **Step detail:** fetched once when a step opens; rendered with the chat's own
  tool renderer; `detail: none` → "no longer kept".
- **Unknown values** (status, kind, trigger) fall back instead of dropping the
  task.

## 8. Settings and retention

```json
{ "activity": { "summaries": true, "retentionDays": 90 } }
```

`summaries` (default true): recap requests. `retentionDays` (default 90; `-1`
keeps forever): month files older than the cutoff's month are deleted, the
cutoff's month kept whole.

## 9. Pitfalls we hit

- **Tests wrote to the real home.** Tests without `FLOWLY_HOME` fell back to
  `~/.flowly` and wrote fake tasks into the owner's journal. `tests/conftest.py`
  now points `HOME` and `FLOWLY_HOME` at a temp dir for the session;
  `tests/test_test_isolation.py` guards it.
- **A running gateway mixes old and new modules.** `journal.py` and
  `transcript.py` are imported lazily (first `activity.list`); with an editable
  install, a gateway started before a commit loads the new `journal.py` against
  the old `steps.py` → `ImportError`. Restart the gateway after changing Core.
- **Translucent icons.** Stroking overlapping shapes in a translucent colour
  doubles the alpha at every overlap (bright knots where the chain's links meet
  its rings). Draw opaque into a mask/layer, colour once.
- **Unpositioned shimmer.** The running shimmer is an absolutely positioned
  `::after`; on an unpositioned row it spread over the whole dialog. The class
  now contains itself (Desktop `index.css`).
- **No outside product names** in prompts, fixtures or docs (project rule).

## 10. Not yet

- A scoped live event instead of polling (the gateway broadcast is not
  owner-scoped yet, so an `activity.updated` event could leak another account's
  session key).
- A helper agent's own steps under the step that spawned it.
- "Allow for this task" approvals: the task boundary now exists to scope them to.
