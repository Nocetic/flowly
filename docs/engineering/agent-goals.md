# Tracked goals and the agent's agenda

Status: Core phase 1 implemented on branch `feat/agent-goals` (2026-10-13), not
merged. Desktop UI on the same branch name in `flowly-desktop`. iOS not started.

## What it is for

An agent that only speaks when spoken to forgets everything the owner left
half done. With this, an agent:

- **tracks the owner's goals** over days or weeks ("a week of daily
  workouts", "find a laptop under 40k"): a plan, a stage, progress, a short
  log, and check-ins on a rhythm it chooses;
- **comes back to unfinished threads on its own** (a purchase being
  researched, a decision, something the owner will try tomorrow);
- **plans its day** once each morning from its goals and the owner's recent
  conversations;
- does all of it **inside limits the code enforces**, not the model: how often
  it may write first, quiet hours, one daily budget for all agents, and
  backing off when the owner does not answer.

The model decides *what is worth following up*; the code decides *whether it
may reach the owner now*. This split is deliberate: research on proactive
assistants (and the products that ship them) agrees that interruption has a
cost, silence must be a first-class choice, and an unanswered message must make
the next one less likely, not more.

### Not the standing goal

`flowly/goals` (`/goal`, the `goal` tool) is a different thing: a durable
objective that keeps the agent working *inside one conversation* until a judge
says it is done. A tracked goal is the owner's own aim, followed over time.
To keep the two apart, the code calls tracked goals **objectives**
(`flowly/objectives`, tool `objective`); product surfaces call them *Goals*.

## Architecture

```
owner conversation ──(agent decides)──▶ objective tool ─▶ objectives.json
        │                                follow_up tool ─▶ agenda.json (items)
        │                                                     │
        │ owner writes ─▶ AgentLoop._process_message          │ due
        │                  └─ agenda.owner_wrote(key)          ▼
        │                     (reset unanswered,         AgendaService.tick (30 s)
        │                      cancel moot follow-ups)    ├─ policy.decide()  ── defer / drop
        │                                                 ├─ budget.reserve() ── shared, all agents
        │                                                 └─ publish system inbound
        │                                                     sender_id="agenda"
        ▼                                                     metadata.agenda_item
AgentLoop._process_agenda_wake ─▶ _process_system_message (hidden turn in the conversation)
   ├─ reply "[SILENT]"/empty ─▶ nothing saved, nothing sent ─▶ turn_finished(delivered=False)
   └─ reply text ─▶ saved (trigger hidden), delivered like any proactive reply
                    ─▶ turn_finished(delivered=True) ─▶ budget.confirm, unanswered+1,
                                                         phone notification
daily, after reviewTime:
AgendaService._maybe_review ─▶ process_direct("agenda:review") with goals + recent
                               conversations; plans with objective/follow_up only
```

One `AgendaService` runs in every agent process: the main gateway and each
named agent's runtime (`flowly/cli/gateway_cmd.py`, started after cron, stopped
before the gateway). Stores are per agent home; the daily budget lives in the
canonical home so all agents share it.

## Files

| File | Holds | Owner |
| --- | --- | --- |
| `<home>/objectives/objectives.json` | the goals: what each one *is* | `flowly/objectives/store.py` |
| `<home>/agenda/agenda.json` | pending wake-ups, their history, per-conversation reply state, settings | `flowly/agenda/store.py` |
| `~/.flowly/agenda/budget.json` | today's sent/reserved first messages, all agents | `flowly/agenda/budget.py` |

All three are written under a `filelock` with an atomic replace (`_atomic_write_json`,
owner-only permissions). Each JSON file carries `version` and a monotonic `revision`.

**When** a goal is next checked lives only in the agenda (one source of truth);
`objectives.list` joins it in as `nextCheckAt`.

### Goal record

```json
{
  "id": "obj_3f9c2a1b7d4e",
  "title": "Work out every day this week",
  "category": "health",            // health|relationships|finance|career|interests|productivity|other
  "why": "Feel stronger",
  "plan": [{"text": "30 min run", "done": true}, {"text": "Gym", "done": false}],
  "stage": "Day 3 of 7",
  "cadence": "every evening at 20:00",
  "progress": {"current": 3, "target": 7, "unit": "days"},
  "status": "active",              // active|paused|done|dropped
  "endsAt": 1792900000000,
  "sessionKey": "desktop:8c1…",    // the conversation the goal was set in
  "createdBy": "agent",            // agent|owner
  "createdAt": 0, "updatedAt": 0, "closedAt": null,
  "log": [{"at": 0, "kind": "checkin", "text": "Ran 30 minutes"}]   // last 50
}
```

Limits: 20 open goals per agent, 200 stored (oldest closed pruned), title 120,
why 1000, 20 steps of 300, stage 200, cadence 120, log entry 500. Every text
field passes `flowly.cron.guard.scan_cron_prompt` (injection and invisible
characters) because it is read back into the agent's prompt on a later wake-up.

### Agenda item

```json
{
  "id": "wk_91b0…", "kind": "follow_up",      // follow_up|objective
  "dueAt": 1792000000000,
  "note": "Compare the two laptops they shortlisted",
  "sessionKey": "desktop:8c1…",
  "objectiveId": null,                         // set for kind=objective
  "cancelOnReply": true,                       // follow-ups only
  "createdAt": 0, "attempts": 0,
  "state": "pending",                          // pending|running
  "deferredReason": "quiet_hours"              // last deferral, if any
}
```

Settled items move to `history` (last 100) with `outcome` (`delivered`,
`silent`, `dropped`, `cancelled`, `replaced`, `failed`), `reason` and `preview`.

Rules: a wake-up must target an **owner conversation** (`desktop:`, `web:`,
`ios:`, `android:`, `cli:`, `tui:`, or a messaging channel prefix); internal
sessions (`cron:`, `agenda:`, `heartbeat:`, flowlet sessions) are refused. A
goal has at most one pending check-in (scheduling another replaces it). A
conversation holds at most 6 follow-ups; an agent at most 40 pending items.
Note ≤ 1000 characters, scanned like goal text.

`conversations[<sessionKey>]` = `{unanswered, lastProactiveAt, lastOwnerAt}`;
top-level `lastOwnerAt`, `lastReviewDay`, `lastReviewAt`.

### Settings (per agent, in `agenda.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `level` | `normal` | `off` / `low` / `normal` / `high`: never / 1 / 2 / 4 first messages a day |
| `quietStart`, `quietEnd` | `22:00`, `09:00` | nothing is sent inside; an overnight window is fine |
| `timezone` | `""` | owner's IANA zone; empty = the agent computer's |
| `reviewTime` | `08:30` | the daily review runs at or after this local time |

## The limits (`flowly/agenda/policy.py`)

`decide(facts)` is pure and runs before any model turn, in this order:

1. level `off` → **drop** (`proactivity_off`)
2. a goal check-in whose goal is not `active` → **drop** (`goal_not_active`)
3. 3 unanswered first messages in this conversation → **drop**
   (`owner_not_answering`) — until the owner writes there again
4. inside quiet hours → **defer** to the end of the window
5. unanswered > 0 and less than `12 h × 2^(unanswered-1)` since the last one →
   **defer** (`waiting_for_reply`)
6. this agent's daily cap reached → **defer** to tomorrow's quiet end
7. all agents' daily cap (6) reached → **defer** likewise
8. a turn is running in the conversation → **defer** 3 minutes
9. otherwise **run**

"Day" is the owner's calendar day. The owner writing in a conversation resets
its `unanswered` to 0 and cancels that conversation's pending follow-ups with
`cancelOnReply` (their reply usually settles them); goal check-ins are kept.

### Shared budget (`flowly/agenda/budget.py`)

Before a turn runs the service **reserves** a slot (under the lock, checking
both the agent cap and the global cap, so two agents waking together cannot
both take the last one). A delivered message **confirms** it; a silent or
failed turn **releases** it. An unconfirmed reservation older than 30 minutes
is treated as a turn that never came back and stops counting.

## The wake-up turn

The service publishes an `InboundMessage(channel="system", sender_id="agenda",
chat_id=<sessionKey>, metadata={"agenda_item": <id>})` — the same re-entry path
a finished background task uses. `AgentLoop._process_message` routes it to
`_process_agenda_wake`, which runs `_process_system_message` in the target
conversation:

- the model sees the whole conversation plus the wake-up prompt
  (`flowly/agenda/prompts.py: wake_prompt`): the note, lateness if the computer
  slept, the goal (plan, stage, progress, last 5 log lines, rhythm), and how
  long the owner has been silent there;
- it may use its tools first (look things up, update the goal, plan the next
  check-in);
- a reply of `[SILENT]` (anywhere, case-insensitive), `NO_REPLY` or nothing:
  **nothing is saved and nothing is sent**; tool side effects (a goal log)
  remain;
- otherwise the turn is saved like any system turn (the trigger is stored as
  `[System: agenda] …` with `user_display_hidden`) and delivered: local
  surfaces (`cli`, `tui`, `desktop`, `ios`, `android`) through
  `gateway.push_session_message`, relay and channels through the returned
  `OutboundMessage`;
- `agenda.turn_finished(...)` reports the outcome; an exception reports
  `failed=True` at once so the service retries in 30 minutes (3 attempts in
  all) instead of holding the agenda for the 15-minute turn timeout.

`ios`/`android` were added to the local-push list of `_process_system_message`
in this change; before it, background-task announces into a phone chat served
directly by the gateway had no delivery path.

### Phone notification

`agenda.service.notify_owner` sends one `kind="chat"` notice through
`flowly.push.notifications.deliver` (dedupe key `chat:agenda:<itemId>`, title =
agent name, body = first line, `conversationId` via `phone_conversation`) for
conversations the agent serves itself (`desktop:`, `ios:`, `android:`, `cli:`,
`tui:`). The presence report holds it while the owner is at a Desktop showing
chats. Relay chats (`web:`) are pushed by the relay itself when the message
lands; messaging channels notify natively.

## The daily review

Once per local day, at or after `reviewTime`, when the level is not `off` and
there is something to review (an active goal, or the owner wrote within 7
days). The day is marked first, so a crashing review is not retried every tick.

It runs `agent.process_direct(prompt, session_key="agenda:review")` in a fresh
session, deleted again when the review ends (what it planned lives in the
agenda; no client should ever list it as a conversation — Desktop's named-agent
list also skips the `agenda:` prefix). Channel `agenda` is in `_NON_USER_CHANNELS` (not owner activity, not
an Activity task) and its route disables the `delivery`, `interactive` and
`scheduling` toolsets: the review cannot write to the owner, ask them
anything or create scheduled tasks; it plans with `objective` / `follow_up`
(its other tools, such as search, stay available for looking things up). The
prompt carries the settings, today's
remaining budget, every active goal with its next check-in, what is already
planned, and the owner's last 8 conversations with their reply state. Because
the review has no owner conversation of its own, its tools must name one
(`conversation=<key>`); a goal check-in always lands in the goal's own
conversation.

## The tools

Both live in the new `agenda` toolset (`flowly/agent/tools/routing.py`), which
cron runs cannot use.

- `objective` — `create` (title, category, why, plan, stage, cadence, target,
  unit, ends, next_check, check_note), `update` (any field, `done_steps`,
  `next_check` to plan/move the check-in), `log`, `close` (`done`/`dropped`,
  cancels the check-in), `list`, `get`.
- `follow_up` — `schedule` (when, note, keep_if_owner_replies), `list`,
  `cancel`.

Times (`flowly/agenda/timeparse.py`): `+30m`/`+2h`/`+1d`/`+1w`, `HH:MM` (next),
`today HH:MM`, `tomorrow [HH:MM]` (09:00 default), ISO date (09:00) or
datetime, read in the owner's zone; at least 1 minute and at most 90 days ahead.

**The conversation is never a model argument from a chat.** The tools read it
from `flowly.agent.tool_context.current_tool_origin()`, which the runtime binds
for every call; a chat cannot plan messages into another conversation. Only a
call from outside an owner conversation (the review) may name one, and it must
be an owner conversation.

The system prompt gets a short "Goals and follow-ups" section when the tools
are available (`flowly/agent/context.py`): track goals when a multi-day plan
takes shape and say so, follow up on unfinished threads, never mention the
tools, ids or agenda, and plan nothing when nothing is worth it.

### Hidden from every product surface

`objective` and `follow_up` are bookkeeping; showing their calls would expose
the machinery ("Tracking goal obj_…", "Follow-up wk_… planned") instead of the
result, and the agent already tells the owner in words. They are listed in
`flowly.tool_activity.INTERNAL_TOOLS` and dropped at the one projection
boundary every surface uses:

- `project_tool_messages_for_ui` removes their calls and results (an assistant
  step that only called them and said nothing disappears) — this covers the
  direct gateway `chat.history`, the relay/Firestore copy of a finished turn,
  and live iteration events (`AgentLoop._emit_iteration_event`, which keeps a
  per-run set of hidden call ids so a result arriving later is dropped too);
- `ToolProgress.announce` never announces them (live tool progress);
- `ActivityRecorder.note_tool` never records them as Activity steps.

The canonical session/provider history is untouched: the model still sees its
own calls.

## Owner-facing RPCs (`flowly/agenda/rpc.py`)

Registered in `feature_rpc._DISPATCH` and allowed through the profile host
(`profile_host_contract.PROFILE_RPC_TIMEOUTS`) for named agents.

| Method | Params | Result |
| --- | --- | --- |
| `objectives.list` | `includeClosed?` (default true) | `{objectives: [goal + nextCheckAt], settings}` |
| `objectives.update` | `id`, `action`: `pause` / `resume` / `done` / `drop` / `delete` | `{ok, objective}` or `{ok, deleted}`; anything but resume cancels the check-in |
| `agenda.list` | `historyLimit?` (0–100, default 20) | `{pending, history, settings}` (public fields only) |
| `agenda.settings.get` | — | `{settings, levels, dailyCaps, globalDailyCap}` |
| `agenda.settings.set` | any of `level`, `quietStart`, `quietEnd`, `timezone`, `reviewTime` | `{settings}`; unknown keys rejected |

Errors map `ObjectiveError.code` to the RPC code: `INVALID`, `NOT_FOUND`,
`LIMIT`, `BUSY`, `CORRUPT`. A resumed goal gets no check-in until the agent
plans one (next turn in that conversation or the next daily review).

## Named agents

Each named agent runs the same service in its own runtime, with its own
stores, and writes into its single conversation (`desktop:profile-home`). The
budget is shared through the canonical home. **Gap:** named runtimes hold no
device push registrations (they live on the primary host, see
`profile-cron-push-2026-09-21.md`), so a named agent's first message reaches
Desktop and the conversation but does not ring the phone yet. Fix: forward the
delivered event through the profile host like `profiles.cron.notify`.

## Failure modes

| Situation | What happens |
| --- | --- |
| Computer asleep at the due time | the wake-up runs on the next tick after waking; the prompt says how late it is and the agent may stay silent |
| Process dies mid-turn | at start, `running` items are rescheduled +5 min (attempts +1); 3 attempts then `failed` |
| Turn raises | reported at once; retried in 30 min; 3 attempts then `failed` |
| Turn exceeds 15 min | treated as failed; a late delivery still counts (`turn_finished` without a waiter) |
| Two agents race for the last slot | `reserve` is atomic under the lock; the loser defers to tomorrow |
| Corrupt JSON | the store raises `CORRUPT`; the service logs and skips that tick; the file is never overwritten blindly |
| Local agent and the Mac is off | nothing runs until it is on again: proactivity needs an always-on agent (VPS / cloud) |

## Not done yet

- Desktop (branch `feat/agent-goals` in `flowly-desktop`, see its
  `docs/engineering/agent-goals.md`): the Goals section above routines in the agent panel
  and the "how often" setting are done; a chat card when a goal is created and
  the goal on the sidebar row are not.
- iOS / Android: same surfaces.
- Phone push for named agents (above).
- One combined morning message across agents instead of separate ones.
- Learning from outcomes beyond backoff (which kinds of message the owner
  answers), event-driven wake-ups (new email, calendar, flowlet data), and a
  client event when goals change (clients refresh on open and after actions).

## Tests

`tests/test_agenda_policy.py` (limits, time parsing), `test_agenda_stores.py`
(goal/agenda files, budget), `test_agenda_service.py` (runner with a faked
turn: delivered, silent, failed, quiet hours, busy, caps, backoff, goal
check-ins, review), `test_agenda_tools.py` (tools bound to the calling
conversation), `test_agenda_wake_turn.py` (the loop's hidden turn, silence,
failure, owner signal), `test_internal_tool_visibility.py` (hidden on every
surface; fails without the fix), `test_feature_rpc_agenda.py` (RPCs, profile
host allowlist).
