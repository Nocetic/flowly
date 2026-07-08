---
name: flowlets
description: "Build a flowlet — a personal, persistent mini-screen (water tracker, habit grid, mood log) the user controls on Desktop and iOS. Use when the user asks for a tracker, reminder screen, counter, dashboard, or any custom little app."
metadata: {"flowly":{"emoji":"📲","tags":["flowlet","ui","tracker","dashboard","screen","mini-app"],"related_skills":["apple-reminders"]}}
---

# Flowlets

A **flowlet** is a small screen you build for the user — a water tracker, a
habit checklist, a mood log, a pomodoro counter. It renders **natively** on
their Desktop and iPhone and stays in sync. The user taps buttons and sliders;
those taps are applied **instantly and deterministically** — you are NOT in the
loop for them. You only get involved to **build** the flowlet, to **log data
the user tells you in chat**, or to **answer questions** about it.

Use the `flowlet` tool. Build with a declarative JSON `definition`. Never write
HTML or code — you assemble components from the catalog below.

## The mental model — three separate things

1. **State** — values that change, declared under `state` (e.g. `goal_ml`).
2. **Series** — an append-only event log, declared under `series` (e.g. every
   glass of water is one `water` event). Charts and totals read from this.
3. **Computed** — derived numbers under `computed`: either an aggregation of a
   series (`{"series":"water","agg":"sum","window":"today"}`) or a safe
   arithmetic `expr` over other keys (`{"expr":"max(0, goal_ml - today_ml)"}`).

Labels interpolate any scalar key with `{key}` (e.g. `"{today_ml} / {goal_ml} ml"`).

## Golden rule for edits

To change an existing flowlet, **first call `flowlet` with `action:"get"`** to
read its current definition, then send the full updated definition with
`action:"update"`. Never guess the current shape.

## Actions on the tool

- `create` — `{definition}` → builds it. The definition holds `catalog`,
  `name`, `icon`, `accent`, `state`, `series`, `computed`, `layout`.
- `update` — `{flowlet_id, definition}` (full replace, versioned) or
  `{flowlet_id, pinned}`.
- `get` — `{flowlet_id}` → definition **and current live values**. Use this to
  answer "how much water did I drink today?".
- `list` — all flowlets with their live values.
- `delete` — `{flowlet_id}`.
- `log` — `{flowlet_id, series, value}` — add a data point when the user tells
  you in chat ("I just drank 500ml"). Updates every open screen instantly.
- `set_state` — `{flowlet_id, key, value}` — change a state value ("make my
  goal 3 liters").
- `query` — `{flowlet_id, series, agg, window}` → one number, to answer a
  question without dumping the whole flowlet.

## Component catalog (catalog: 1)

Always start the definition with `"catalog": 1`.

**Layout:** `card`, `row`, `column`, `grid`, `list` (all take `children`),
`divider`, `spacer`.

**Display:** `header` (text), `text` (text, interpolates `{key}`), `badge`
(text), `icon` (name), `stat` (value, label), `progress` (value, max),
`ring` (value, max), `gauge` (value, min, max), `chart` (data + `kind`:
line/bar/area), `sparkline` (data), `heatmap` (data), `table` (rows),
`clock`, `countdown` (target).

**Display — structured / professional:**
- `metric` — a headline number: `value` (bind), `unit?`, `label?`, plus an
  optional trend `delta` (bind) and `deltaLabel?` (e.g. "vs last hour"); set
  `invert:true` when down is good.
- `status` — a semantic pill: `text` + `tone` (`ok`/`warn`/`bad`/`neutral`).
- `keyvalue` — labeled pairs: `rows:[{label, value}]` (value interpolates `{key}`).
- `timeline` — dated events: `events:[{title, time?, tone?}]` (tone: done/now/wait).
- `callout` — a toned note box: `text` + `tone` (`info`/`success`/`warn`/`bad`) + `icon?`.
- `code` — a monospaced block: `text` (+ `language?`).
- `link` — opens a URL: `text` + `url` (http/https).
- `image` — `src` (http/https/data) + `alt?`.

**Input (carry an `action`, need an `id`):** `button` (text), `icon_button`
(icon), `stepper` (value), `slider` (min, max, value), `toggle` (value),
`checklist` (items — each `{key,label}`, toggles a bool state key), `segmented`
(options), `input`, `number_input`, `rating` (max), `select` (options, for
more than ~4 choices), `date` (a date → `set`), `textarea` (long text → `set`),
`timer` (a stopwatch — see below).

**Timer** — for billable time, an experiment, a workout. Declare a state key of
`type:"timer"`, and a `timer` component with `value:"<key>"` and
`action:{op:"timer_toggle", key:"<key>"}`. It ticks live while running; the bot
persists elapsed seconds across sessions.

`value`/`max`/`min` on a display component are either a number or the name of a
state/computed key. A `chart`/`sparkline`/`heatmap` `data` object is
`{"series": "...", "agg": "sum|count|avg|min|max|last", "bucket": "hour|day|week",
"window": "today|7d|30d|90d|all"}`.

## Actions (what a tap does — declared, deterministic, no LLM)

Put an `action` on an input component. Ops:

- `set` — `{op:"set", key}` — write the component's value to a state key
  (slider/input/number_input/segmented).
- `increment` / `decrement` — `{op:"increment", key, by?}` (stepper, buttons).
- `toggle` — `{op:"toggle", key}` (toggle switch; checklist items toggle their own key).
- `log` — `{op:"log", series, value?}` — append an event (a "drink 250ml"
  button uses a fixed `value`; a `rating` passes the tapped value).
- `remove_last` — `{op:"remove_last", series}` — an undo button.
- `reset` — `{op:"reset", key}` or `{op:"reset", series}`.
- `agent` — `{op:"agent", message}` — hands `message` to you as a normal turn
  (e.g. an "Analyze my week" button). Your reply is delivered to the chat.
- `timer_toggle` — `{op:"timer_toggle", key}` — start/stop a `timer` state key.
- `batch` — `{op:"batch", ops:[...]}` — several ops at once.

A button with a fixed `value` (like drink-250) ignores any client value. Free
inputs (slider/input/rating) supply their value, validated to the component's
bounds.

## Worked example — water tracker

```json
{
  "catalog": 1,
  "name": "Water",
  "icon": "droplet",
  "accent": "#00A6C8",
  "state": { "goal_ml": { "type": "number", "default": 2000, "min": 250, "max": 10000 } },
  "series": { "water": { "unit": "ml" } },
  "computed": {
    "today_ml": { "series": "water", "agg": "sum", "window": "today" },
    "remaining": { "expr": "max(0, goal_ml - today_ml)" }
  },
  "layout": [
    { "type": "header", "text": "Today" },
    { "id": "bar", "type": "progress", "value": "today_ml", "max": "goal_ml", "label": "{today_ml} / {goal_ml} ml" },
    { "type": "row", "children": [
      { "id": "drink", "type": "button", "text": "Drank 250 ml", "style": "primary", "action": { "op": "log", "series": "water", "value": 250 } },
      { "id": "undo", "type": "icon_button", "icon": "undo", "action": { "op": "remove_last", "series": "water" } }
    ]},
    { "id": "goal", "type": "slider", "min": 1000, "max": 4000, "step": 250, "label": "Daily goal", "value": "goal_ml", "action": { "op": "set", "key": "goal_ml" } },
    { "id": "week", "type": "chart", "kind": "bar", "data": { "series": "water", "agg": "sum", "bucket": "day", "window": "7d" } }
  ]
}
```

Other easy builds with the same pieces: habit checklist (bool state keys +
`checklist`), mood log (`rating` → `log`, `sparkline`), pomodoro (`ring` +
count), weight/budget/reading trackers.

## Pairing with a reminder

If the user wants a nudge ("remind me every 2 hours"), also create a `cron` job
whose message tells you to read the flowlet and write a short reminder — e.g.
*"Read the Water flowlet (flowlet tool, action=get) and, based on how much is
left, send one short line."* Deliver it to the user's chat.

## Language

Write all user-facing text (`name`, labels, button text) in the user's
language. Keep it plain and warm — no jargon like "component" or "state".

## Write text in the user's language

All labels the user sees should match the language they speak to you in.
