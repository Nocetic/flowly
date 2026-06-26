---
title: Board — cross-channel task board
eyebrow: Features
description: A single task board you capture to from any channel — terminal, Telegram, voice — and that the agent can actually run, sequentially or as a fan-out of parallel sub-tasks, reporting the result back on the channel the card came from.
group: Automation
---

Most task boards are passive: you move cards around, but a human still does the
work. Flowly's Board is different. It's a normal personal to-do board **and** an
execution surface — you (or the agent) can tell it to *run* a card, and Flowly
does the work, then reports back on whatever channel the card came from.

Everything is local: cards live in a single SQLite file under your Flowly home.
Nothing is synced to any server.

## Quick start

The fastest way to feel it, from the terminal UI:

```text
> add "summarise today's AI news" to my board and run it
⚡ added · summarise today's AI news
… Flowly: I'm on it — I'll send the summary when it's done.

  (a minute later, unprompted)

Flowly: Here's today's AI roundup — OpenAI shipped …, Anthropic …, and …
```

The same flow works from Telegram, Discord, WhatsApp, email, or a voice call.
You can also drive it explicitly with the [`/board` command](#the-board-command)
in the terminal, or visually in the [desktop Board tab](#the-desktop-board).

## How it works

```text
capture (any channel)        run                        deliver
──────────────────────       ─────────────────────      ──────────────────────
"remind me to ship the   →   agent works the card   →   result comes back on
 release"                     (or N parallel cards)      the card's origin channel
       │                             │
       ▼                             ▼
  card on the board           todo → in_progress → done
```

Every card remembers **where it was created** — the `origin_channel` and
`origin_chat_id`. That single fact is what makes the board *cross-channel*: a
card dropped from Telegram reports back to Telegram; one from the terminal
reports back in the terminal; one from a voice call speaks the result.

### The lifecycle

```text
todo ──▶ in_progress ──▶ done
  │           │
  │           ├──▶ waiting     (blocked on input / approval) ──▶ in_progress
  │           └──▶ cancelled
  └──────────────────────────▶ cancelled
```

| Status | Meaning |
| --- | --- |
| `todo` | Captured, not started. A run that fails returns here (retryable). |
| `in_progress` | The agent is actively working it. |
| `waiting` | Paused — e.g. blocked on your input or an approval. |
| `done` | Finished; the agent's output is stored as the card's `result`. |
| `cancelled` | Stopped by you (or a worker that was killed mid-run). |

## Board, cron, or delegation?

Flowly has three overlapping ways to get work done without you babysitting it.
They're easy to mix up, so:

| You want to… | Use | Why |
| --- | --- | --- |
| Capture a task now and run it later or on demand, see it tracked, get the result back on your channel | **Board** | Durable card with a status you can watch; reports back where it came from. |
| Run something **on a schedule** | **[Cron](/docs/features/cron)** | Time-triggered; the board is on-demand. |
| Hand one task to a specialist **right now** and use the result **this turn** | **[Delegation](/docs/features/delegation)** (`delegate_to` / `spawn`) | Inline and ephemeral — no card, no tracking, no cross-channel report. |

> [!NOTE]
> `board_run` is built *on* delegation — running a card spawns a sub-agent under
> the hood. The difference is durability and reach: a card persists, shows up on
> the board, can run in parallel with siblings, and delivers its result back on
> the channel it came from. Reach for plain delegation when you just need a quick
> answer inside the current turn.

## Capturing cards

You never have to learn a syntax — just say what you want and the agent uses the
board tools for you:

> "add *pay the invoice* to my board"
>
> "put *draft the launch tweet* on the board"
>
> "remind me to review the PR — and run a quick summary of it now"

Cards capture their origin automatically, so the result knows where to go back.

### Board tools

The agent has five board tools. They're always available; the board file is
created lazily on first use.

| Tool | Parameters | What it does |
| --- | --- | --- |
| `board_add` | `title` (required), `body` | Capture a card. Origin channel/chat is recorded from the live session. |
| `board_list` | `status` | List cards, optionally filtered to one status. |
| `board_get` | `card_id` (required) | Fetch one card with its notes and full result. |
| `board_update` | `card_id` (required), `status`, `title`, `body`, `note`, `result` | Move a card, edit it, or append a note. |
| `board_run` | `card_id` **or** (`goal` + `subtasks[]`) | Execute an existing card, or split a goal into parallel sub-cards. |

> [!NOTE]
> The tools never block. `board_run` starts the work in the background and the
> result is **delivered to you later**, the same way a chat reply arrives — so
> the agent acknowledges and ends its turn instead of stalling for minutes.

## Running cards

### Sequential — one card

> "run the *summarise AI news* card"

Flowly moves the card to `in_progress`, runs it in an isolated sub-agent (with
the full tool set — web search, files, shell, …), stores the output as the
card's `result`, marks it `done`, and sends you the summary on the card's
channel.

### Parallel — a goal into sub-cards

> "fix these five failing tests in parallel"

The agent **decomposes** the goal into child cards and runs them concurrently.
Each child becomes its own card under a parent; you get **one consolidated
report** when they all finish.

```text
"fix these 5 tests"  →  parent card  ┐
                         ├─ test_auth        (running)
                         ├─ test_billing     (running)
                         ├─ test_webhooks    (queued)   ← capped at 5 at once
                         ├─ …
                         └─ when all terminal → "4/5 done, 1 failed"
```

> [!NOTE]
> Decomposition is the agent's job, not a hidden algorithm — it's a normal LLM
> step, so it uses your context to split work sensibly. At most **5 sub-cards
> run at once**; the rest queue and drain as slots free up.

### Controlling a run

- **Cancel** any time — "cancel that", the desktop Cancel button, or
  `/board cancel <id>`. A running card stops and moves to `cancelled`.
- **Failures are retryable** — a card that errors goes back to `todo` with the
  error recorded as a note, so you can fix and re-run.
- **Crash recovery** — if the gateway restarts while a card is `in_progress`,
  that card is reset to `todo` on boot (its worker is gone), so nothing is left
  stuck.

## Patterns

A few ways the board tends to get used in practice:

- **Fan-out.** Split one goal into independent pieces and run them at once —
  *"audit these 6 files in parallel"*. The agent creates a parent card and a
  child per piece; up to five run concurrently and you get one summary.
- **Capture anywhere, act anywhere.** Drop a card from your phone over Telegram,
  then open the desktop and hit **Run**; or capture it in the terminal and let a
  morning routine pick it up. The card doesn't care which surface touches it.
- **Human-in-the-loop.** Start a run and stay in control: cancel or redirect it
  with a single chat message, or move a card to `waiting` so it pauses for your
  go-ahead before continuing.
- **A trail you can read.** Every card keeps its notes, its error (if it failed),
  and its result. Open a card later to see exactly what happened.

> [!NOTE]
> The board runs **independent** sub-tasks in parallel; it doesn't yet model
> dependencies between cards (a strict "do A, then B" pipeline). For ordered
> steps, run them as separate cards in sequence, or describe the order inside a
> single card's task.

## The `/board` command

In the terminal UI, `/board` (alias `/kanban`) shows and operates the board
**inline** — it prints into the transcript, it does not open a modal.

```text
/board                 show the board, grouped by status
/board add <title>     add a card
/board run <id>        run a card
/board done <id>       move a card to Done
/board cancel <id>     cancel a running card
/board del <id>        delete a card
/board clear           remove all Done cards
/board help            this help
```

Card ids accept a unique **prefix** — if `c_a1b2c3d4` is the only card starting
with `c_a1`, then `/board run c_a1` is enough.

```text
> /board
📋 Board · 3 cards

○ To do (1)
- c_3f9a2b1c  pay the invoice · telegram

◐ In progress (1)
- c_77c0d4e8  summarise AI news

✓ Done (1)
- c_91be22af  weekly report
```

## The desktop board

The desktop app has a full **Board** tab:

- Four columns — To do / In progress / Waiting / Done — side by side, each
  scrolling internally when it fills up.
- **Drag a card** between columns to change its status.
- Per-card **Run / Cancel / Delete**, and inline **add** at the top.
- Click a card to open a **detail view** that renders the result as Markdown,
  with **Edit** for `todo`/`waiting` cards.
- Running cards show a subtle **shimmer**; the Done column has a one-click
  **clear**.

The desktop reads the same `board.db` (it polls `GET /api/board` a few times a
minute), so a card added in the terminal appears on the desktop within seconds,
and vice-versa.

## Where data lives

Everything is local, under your Flowly home — `~/.flowly/`, or
`~/.flowly/profiles/<name>/` for a named profile:

| File | Contents |
| --- | --- |
| `board.db` | Cards, statuses, results, notes (SQLite, WAL mode) |

The card's `result` field stores the agent's output, **truncated to ~200,000
characters** (with a trailing ellipsis when longer). The pre-truncation text is
not retained separately.

### Data model

```sql
cards(
  id TEXT PRIMARY KEY,          -- "c_" + 8 hex
  title, body,
  status,                       -- todo | in_progress | waiting | done | cancelled
  origin_channel, origin_chat_id,
  created_by,                   -- user | agent
  run_id,                       -- the sub-agent run while executing
  parent_id,                    -- set on decomposed child cards
  result, error,
  created_at, updated_at
)
card_notes(id, card_id → cards.id ON DELETE CASCADE, author, text, created_at)
```

## API reference

The board is exposed over the local gateway for clients (the desktop, the
terminal UI, your own scripts). All of it is localhost-only.

### HTTP

```text
GET  /api/board               → board snapshot (below)
POST /api/board/action        → { "action": "...", ... }
```

A **snapshot** looks like this (keys are camelCase for JS clients):

```json
{
  "columns": [
    { "status": "todo",        "cards": [ /* card objects */ ] },
    { "status": "in_progress", "cards": [] },
    { "status": "waiting",     "cards": [] },
    { "status": "done",        "cards": [] }
  ],
  "counts": { "todo": 1, "in_progress": 0, "waiting": 0, "done": 1, "cancelled": 0 },
  "total": 2,
  "timestampMs": 1780000000000
}
```

> [!NOTE]
> `cancelled` cards are counted but are **not** returned in `columns` — they
> don't show on the board.

**Actions** (`POST /api/board/action`, or the `board.action` WebSocket RPC):

| `action` | Fields | Effect |
| --- | --- | --- |
| `add` | `title`, `body?`, `originChannel?`, `originChatId?` | Create a card. |
| `move` | `cardId`, `status` | Change a card's status. |
| `update` | `cardId`, `title?`, `body?` | Edit a card. |
| `note` | `cardId`, `text`, `author?` | Append a note. |
| `run` | `cardId` | Run a card in the background. |
| `cancel` | `cardId` | Cancel a running card. |
| `delete` | `cardId` | Delete a card. |
| `clear_done` | `status?` (default `done`) | Bulk-delete finished cards. |

### WebSocket RPC

The same surface is available over the gateway WebSocket for the terminal UI:
`board.snapshot` (returns `{ "snapshot": <snapshot|null> }`) and `board.action`
(takes the action body above).

## Architecture

The board is **single-writer**: only the orchestrator writes `board.db`. The
sub-agents that execute cards never touch the database — they run, return a
result string, and the orchestrator records it.

> [!IMPORTANT]
> That one rule is why the board has **no claim-locks, compare-and-swap,
> heartbeats, or polling dispatcher**. Those exist in swarm systems to coordinate
> many worker *processes* racing on a shared database. Flowly's workers are
> in-process async sub-agents coordinated by a single owner, so none of that
> machinery is needed. Crash recovery is a one-line reset of orphaned
> `in_progress` cards on boot.

Completion delivery reuses the path a normal message already takes to reach you:

- **Real channels** (Telegram / WhatsApp / web / …) → their channel adapter.
- **Local clients** (terminal UI / desktop) have no channel adapter, so the
  gateway **pushes** the result over its WebSocket — the same mechanism is also
  how sub-agent results surface in the terminal.

When a card finishes, the agent is woken with the result and replies to you
naturally and in context (your persona, the conversation so far) — it isn't a
raw dump, it reads like a normal reply.

Source: `flowly/board/` (store + orchestrator), `flowly/agent/tools/board.py`
(tools), `flowly/gateway/server.py` (HTTP + WS API).

## FAQ

**Does the agent send my cards anywhere?**
No. `board.db` is a local file. The only thing that ever leaves your machine is
whatever a card's *task* requires (e.g. a web search), via your own model
provider.

**What happens if a run takes a long time?**
It runs in the background. You're free to keep chatting; the result is delivered
when it's ready. A sub-agent has a generous timeout and a bounded number of
steps, after which it returns whatever it has.

**Can a card create more cards?**
The agent can, via `board_run` with a goal + sub-tasks. But a *sub-agent*
running a card cannot itself fan out again — that guard prevents runaway
recursion.

**Why did my card go back to `todo`?**
It failed (the error is on the card as a note) or the gateway restarted
mid-run. Either way it's safe to run again.

## Related

- [Cron — scheduled tasks](/docs/features/cron)
- [Delegation](/docs/features/delegation)
- [Tools reference](/docs/reference/tools)
- [Slash commands](/docs/reference/slash-commands)
