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

## Reminders belong to the flowlet — never a cron job

If the user asks for a reminder, nudge, or notification tied to a flowlet
("her gün hatırlat", "1 dakikada bir haber ver", "geride kalırsam uyar",
"hedefi tutunca kutla") that goes in the flowlet's own **`watches`** array (see
below) — put it in the `definition` when you `create`/`update`. **Do NOT create
a `cron` job for it.** A watch is evaluated by the bot itself (no model turn),
so it's cheaper, instant, and the user sees it on the screen. Only fall back to
`cron` when the reminder needs logic no `when` expression can express (a web
lookup, cross-flowlet reasoning).

## Actions on the tool

- `create` — `{definition}` → builds it. The definition holds `catalog`,
  `name`, `icon`, `accent`, `state`, `series`, `computed`, `layout`, and
  `watches` (reminders — include them here when the user asks for any).
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
- `notify` — `{flowlet_id, title, body}` — fire a one-off reminder right now
  (push on mobile, native notification on desktop). For *recurring* or
  *conditional* reminders use `watches` instead (they fire themselves); reach
  for `notify` only for a single immediate ping.

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
  The message templates `{value}` (what the user typed/tapped) and any `{key}`
  live value — so a free-text input can say
  `{"op":"agent","message":"Log this meal and estimate calories: {value}"}`
  and you'll receive the user's own words to interpret.
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
  ],
  "watches": [
    { "id": "behind", "trigger": "condition", "when": "today_ml < goal_ml", "after": "18:00",
      "notify": { "title": "Su hatırlatması", "body": "{today_ml} / {goal_ml} ml — biraz geridesin" } },
    { "id": "reached", "trigger": "goal", "when": "today_ml >= goal_ml", "once": true,
      "notify": { "title": "Hedef tamam 🎉", "body": "{today_ml} ml — harika gidiyorsun" } }
  ]
}
```

The user asked for a water tracker *and* a reminder → both live in this one
definition. A "remind me every day at 9" would instead be
`{ "id": "morning", "trigger": "schedule", "at": "09:00", "notify": {...} }`;
"every 2 minutes" → `"everyMinutes": 2`. Never a separate cron job.

Other easy builds with the same pieces: habit checklist (bool state keys +
`checklist`), mood log (`rating` → `log`, `sparkline`), pomodoro (`ring` +
count), weight/budget/reading trackers.

## Dynamic lists — todo / shopping / journal screens

A `list` state key holds living rows the user adds and removes; a `repeater`
renders them. This is how you build a todo list, shopping list, or note log:

```json
{
  "catalog": 1, "name": "Görevler", "icon": "check", "accent": "#7C6FF0",
  "state": {
    "tasks": { "type": "list", "item": { "title": "string", "done": "bool" }, "max": 100 }
  },
  "layout": [
    { "id": "new_task", "type": "input", "placeholder": "Yeni görev…",
      "action": { "op": "item_add", "key": "tasks" } },
    { "type": "repeater", "source": "tasks", "empty": "Henüz görev yok",
      "item": { "type": "row", "children": [
        { "id": "tgl", "type": "toggle", "value": "$.done",
          "action": { "op": "item_toggle", "key": "tasks", "field": "done" } },
        { "type": "text", "text": "{$.title}" },
        { "id": "del", "type": "icon_button", "icon": "trash",
          "action": { "op": "item_remove", "key": "tasks" } } ] } }
  ]
}
```

The rules:
- `item` declares the field schema (`string` / `number` / `bool` / `date`,
  ≤ 8 fields; `id` is reserved — assigned automatically).
- Inside the repeater's `item` template, `$.field` binds a prop to the current
  row and `{$.field}` interpolates it into text. Row actions
  (`item_toggle` / `item_update` / `item_remove` / `item_move`) must live
  inside the repeater; `item_add` can sit anywhere (the quick-add input above
  maps its text to the single string field).
- The screen's grid card automatically previews as "done/total" when the item
  schema has a bool field.
- A journal = a list of `{text: "string", day: "date"}` rendered the same way.

**Reason about a list** with a `computed` that aggregates it —
`{ "list": "<key>", "agg": "count|sum|avg|min|max", "field?": "...", "where?": "<expr>" }`
→ a number you can show or gate on. `where` runs per item with its fields (and
date fns) in scope:
```json
"computed": {
  "open":   { "list": "tasks", "agg": "count", "where": "done == 0" },
  "overdue":{ "list": "tasks", "agg": "count", "where": "days_until(due) < 0" },
  "total":  { "list": "cart",  "agg": "sum",   "field": "price" }
}
```
Then: `stat value="open"`, `visibleWhen="open == 0"` (all done → hide the list,
show a "hepsi tamam 🎉" callout), or a watch `when="open == 0"`.

## Live data — bind a screen to the outside world (`sources`)

A flowlet doesn't have to hold only what the user logs. A top-level `sources`
object binds a **source-owned** state key to live data you fetch on a schedule —
so "show my repo's commits, hourly" is a self-refreshing panel, not a chore.
This is what makes a flowlet feel like it *knows the user's world*.

```json
{
  "catalog": 1, "name": "Repo", "icon": "activity", "accent": "#7C6FF0",
  "state": {
    "repo": { "type": "string", "default": "Nocetic/flowly" },
    "commits": { "type": "list", "item": { "title": "string", "who": "string", "at": "date" },
                 "source": true }
  },
  "sources": {
    "commits": { "kind": "agent",
                 "prompt": "the last 10 commits to {repo} in the past hour, newest first",
                 "into": "commits", "refresh": "1h", "limit": 10 }
  },
  "layout": [
    { "id": "repo", "type": "input", "placeholder": "owner/repo",
      "action": { "op": "set", "key": "repo" } },
    { "type": "repeater", "source": "commits", "empty": "No recent commits",
      "item": { "type": "row", "children": [
        { "type": "text", "text": "{$.title}" },
        { "type": "badge", "text": "{$.who}" } ] } }
  ]
}
```

The rules:
- Declare the target key with **`"source": true`** — it's owned by the source,
  read-only to the user (no `set`/`item_add` on it; the snapshot is replaced
  each refresh).
- **`kind: "agent"`** — a normal turn of yours (with all your tools) fetches the
  data and returns JSON matching the target's schema. Same privilege as a cron
  self-prompt; it runs on a schedule, isolated from chat.
- `prompt` — what to fetch, templated with `{key}` live values (e.g. `{repo}`
  from an input, so the user can retarget it).
- `into` — the source-owned key; a `list` → an array of `{item fields}`, a
  scalar (number/string) → one value. `limit` caps a list.
- `refresh` — `"manual"` (only on open / a refresh tap) or `"15m"` / `"1h"`
  (min 10 m; sources are throttled and back off on failure, keeping stale data).
- A source panel refreshes when the user opens the screen and on its interval;
  the client can pull-to-refresh. The user sees every source in plain language
  in the screen's "Data sources" section — keep prompts honest.

Use this for dashboards over the user's real world (repos, calendar, tasks,
weather, metrics) — anything you can fetch with your tools.

## Adaptive screens — `visibleWhen` + conditional text

Two tools make a screen react to its own data instead of looking static:

**`visibleWhen`** — any component may carry a boolean expression; the client
hides it while the expression is falsy. Use it for warnings, celebrations, and
sections that only matter sometimes:

```json
{ "type": "callout", "tone": "warn", "text": "Over budget by {over} ₺",
  "visibleWhen": "over > 0" }
```

Names must be declared state/computed keys (same grammar as watch `when`:
arithmetic, comparisons, `and/or/not`, `min/max/abs/round`).

**Dates.** The grammar also has `now()`, `weekday()` (0=Mon…6=Sun), and
`days_until("YYYY-MM-DD" | key)` / `days_since(...)` — so a deadline reacts to
time: `visibleWhen: "days_until(due) <= 1"` (a `due` string state key or a
`date` item field), a `cases` "bugün!" / "{n} gün kaldı", or a watch
`when: "days_until(due) == 0"`.

**Conditional text** — a `computed` entry with `cases` resolves to a *string*:
the first truthy `when` wins, `{key}` templating works, `else` is the fallback.
Consume it like any value: `"text": "{statusText}"`.

```json
"statusText": { "cases": [
    { "when": "today_ml >= goal_ml", "text": "Hedef tamam — {today_ml} ml 🎉" },
    { "when": "today_ml >= goal_ml / 2", "text": "Yarıyı geçtin ({today_ml} ml)" }
  ], "else": "Şimdilik {today_ml} ml — devam" }
```

Prefer these over always-visible static text: a screen that says "geridesin" /
"harikasın" at the right moment feels alive.

## Reminders that fire themselves — `watches`

A flowlet can watch itself and push a reminder with **no cron job and no LLM
turn**. Add a top-level `watches` array. The bot evaluates each rule
deterministically (on a 60-second heartbeat and the instant the user taps), and
when one fires it pushes to the phone + pops a desktop notification that opens
the flowlet on tap. **Prefer this over a cron+notify pairing** — it's cheaper,
instant, and the user can see the rule on the screen.

Four triggers:

- `schedule` — a time of day (`"at": "20:00"`), an interval (`"everyMinutes": 120`),
  optionally limited to `"days": ["mon","wed","fri"]`. Fires once per day for `at`
  (catches up if the bot was offline). *"Daily 9am summary."*
- `condition` — a boolean `when` expression over your state/computed keys, e.g.
  `"today_ml < goal_ml"`. **Edge-triggered**: fires once when it flips false→true,
  never again until it drops and rises again. Add `"after": "18:00"` to only nag
  in the evening. *"Behind on water after 6pm."*
- `goal` — same as condition but for celebrating a target: `"when": "today_ml >= goal_ml"`.
  Use `"once": true` for a one-time congratulations. *"You hit your goal 🎉"*
- `stale` — no activity for `"idleMinutes": 180`. Re-arms only after fresh
  activity. *"Haven't logged in 3 hours."*

Each watch needs a stable `"id"` and a `"notify": { "title", "body" }`. The
title/body may template current values with `{key}` — e.g. `"{today_ml} / {goal_ml} ml"`.

```json
"watches": [
  { "id": "evening_nudge", "trigger": "condition",
    "when": "today_ml < goal_ml", "after": "18:00",
    "notify": { "title": "Water check", "body": "{today_ml}/{goal_ml} ml — a bit behind" } },
  { "id": "goal_hit", "trigger": "goal", "when": "today_ml >= goal_ml", "once": true,
    "notify": { "title": "Goal reached 🎉", "body": "{today_ml} ml today — nice." } },
  { "id": "morning", "trigger": "schedule", "at": "09:00",
    "notify": { "title": "New day", "body": "Fresh water goal for today." } }
]
```

**Composed notifications:** add `"compose": true` inside `notify` and *you*
write the notification when the watch fires — you get the live screen data and
send a short, personal push via the flowlet notify action ("Dün 3L içmiştin,
bugün yavaşsın — bir bardak?"). The static `title`/`body` stay as the fallback
when composing isn't possible. Use it where a personal touch beats a template;
plain templated pushes are cheaper and instant.

Guidance: keep reminders kind and rare. `when` expressions support
`+ - * / min() max() abs() round()`, comparisons `< <= > >= == !=`, and
`and / or / not`. Names must be declared state or computed keys. Default
cooldowns already stop nagging (condition 6h, goal 12h, stale 12h); override
with `"cooldownMinutes"`. Only add `"also": { "op": "agent", "message": "…" }` when
the reminder genuinely needs you to *do* something (draft a message, look
something up) — it costs a model turn and is throttled to ≥30 min.

For logic too rich for a `when` expression (needs a web lookup, cross-flowlet
reasoning), fall back to a `cron` job that reads the flowlet (action=get) and
calls the `notify` action.

## Language

Write all user-facing text (`name`, labels, button text) in the user's
language. Keep it plain and warm — no jargon like "component" or "state".

## Write text in the user's language

All labels the user sees should match the language they speak to you in.
