---
title: Plan mode
eyebrow: Features
description: A standing mode where the agent proposes a plan and waits for your approval before it changes anything — enforced in the backend, ticked off step by step, and in sync on every device.
---

**Plan mode** inverts the default. Normally you ask for something and the agent
starts working. In plan mode it stops first: it breaks the task into concrete
steps, shows you the plan, and touches nothing until you approve it.

It is a **mode**, not a tool you invoke. It lives in the same `Shift+Tab` cycle
as the exec permission levels, and once it's on it stays on for every message in
that conversation until you turn it off.

Reach for it when the work is long, expensive, or hard to undo — a refactor
across twenty files, a mailbox cleanup, a migration. You see the shape of the
work while changing it is still cheap.

## Turning it on

Press **`Shift+Tab`** until the composer shows **▣ Plan**. The cycle is:

```text
🔒 Ask  →  ⚖️ Auto  →  🚀 YOLO  →  ▣ Plan
```

Plan mode is **orthogonal to the exec policy**. Unlike the other three levels it
sets no security/ask policy of its own — whatever was active stays active, and
plan mode adds the approval gate on top. Cycling past it turns the mode off and
applies the next level's policy as usual.

Or use the command, which works on every surface that sends text — the TUI,
Desktop, iOS, and any chat channel:

| Command | What it does |
| --- | --- |
| `/plan` | Toggle the standing mode on or off. |
| `/plan on` | Turn the standing mode on. |
| `/plan off` | Turn it off **and abort the active plan**. (`/plan stop`, `/plan cancel`) |
| `/plan status` | Whether the mode is on, plus the active plan's progress. (`/plan ?`) |
| `/plan <task>` | Plan **this one task**, without turning the standing mode on. |

`/plan <task>` is the one-shot: it forces plan-first behavior for that message
only, then the conversation goes back to normal. `/plan` with no argument is the
standing mode — every message plans first until you leave.

## Approving a plan

When the agent proposes, the plan appears on your surface and the turn waits.
You have three answers:

| Decision | What happens |
| --- | --- |
| **Approve** | The plan starts executing. |
| **Reject** | Nothing runs. The plan is closed. |
| **Revise** | Your feedback goes back to the agent, which proposes again. |

"Reject" and "revise" are deliberately separate — "no" and "not like that" are
different answers, and blurring them costs you a turn.

> [!IMPORTANT]
> A proposal that goes unanswered for **10 minutes** times out, and a timeout
> means **not approved** — nothing executes. Plan mode never falls back to its
> own judgement when you don't answer.

Plan mode needs a person at a surface, so a proposal raised inside a
[cron](/docs/features/cron) run is rejected immediately rather than hanging the
schedule until it times out.

## What the gate actually blocks

While plan mode is armed and nothing is approved yet, the agent is blocked from
every tool with a real side effect:

- **Running things** — `exec`, `process`, `shell`, `docker`
- **Writing files** — `write_file`, `edit_file`, `memory_append`
- **Reaching people** — `email`, `message`, `voice_call`
- **External services** — Google Workspace, Linear, GitHub, Sentry, Trello, Home Assistant
- **Durable state** — `board`, `cron`, `flowlet`, `artifact`, `image_generate`, `knowledge_graph`
- **Spawning work** — `spawn`, `delegate`, `builtin_agent`

Read-only tools stay open on purpose: `read_file`, `list_dir`, search, memory
recall, `web_fetch`, screenshots, and the `plan` tool itself. The agent can
investigate as much as it likes to write a *good* plan — it just can't change
anything while doing it.

> [!NOTE]
> This is enforced in the backend, before the tool call runs — not by asking the
> model to behave. A model that ignores its instructions and calls `exec` anyway
> is refused by the gate.

## Watching it run

A plan is a list of steps, and steps are the source of truth for progress. Each
one carries an imperative form ("Add the RPC handler") and the gerund shown
while it's running ("Adding the RPC handler"). As the agent works, each step
moves through `pending` → `in_progress` → `completed` (or `blocked` / `skipped`),
and the surface ticks it off live.

Every surface shows the plan just above the composer:

- **TUI** — a panel above the input, floating over the transcript.
- **Desktop** — a task bar above the composer; the proposal arrives as a card.
- **iOS** — a compact pill showing the title and step count; tap it to expand the
  full step list in a sheet.

The plan follows the conversation, not the window. Leave a chat mid-run and come
back — even from a different device — and the current plan is restored.

## If Flowly restarts

A plan left `executing` when the process stops is moved to **`paused`** on the
next start. The coroutine that was running it is gone and can't be revived, but
the plan and its completed steps survive on disk, and your surface offers to
resume from where it stopped.

The standing mode itself survives restarts too: a conversation you put in plan
mode is still in plan mode when Flowly comes back — the same way your exec
permission level persists.

## Where plans live

```text
~/.flowly/plan-mode/<session>/plan_<id>.json           # full snapshot
~/.flowly/plan-mode/<session>/plan_<id>.revisions.log  # append-only audit trail
```

Snapshots are written atomically (temp file, then rename), so a crash mid-write
can't leave a half-written plan. The revisions log appends one JSON line per
mutation — a readable history of what changed, when, and why.

Set `FLOWLY_PLAN_PERSIST=0` to keep plans in memory only, for a throwaway or
test instance.

## Limits

- **One active plan per conversation.** Proposing a new plan aborts the previous
  one.
- **The 10-minute approval timeout is not configurable.**
- **Cron and background runs can't approve** — a plan proposed there is rejected.
- **Live updates over the relay reach the device you're chatting on.** Other
  devices catch up when you open the conversation, rather than ticking along in
  real time.

## Related

- [Sandbox & exec approvals](/docs/using-flowly/sandbox-and-approvals) — the per-command gate plan mode sits on top of
- [Slash commands](/docs/reference/slash-commands) — `/plan` and the `Shift+Tab` cycle
- [Board](/docs/features/board) — for work you want queued and run, not planned per turn
- [File layout](/docs/reference/file-layout) — where plans live on disk
