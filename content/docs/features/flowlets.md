---
title: Flowlets
eyebrow: Features
description: Live, interactive mini-screens the agent builds for you — trackers, checklists, dashboards, forms — native on every device, driven by your own data, entirely local.
---

A **flowlet** is a small, live screen the agent builds when you ask for one: a
water tracker with a tap-to-drink button and a weekly chart, a shopping list you
check off, an expense tracker that reads receipts from a photo, a habit board
that reminds you at 9pm. You don't design it and you don't write any config —
you describe what you want ("track my daily water"), and the agent assembles it
from a catalog of building blocks. It then lives on your dashboard, updates
instantly when you tap it, and syncs across your devices.

If you'd rather not start from a blank page, there are also
[ready-made templates](#start-from-a-template) — pick one and it's yours.

Flowlets turn the agent from something you *talk to* into something that *builds
you tools*. Each one is a real, persistent mini-app that knows your world.

Everything is local. A flowlet's definition, its data, and its photos live in a
single SQLite file under your Flowly home — nothing is synced to any server, and
the same screen renders natively on Desktop and iOS.

## What makes a flowlet different

A flowlet is **not** a chat message, a static image, or a one-off report:

- **It's interactive.** Buttons, toggles, steppers, sliders, checklists, forms —
  you tap and it responds instantly.
- **It's live.** Its numbers, charts, and progress bars are computed from your
  data every time you open it. Log a glass of water and the weekly chart moves.
- **It's persistent.** It stays on your dashboard across sessions, versioned so
  earlier definitions are never lost.
- **It's native.** The same definition renders as native SwiftUI on iOS and a
  native component tree on Desktop — never a cramped web view.
- **It's yours.** It runs on your machine, on your data, with no round-trip to
  anyone's cloud.

Compared to [artifacts](artifacts.md) — which are renderable *outputs* you look
at — a flowlet is a renderable *surface you operate*, with its own state and
logic.

## How a flowlet works

The single most important idea: **your machine owns the truth; the screen is
just a renderer.** This is what keeps flowlets fast, correct, and private.

1. **The agent authors a definition.** When you ask for a tracker, the agent
   writes a small declarative document — components, state, and what each tap
   does — and validates it. No code, no HTML.
2. **Your device renders it natively.** The client reads the definition plus a
   flat `values` map (all the resolved numbers, lists, and chart data) and draws
   native controls. It does no math and makes no decisions.
3. **A tap is deterministic — no model involved.** When you press a button, the
   client sends only *"component X was tapped"*. Your machine looks up what that
   component's action is, applies it exactly (add a row, log a value, flip a
   toggle), and sends back a fresh `values` map. The screen re-renders. There is
   no language model in the loop for a tap, so it's instant, free, and can never
   "hallucinate" a wrong result.

The only tap that ever reaches the model is a deliberate one — an `agent` button
you designed to ask a question, or a `photo` capture that reads an image.

> [!NOTE]
> Because every tap is a named, declared action rather than free-form code, a
> flowlet you share can't do anything its definition doesn't spell out. This is
> the foundation the sharing model (below) is built on.

## Anatomy of a flowlet

A flowlet's definition is a handful of parts. You never write these — this is
what the agent assembles — but understanding them explains what a flowlet can do:

| Part | What it holds |
|------|---------------|
| **name / icon / accent** | The title, an icon, and a hex accent colour for the tile. |
| **state** | The values the flowlet remembers — a number, a string (a date is stored as one), a flag, a **list** of rows, or a timer. Each has a default. |
| **series** | An append-only event log ("drank 250ml at 3pm") that charts and totals read from. |
| **computed** | Derived values the agent never has to keep in sync — a sum over a list, a percentage, a status sentence. Recomputed every render. |
| **layout** | The screen itself: the tree of components you see and tap. |
| **screens** | Optional drill-down pages — tap a list row to open a detail/edit screen. |
| **watches** | Reminders the flowlet fires *itself*, with no scheduled model turn. |
| **sources** | Live external data the flowlet pulls in on a schedule. |

The full catalog of components, actions, and grammar is in the
[Flowlet catalog reference](../reference/flowlet-catalog.md).

## State, values, and computed numbers

A flowlet remembers things in **state**. A state key has a type — `number`,
`string` (a date is stored as one), `bool`, a `list` of structured rows, or a
`timer` — and a default. Tapping a control mutates state deterministically (a
stepper's `+` increments a number; a toggle flips a flag).

Anything derived is a **computed** value, so the agent never stores a number it
would have to keep correct by hand. A computed can be:

- an **aggregate over a list** — `sum` / `count` / `avg` / `min` / `max` of a
  field, optionally filtered (e.g. "total of `amount` for rows in the last 30
  days");
- an **aggregate over a series** — the same, over the event log, in a time
  window (`today`, `7d`, `30d`, `90d`, `all`);
- an **expression** — safe arithmetic and comparisons over other values
  (`max(0, monthly_budget - month_total)`), with a few date helpers
  (`days_until`, `days_since`, `weekday`);
- a **conditional sentence** — the first matching case wins, so a card can read
  "Over budget by 200 ₺" or "You're doing great" depending on the numbers.

Because these recompute from your data on every render, the screen is always
consistent — there's no stale total to get out of step.

## Semantic building blocks — the agent states intent, the system owns layout

For the common shapes, the agent doesn't hand-assemble rows and inputs from raw
primitives — it uses **composites** that own their own layout and wiring, so a
list row is never lopsided and a form is never mis-wired. Three of them:

- **`list_row`** — one row of a list. The agent says *which field is the title,
  which is the subtitle, which is the trailing value, which is the badge, which
  is the thumbnail image* — and the system lays it out: title on top, a muted
  detail line under it, the value right-aligned, a photo thumbnail on the left,
  a chevron if the row drills into a detail screen. Truncation, spacing, and
  alignment are guaranteed, not left to chance.
- **`form`** — a multi-field entry card ("add an expense" = title + amount +
  category + date). The agent lists the fields; the system builds the typed
  inputs, holds the draft, adds the row on submit, and clears the form. This is
  why adding an item "just works" instead of leaking half-filled state.
- **`tracker_card`** — a headline number plus a chart *about a list* ("this
  month" + a bar chart of daily spend). The total and the chart are both
  computed from the list's rows, so they can never drift from what's actually in
  the list — deleting a row updates both.

You never name these; the agent picks them. They're what make a generated
flowlet look designed rather than assembled.

## Lists you build up — todos, shopping, expenses

A `list` state key holds living rows you add and remove; a **repeater** renders
one row per item. This is how todo lists, shopping lists, journals, and expense
trackers work.

- **Add** a row with a quick-add input, a full `form`, or a `photo` capture.
- **Check off / edit** a row in place, or tap it to open a detail screen. Every
  user-owned list row is guaranteed to be editable — the system injects the edit
  controls if the agent didn't, so a wrong value can always be fixed.
- **Delete** a row by swiping it (a round, deliberate delete), everywhere.

Row edits and deletes are ordinary deterministic actions — no model turn — so a
50-item list stays instant.

## Charts

A flowlet draws its own charts natively (Swift Charts on iOS, hand-tuned SVG on
Desktop), from data your machine resolves — the client only draws.

- **Trends over time** — `bar`, `line`, or `area` over hours / days / weeks.
- **Compare series** — overlay up to four lines or grouped/stacked bars.
- **Breakdowns** — a `pie` or `donut` grouped by category (top slices, with the
  tail folded into "other").
- **A chart about a list** — point it at the list itself and it aggregates the
  rows, so it stays correct through every add, edit, delete, and photo capture.
  (This is what a `tracker_card` uses.)
- **Compact inline** — a `sparkline` or a `heatmap` for an at-a-glance history.

## Actions — what a tap does

Every interactive component carries a declared **action**. The common ones:

- **`set` / `increment` / `decrement` / `toggle`** — change a state value.
- **`log`** — append an event to a series (a "drink 250 ml" button).
- **`item_add` / `item_update` / `item_toggle` / `item_remove` / `item_move`** —
  manage a dynamic list.
- **`reset` / `remove_last`** — clear a value, or undo the last logged event.
- **`timer_toggle`** — start/pause a live stopwatch.
- **`batch`** — do several of the above at once.
- **`agent`** — the one action that reaches the model: hand it a message, exactly
  as if you'd typed it. Used sparingly, for the "ask my agent about this" button.

An action can be latched with **`once`** — at most once per day, per week, or
ever. This is what makes a "complete the day" button a real guarantee: after it
fires and resets your checkboxes, tapping it again the same day does nothing.

## Read a photo into a flowlet

A **`photo`** capture is a first-class action: a receipt, a meal, a whiteboard —
the agent reads the image in one isolated, tool-less turn and turns it into a
new list row: the expense's amount and merchant, the meal's calories and macros.
You can take the photo there and then, choose one from your library, or pick an
image file — a meal you already photographed is worth just as much as one you
shoot now.

The image is attacker-controlled input, so the reading turn runs with **every
tool disabled**; it can only look and answer.

## Reminders that fire themselves

A flowlet can carry **watches** — declarative reminders it evaluates on its own,
with no scheduled model turn burning tokens. A watch fires on a schedule
("every day at 21:00"), on a condition ("when today's water is still under the
goal after 6pm"), when a goal is first met, or when something's gone stale ("no
entry in 2 days"). It re-checks the instant its data changes and again on a
background heartbeat, with a cooldown so it never nags. Optionally, the agent can
*compose* the reminder text from the live numbers at fire time.

This is why a tracker feels like it's looking out for you — the reminder is part
of the flowlet, not a separate cron job. (For reminders that aren't tied to a
tracker, use [cron](cron.md).)

## Live, external data

A flowlet can bind a list to the outside world with **sources** — the agent
fetches something on a schedule (or on pull-to-refresh) and writes it into a
list the screen already renders: today's calendar, your open pull requests, the
weather. Source-owned lists are read-only in the UI — the snapshot is replaced,
not edited.

## Screens that adapt

Any component can carry a **visibility condition**, so a "you're over budget"
callout only appears when you actually are, and a "complete the day" button only
shows once both habits are checked. Text can be **conditional**, too — the same
card reads "3 days left" or "due today" from the numbers. All of it is evaluated
on your device against the live values, identically on every platform.

## Across your devices — and entirely local

A flowlet you create on Desktop appears on your iPhone and vice-versa, because
both talk to the same agent on your machine. The state is one source of truth;
tap it on one device and every open screen updates.

And it's all local:

- The definition, all state, every logged event, and every captured photo live
  in one SQLite file (`flowlets.sqlite`) under your Flowly home.
- Charts, totals, and conditions are computed on your machine.
- Nothing about a flowlet is sent to any external service unless a `source` you
  set up explicitly fetches it, or an `agent` button you designed sends a
  message.

## Start from a template

Asking for a screen works, but it assumes you already know what to ask for. So
Flowly ships a handful of **ready-made flowlets** — pick a card and you have a
working screen a second later:

| Template | What you get |
|----------|--------------|
| **Water** | A daily goal as a ring, one tap per glass, a weekly chart, and an evening nudge if you're behind. |
| **Habits** | Two habits to tick off, a "complete the day" button that can only fire once a day, a weekly streak, and 90 days on a grid. |
| **Expenses** | A monthly budget with an over-budget warning, a category breakdown, and entry by hand *or* straight from a receipt photo. |
| **Tasks** | A list you add to and tick off, split into what's open and what's done. |
| **Sleep** | Log the night, see the 30-day trend against your target, get a bedtime nudge. |
| **Mood** | A one-tap daily rating, 90 days on a grid, and room for a note about the day. |

Two things worth knowing:

- **It's yours immediately.** Creating from a template writes an ordinary
  flowlet you own — there's no lasting link back to the template. Ask the agent
  to change anything about it ("drop the second habit, add a 7am reminder") and
  it edits it like any other screen.
- **It arrives in your language.** The screen is built in whichever language the
  app is set to, so you get a finished screen rather than one to translate.

On an empty Flowlets page the templates *are* the page. Once you have flowlets
of your own they move out of the way into a single button.

## Talking to your agent about flowlets

You never write a definition — you ask. The agent builds, and refines on
request:

- *"Make me a water tracker with a goal of 3 liters and a weekly chart."*
- *"Add a habit board — read 1 hour, code 5 hours — with a 9pm reminder."*
- *"Build an expense tracker where I can add a receipt by photo, and show a
  monthly total and a category breakdown."*
- *"On my water tracker, make the daily goal 2.5 liters."* → it edits the
  existing one, versioned.
- *"How much did I spend on groceries this month?"* → it reads the flowlet's
  live values and just tells you.

Behind the scenes, when the agent creates or updates a flowlet it also runs a
quick **self-review** — a deterministic set of quality checks (is a list
add-able? does a chart drift from its list? does a "complete the day" button
have a once-per-day latch?) plus a preview against sample data — and fixes what
it flags. That's why a generated flowlet tends to come out right the first time.

## Limits and safety

Flowlets are bounded by design, so a generated one can never run away:

- A definition is capped in size and component count; lists cap at 200 rows.
- Photo captures and `agent` taps are rate-limited per flowlet and globally.
- A `source`'s fetch and a watch's `agent` compose are throttled.
- The reading turn for a captured photo runs with **all tools disabled** — it
  can look at the image and nothing else.

## Reference

- **[Flowlet catalog](../reference/flowlet-catalog.md)** — the complete
  component list, action ops, chart shapes, expression grammar, watches and
  sources schemas, and the serve-time guarantees.
- [Artifacts](artifacts.md) — renderable outputs you look at, the sibling of a
  flowlet's interactive surface.
- [Cron](cron.md) — scheduled reminders that aren't tied to a flowlet.
