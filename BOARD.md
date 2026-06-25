# Flowly Board

The Board is a **cross-channel task board**. Capture a task from any channel —
the terminal, Telegram, a voice call — and it lands on a single board. Ask
Flowly to run a card and the agent works it (alone, or as a fan-out of parallel
sub-tasks) and reports the result back on the channel the card came from.

It is two things at once: a plain personal to-do board, *and* an execution
surface where the agent actually does the work. Cards live in a local SQLite
file; nothing leaves your machine.

---

## Mental model

```
  capture (any channel)        run                       deliver
  ───────────────────────      ───────────────────────   ──────────────────
  "remind me to ship the   →   agent works the card  →   result comes back on
   release"                     (or N parallel cards)     the card's origin
        │                              │                        channel
        ▼                              ▼
   card on the board            todo → in_progress → done
```

A card carries the **channel it was created from** (`origin_channel` /
`origin_chat_id`). That's what makes the board cross-channel: a card dropped
from Telegram remembers to report back to Telegram; one from the terminal
reports back in the terminal.

Statuses: `todo → in_progress → waiting → done` (plus `cancelled`).

---

## Capturing cards

Just say so, on any channel — the agent uses the board tools:

> "add *pay the invoice* to my board"
> "put *summarise today's AI news* on the board and run it"

Or from the terminal UI, use the [`/board` command](#the-board-command).

### Board tools (what the agent can call)

| Tool | Purpose |
|---|---|
| `board_add` | Capture a card. Origin channel/chat is recorded automatically. |
| `board_list` | List cards, optionally filtered by status. |
| `board_get` | Fetch one card with its notes and result. |
| `board_update` | Move a card, edit its title/body, or append a note. |
| `board_run` | Execute a card, or split a goal into parallel sub-cards. |

The tools are always available; the board itself is created lazily at
`~/.flowly/board.db` on first use.

---

## Running cards

`board_run` executes work in the **background** and delivers the finished
result to the card's origin channel when it's done — the same way a chat reply
arrives. Acknowledge, end the turn, get notified on completion. There is no
blocking and no second "go fetch the result" step.

### Sequential — one card

> "run the *summarise AI news* card"

The agent runs the card in an isolated sub-agent, marks it `done`, stores the
output as the card's **result**, and sends you the summary.

### Parallel — a goal split into sub-cards

> "fix these 5 tests in parallel"

The agent decomposes the goal into child cards and runs them concurrently
(capped at 5 at a time). Each child is its own card under the parent; when all
finish you get **one consolidated report**.

### Controlling a run

- **Cancel** a running card any time — from chat ("cancel that"), the desktop
  Cancel button, or `/board cancel <id>`.
- A failed card returns to `todo` with the error recorded, so it's retryable.
- On restart, any card left `in_progress` by a previous process is reset to
  `todo` automatically (crash recovery).

---

## The `/board` command

In the terminal UI, `/board` (alias `/kanban`) shows and operates the board
**inline** — no modal:

```
/board                 show the board (grouped by status)
/board add <title>     add a card
/board run <id>        run a card
/board done <id>       move a card to Done
/board cancel <id>     cancel a running card
/board del <id>        delete a card
/board clear           remove all Done cards
/board help            this help
```

Card ids accept a unique **prefix** — `c_a1` is enough if it's unambiguous.

---

## The desktop board

The desktop app has a full **Board** tab: four columns (To do / In progress /
Waiting / Done), drag-to-move, inline add, per-card Run / Cancel / Delete, and
a card detail view that renders the result as Markdown. Running cards shimmer.
Done cards can be cleared in one click.

The desktop reads the same `board.db` (it polls the gateway's `/api/board`), so
a card added in the terminal shows up on the desktop within a few seconds, and
vice-versa.

---

## Where data lives

Everything is **local**. Under your Flowly home (`~/.flowly/`, or
`~/.flowly/profiles/<name>/` for a named profile):

| File | Contents |
|---|---|
| `board.db` | Cards, statuses, results, notes (SQLite, WAL) |
| `artifacts.sqlite` | Full sub-agent output when a result is large |

The card's `result` field holds the agent's output (truncated for display);
the complete output, when long, is saved as an artifact. Nothing is synced to
any server.

---

## Architecture notes

The board is **single-writer**: only the orchestrator writes `board.db`.
Sub-agents that execute cards never touch it — they run, return a result
string, and the orchestrator records it. That single rule is why the board
needs no claim-locks, compare-and-swap, heartbeats, or a polling dispatcher:
Flowly's workers are in-process async sub-agents coordinated by one owner, not
a fleet of processes racing on a shared database.

Completion delivery reuses the same path a normal message takes to reach you:
channel adapters for Telegram/WhatsApp/etc., and a gateway WebSocket push for
local clients (terminal UI / desktop) that have no channel adapter.

- Storage + tools: `flowly/board/`, `flowly/agent/tools/board.py`
- Execution: `flowly/board/orchestrator.py`
- HTTP + WS API: `flowly/gateway/server.py` (`/api/board`, `board.snapshot`,
  `board.action`)
