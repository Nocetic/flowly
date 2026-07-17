---
title: Flowlet catalog
eyebrow: Reference
description: The complete flowlet definition — every component, action, chart shape, expression, watch and source, plus the guarantees the system fills in on its own.
---

This is the exhaustive reference for a **flowlet** definition: the declarative
document the agent authors for a live mini-screen. For the concepts and how to
ask for one, see the [Flowlets feature guide](../features/flowlets.md). You never
write this by hand — it's here so you can see exactly what a flowlet can express.

Current catalog version: **3**. A definition carries a `catalog` number; a client
renders anything it understands and gracefully placeholders the rest, so a higher
catalog is always forward-compatible.

## Definition shape

```json
{
  "catalog": 3,
  "name": "Water",
  "icon": "droplet",
  "accent": "#00A6C8",
  "state":    { "...": "..." },
  "series":   { "...": "..." },
  "computed": { "...": "..." },
  "layout":   [ "...components..." ],
  "screens":  { "...": "..." },
  "watches":  [ "..." ],
  "sources":  [ "..." ]
}
```

| Key | Required | Purpose |
|-----|----------|---------|
| `catalog` | yes | Catalog version (integer, ≤ 3). |
| `name` | yes | Title, ≤ 100 chars. |
| `icon` | no | Icon name for the tile. |
| `accent` | no | Hex colour (`#00A6C8` or `#0AC`). |
| `state` | no | `{key: spec}` — what the flowlet remembers. |
| `series` | no | `{name: {unit?}}` — append-only event logs. |
| `computed` | no | `{key: spec}` — derived values. |
| `layout` | yes | Array of components — the screen. |
| `screens` | no | `{id: {title?, layout}}` — drill-down pages (≤ 6). |
| `watches` | no | Reactive reminders (≤ 20). |
| `sources` | no | Live data bindings (≤ 8). |

## State

`state` maps a key to a typed cell with a default.

| Type | Value | Notes |
|------|-------|-------|
| `number` | a number | Optional `min` / `max` clamp. |
| `string` | text | Optional `maxLength`. A date is stored as a `"YYYY-MM-DD"` string. |
| `bool` | true/false | |
| `timer` | `{running, elapsed}` | A live stopwatch; toggled with `timer_toggle`. |
| `list` | array of rows | The living rows of a repeater. |

(There is no top-level `date` state type — a stored date is a `string`. `date`
*is* a list item-field type and a `date` input component.)

A `list` declares an **item schema** — `{field: type}` with up to 8 fields of
`string` / `number` / `bool` / `date` / `image` (the `id` field is reserved and
auto-assigned). A list caps at 200 rows (`"max": N` to lower it). A list marked
`"source": true` is owned by a [data source](#sources) and is read-only in the
UI.

```json
"state": {
  "goal_ml":   { "type": "number", "default": 2000 },
  "expenses":  { "type": "list", "max": 200, "item": {
      "title": "string", "amount": "number", "category": "string",
      "date": "date", "receipt": "image" } }
}
```

## Series

An append-only event log. Charts and windowed aggregates read from it; you add to
it with the `log` action.

```json
"series": { "water": { "unit": "ml" } }
```

## Computed

Derived values, resolved on every render in dependency order. Exactly one form
per key:

**Aggregate a series** — over a time window:
```json
"today_ml": { "series": "water", "agg": "sum", "window": "today" }
```

**Aggregate a list** — with an optional per-row filter:
```json
"month_total": { "list": "expenses", "agg": "sum", "field": "amount",
                 "where": "days_since(date) < 30" }
```

**Expression** — safe arithmetic / comparison over other values:
```json
"remaining": { "expr": "max(0, monthly_budget - month_total)" }
```

**Conditional text** — first matching `when` wins:
```json
"status": { "cases": [
    { "when": "month_total > monthly_budget", "text": "Over by {over} ₺" },
    { "when": "month_total == 0", "text": "No spending yet" } ],
  "else": "{remaining} ₺ left this month" }
```

- `agg` ∈ `sum` `count` `avg` `min` `max` `last` for a series; a **list**
  aggregate excludes `last` (`sum`/`count`/`avg`/`min`/`max`).
- `window` ∈ `today` `7d` `30d` `90d` `all`.

## Components

49 component types in four groups. Every component may carry an `id` (required
when it has an `action`, auto-assigned if you forget) and a `visibleWhen`
expression.

### Layout (8)

| Type | Props | Notes |
|------|-------|-------|
| `card` | `children` | The app's card surface. |
| `row` | `children` | Horizontal. |
| `column` | `children` | Vertical. |
| `grid` | `children`, `columns` | Equal columns; a chart-bearing grid is forced full-width. |
| `list` | `children` | Divided rows. |
| `divider` | — | A hairline. |
| `spacer` | `size` | Vertical gap. |
| `repeater` | `source`, `item`, `empty?`, `navigate?`, `where?`, `sortBy?` | One `item` per row of a `list`. See [Dynamic lists](#dynamic-lists). |

### Display (23)

| Type | Props |
|------|-------|
| `header` | `text`, `subtitle?` |
| `text` | `text` (interpolates `{key}`) |
| `badge` | `text` |
| `icon` | `name` |
| `stat` | `value` (bind), `label?` |
| `metric` | `value`, `unit?`, `label?`, `delta?`, `deltaLabel?`, `invert?` |
| `progress` | `value`, `max` |
| `ring` | `value`, `max`, `label?` |
| `gauge` | `value`, `min`, `max`, `label?` |
| `chart` | `data`, `kind` — see [Charts](#charts) |
| `sparkline` | `data` |
| `heatmap` | `data` |
| `table` | static `rows`, or a data-bound `source` + `columns` — see [Tables](#tables) |
| `status` | `text`, `tone?` (`ok` / `warn` / `bad` / `neutral`) |
| `keyvalue` | `rows: [{label, value}]` |
| `timeline` | `events: [{title, time?, tone?}]` (tone `done` / `now` / `wait`) |
| `callout` | `text`, `tone?` (`info` / `success` / `warn` / `bad`), `icon?` |
| `code` | `text`, `language?` |
| `link` | `text`, `url` |
| `image` | `src` (a URL, a `data:` URI, a `$.field` ref, or a state key), `height?`, `alt?` |
| `clock` | `seconds?` |
| `countdown` | `target` (epoch ms or `"YYYY-MM-DD[THH:MM]"`, read in local time) |
| `timer` | `value` (a `timer` state key) |

A `value`/`max`/`min` bind is a number literal or the name of a state/computed
key. An `icon` (on `icon` / `icon_button` / `checklist` items) is a name from a
vetted set; an unknown name degrades to a sensible fallback.

### Input (15)

| Type | Props | Fires |
|------|-------|-------|
| `button` | `text`, `style?` (`primary`/`secondary`/`ghost`/`destructive`), `action` | its action |
| `icon_button` | `icon`, `action` | its action |
| `input` | `action` (`set`/`item_update`/`item_add`), `value?`, `placeholder?`, `label?`, `maxLength?` | on Enter / add button |
| `number_input` | as `input`, plus `min?` / `max?` | as `input` |
| `textarea` | as `input`, multiline, `rows?` | on submit / add |
| `select` | `options`, `action`, `placeholder?` | on pick |
| `date` | `action` | on pick |
| `segmented` | `options`, `action` | on pick |
| `toggle` | `value`, `action`, `label?` | on flip |
| `checklist` | `items: [{key, label?, icon?}]` | toggles the tapped item's key |
| `stepper` | `value`, `action` (`increment`), `label?`, `min?` / `max?` | on +/− |
| `slider` | `min`, `max`, `step?`, `value`, `action`, `label?` | on release |
| `rating` | `max?`, `value`, `action` | on tap |
| `search` | `target` (a repeater/table id), `fields?`, `placeholder?` | filters client-side |
| `photo` | `action` (`vision`) | captures an image → a new row |

`options` (on `select` / `segmented`) is an array of strings, or of
`{value, label}` objects. Interactive controls reflect a tap **optimistically**
(the UI updates the instant you tap and reconciles when the authoritative value
lands), so nothing feels laggy on a slow link.

### Composites (3)

Catalog-3 semantic components. The agent states intent; the system expands each
to primitives before rendering, and owns the layout and wiring. Old clients
render the expansion unchanged.

**`list_row`** — a repeater's `item`. Every prop but `title` is optional; a bare
`$.field` interpolates, a `{$.field} unit` template is kept, a plain word is a
literal.

```json
{ "type": "list_row",
  "thumb": "$.receipt", "title": "$.title", "subtitle": "$.merchant",
  "badge": "$.category", "value": "{$.amount} ₺" }
```

**`form`** — a multi-field entry card that adds a row into a list. Each `field`
names a field of the `into` list; the control type is derived (string→input,
number→number_input, date→date, bool→toggle, `options`→a picker). A date
defaulting to `"today"` pre-fills today. Submit adds the row and clears the form.

```json
{ "type": "form", "id": "addExpense", "into": "expenses", "title": "Add expense",
  "fields": [
    { "field": "title", "label": "What" },
    { "field": "amount", "label": "Amount" },
    { "field": "category", "options": ["Food", "Bills", "Other"] },
    { "field": "date", "default": "today" } ],
  "submit": { "label": "Add" } }
```

**`tracker_card`** — a headline metric + a list-backed chart. `field` (a number
field) and `agg` (default `sum`) drive the metric; `chart`
(`bar`/`line`/`area`/`pie`/`donut`) draws the same data; `window` scopes both.
Pie/donut groups by a category-like field (or an explicit `by`).

```json
{ "type": "tracker_card", "id": "spend", "list": "expenses", "field": "amount",
  "title": "This month", "window": "30d", "chart": "bar" }
```

### Icons

An `icon` (on `icon` / `icon_button`, and `checklist` items) is a name from this
vetted set — an unknown name degrades to a sensible fallback, so these always
render:

`activity` · `arrow-down` · `arrow-up` · `bed` · `bell` · `book` · `brain` ·
`calendar` · `camera` · `cart` · `check` · `clock` · `cloud` · `coffee` · `cup` ·
`droplet` · `dumbbell` · `flame` · `heart` · `leaf` · `mail` · `minus` · `moon` ·
`music` · `pen` · `phone` · `pill` · `plus` · `run` · `smile` · `sparkles` ·
`star` · `sun` · `target` · `trash` · `trophy` · `undo` · `walk` · `wallet` ·
`zap`

## Actions

Every interactive component declares one `action` object with an `op`. Any action
may also carry `"once": "day" | "week" | true` — a server-side latch that lets
the action fire at most once in that window; a repeat tap is a silent no-op.

| Op | Shape | Effect |
|----|-------|--------|
| `set` | `{op, key}` | Set a state key to the supplied value. |
| `increment` / `decrement` | `{op, key, by?}` | Step a number by `by` (default 1). A stepper supplies only the sign. |
| `toggle` | `{op, key}` | Flip a bool. |
| `log` | `{op, series, value?, category?}` | Append an event. |
| `remove_last` | `{op, series}` | Undo the last logged event. |
| `reset` | `{op, key}` or `{op, series}` | Reset a key to default / clear a series. |
| `timer_toggle` | `{op, key}` | Start/pause a `timer` state key. |
| `item_add` | `{op, key, fields?}` or `{op, key, item?}` | Add a row. `fields` = a `{field: template}` map; `item` = a fixed `{field: value}` map. |
| `item_update` | `{op, key, field}` or `{op, key, fields}` | Edit one field (or several) of the tapped row. |
| `item_toggle` | `{op, key, field}` | Flip a bool field of the tapped row. |
| `item_remove` | `{op, key}` | Delete the tapped row. |
| `item_move` | `{op, key}` | Reorder the tapped row. |
| `batch` | `{op, ops: [...]}` | Up to 20 ops at once; not nested. |
| `vision` | `{op, prompt, into}` | Read a captured photo into a new list row. |
| `agent` | `{op, message}` | Hand a `message` (≤ 2000 chars, templated with `{value}`/`{key}`) to the model — the only op that reaches it. |

### `item_add` field templates

`item_add` with a `fields` map builds one row from several inputs. `{value}` is
the tapped input's value; `{state_key}` pulls a live value; a lone `{token}`
keeps its type (a number stays numeric); `today` on a date field becomes the
current date. (A `form` composite generates all of this for you.)

## Dynamic lists

A `list` state key holds living rows; a `repeater` renders one `item` template
per row.

```json
{ "type": "repeater", "source": "tasks", "empty": "No tasks yet",
  "navigate": "taskDetail",
  "item": { "type": "list_row", "title": "$.title" } }
```

| Prop | Purpose |
|------|---------|
| `source` | the `list` state key (required) |
| `item` | the row template — a `list_row`, or any component tree (required) |
| `empty` | text shown when the list is empty |
| `navigate` | a `screens` id — tapping a row opens that detail screen, scoped to the row (screens are one level deep — a screen can't itself navigate) |
| `where` | a per-row filter expression (over the row's fields) |
| `sortBy` | `{field, dir}` — `dir` is `asc` / `desc` |

Inside the `item`, `$.field` binds a prop to the current row and `{$.field}`
interpolates it. Row actions — `item_update` / `item_toggle` / `item_remove` /
`item_move` — live inside the template; `item_add` can sit anywhere. Every
user-owned list row is guaranteed editable: if you author no detail screen, one
with the right edit control per field is synthesized. Rows are swipe-to-delete.

## Tables

A `table` is either **static rows** or a **data-bound** list.

```json
{ "type": "table", "source": "prs",
  "columns": [ { "field": "title", "label": "Title" },
               { "field": "n", "label": "#", "align": "right", "width": 48 } ],
  "sortBy": { "field": "n", "dir": "desc" }, "navigate": "prDetail" }
```

- **Static**: `rows` — an array of cell arrays.
- **Data-bound**: `source` (a `list` key) + `columns` (≤ 6), each
  `{field, label?, align?, width?}` (`align` = `left`/`center`/`right`). Optional
  `sortBy: {field, dir}`, `where`, `navigate`, `empty`. Tapping a header re-sorts
  client-side.

## Charts

A `chart`'s `data` object has one of these shapes; `kind` picks the drawing.

| Shape | `data` | `kind` |
|-------|--------|--------|
| Single time series | `{series, agg?, bucket?, window?}` | `bar` / `line` / `area` |
| Multi-series overlay | `{series: [{key, label?, color?}], stacked?}` | `bar` / `line` / `area` |
| Category breakdown | `{series, by: "category", agg?}` | `pie` (+ `donut: true`) |
| List-backed time | `{list, field?, date?, bucket?, window?}` | `bar` / `line` / `area` |
| List-backed category | `{list, by: "<field>", field?, agg?}` | `pie` / `donut` |
| Scatter | `{list, x, y}` | `scatter` |

- `bucket` ∈ `hour` `day` `week`; `window` ∈ `today` `7d` `30d` `90d` `all`.
- A **category** breakdown's `agg` is `sum` or `count` only.
- A **list-backed** chart aggregates the list's rows directly, so it stays
  correct through every add, edit, delete and photo capture — no parallel series
  to drift. Prefer it (or a `tracker_card`) whenever the data lives in a list.
- `sparkline` and `heatmap` take the single-series `data` shape.

## Expressions

Used in `computed.expr`, `visibleWhen`, `cases[].when`, and a list `where`. A
tiny, safe grammar — no attribute access, no calls outside the whitelist, no
names outside the resolved values.

- **Operators**: `+ - * / % ** //`, comparisons `< <= > >= == !=`, and
  `and` / `or` / `not`. Comparisons evaluate to `1.0` / `0.0`.
- **Functions**: `min` `max` `abs` `round` `floor` `ceil`; and date helpers
  `now()`, `weekday()` (0=Mon), `days_until("YYYY-MM-DD")`,
  `days_since("YYYY-MM-DD")` (a literal or a date key).
- **Names** resolve against state + computed keys; inside a list `where`, against
  the row's fields.

## Interpolation & binding

| Form | Where | Resolves to |
|------|-------|-------------|
| `{key}` | any display `text`/`label` | the value of a state/computed key |
| `$.field` | a prop inside a repeater `item` | the current row's field (literal) |
| `{$.field}` | text inside a repeater `item` | the current row's field, formatted |

## Watches

`watches` is an **array** of reactive reminders the flowlet evaluates itself —
no scheduled model turn. Each rule has a stable `id`, a `trigger` (one of four
kinds), a `notify`, and an optional cooldown.

```json
"watches": [ {
  "id": "drink_reminder", "trigger": "condition",
  "when": "today_ml < goal_ml", "after": "18:00",
  "cooldownMinutes": 120,
  "notify": { "title": "Water", "body": "{today_ml}/{goal_ml} ml today" }
} ]
```

| `trigger` | Fires when | Fields |
|-----------|-----------|--------|
| `schedule` | a time of day / an interval / given days | `at: "21:00"`, `everyMinutes`, `days: ["mon",…]` |
| `condition` | a `when` expression goes true (edge-triggered) | `when`, optional `after: "HH:MM"` (only fire past that time of day) |
| `goal` | a `when` first becomes true (once) | `when` |
| `stale` | nothing changed for a while | `idleMinutes` |

- `notify` = `{ title, body?, compose? }`. Set `compose: true` to have the agent
  write the reminder text from the live numbers at fire time.
- `cooldownMinutes` (optional) throttles re-fires; `once: true` (optional) fires
  the whole watch at most once ever; `days: [...]` may narrow any trigger.
- `also` = `{ op: "agent", message }` — the only side-effect a watch may trigger
  beyond a notification; runs an agent turn on fire (heavily throttled).

## Sources

`sources` is an **object** `{name: {…}}` binding a `list` to live external data.
The agent fetches on a schedule (or on pull-to-refresh) and writes the result
into the list.

```json
"sources": {
  "commits": { "kind": "agent",
               "prompt": "the last 10 commits to {repo} in the past hour",
               "into": "commits", "refresh": "1h", "limit": 10 }
}
```

- `kind` — currently `"agent"`: a normal turn of yours (with your tools) runs as
  a self-prompt on a schedule, isolated from chat.
- `prompt` — what to fetch, templated with `{key}` live values.
- `into` — the source-owned `list`. A source-owned list is **read-only** in the
  UI (the snapshot is replaced each refresh, not edited).
- `refresh?` — a cadence like `"15m"` / `"1h"` (omit for manual / pull-to-refresh);
  minimum 10 minutes.
- `limit?` — cap the rows fetched. `prompt` is capped at 1000 characters.

## Serve-time guarantees

Some correctness is filled in by the system when a flowlet is served, never left
to whether the agent remembered it. These transforms are deterministic,
idempotent, and never change what's stored:

- **Forgotten ids are assigned** — a control that needs an id gets a
  deterministic one, so nothing is rejected and no chart silently renders empty.
- **Composites expand** to primitives, so every client (old or new) renders the
  same layout.
- **Every user-owned list row is editable** — if the agent authored no edit
  screen, one is synthesized with the right controls per field.
- **Every stored photo is shown** — an `image` field gets a row thumbnail and a
  full photo on its detail screen.
- **A chart-bearing multi-column grid is forced full-width**, because charts
  don't fit side by side on a phone.

## The `flowlet` tool

The agent authors and maintains flowlets through one tool. You never call it —
you ask in plain language — but these are the operations behind the scenes:

| Action | Does |
|--------|------|
| `create` | Build a flowlet from a `definition`. |
| `update` | Replace a flowlet's `definition` (versioned), or set `pinned`. |
| `get` | The definition **and current live values** (answers "how much today?"). |
| `list` | Every flowlet with its live values. |
| `delete` | Remove a flowlet. |
| `log` | Append an event to a series (from chat: "I drank 500 ml"). |
| `set_state` | Change a state value ("make my goal 3 liters"). |
| `query` | One aggregated number from a series, without dumping the flowlet. |
| `notify` | Fire a one-off reminder now (recurring/conditional → use a `watch`). |

On `create`/`update` the tool returns a **review** — deterministic lint findings
plus a preview resolved against sample rows — which the agent reads and acts on,
so a generated flowlet tends to come out right the first time.

## A complete example

A daily-habit tracker — checklist, a once-per-day "complete" button, a weekly
total, a chart, and a 9pm reminder — showing how the pieces fit:

```json
{
  "catalog": 3, "name": "Habits", "icon": "check", "accent": "#7C6FF0",
  "state": {
    "read_done": { "type": "bool", "default": false },
    "code_done": { "type": "bool", "default": false }
  },
  "series": { "days": {} },
  "computed": {
    "both_done":    { "expr": "read_done > 0 and code_done > 0" },
    "weekly_total": { "series": "days", "agg": "sum", "window": "7d" }
  },
  "layout": [
    { "id": "habits", "type": "checklist", "items": [
      { "key": "read_done", "label": "Read 1 hour", "icon": "book" },
      { "key": "code_done", "label": "Code 5 hours", "icon": "zap" } ] },
    { "id": "complete_day", "type": "button", "text": "Complete the day",
      "style": "primary", "visibleWhen": "both_done > 0",
      "action": { "op": "batch", "once": "day", "ops": [
        { "op": "log", "series": "days", "value": 1 },
        { "op": "reset", "key": "read_done" },
        { "op": "reset", "key": "code_done" } ] } },
    { "type": "metric", "value": "weekly_total", "label": "This week" },
    { "id": "trend", "type": "chart", "kind": "bar",
      "data": { "series": "days", "agg": "sum", "bucket": "day", "window": "7d" } }
  ],
  "watches": [ {
    "id": "evening_nudge", "trigger": "schedule", "at": "21:00",
    "notify": { "title": "Habits", "body": "Did you read and code today?" }
  } ]
}
```

## Limits

| Bound | Value |
|-------|-------|
| Definition size | 64 KB |
| Components | 200 |
| Nesting depth | 8 |
| State keys / computed | 50 each |
| Series | 20 |
| List rows | 200 |
| Item fields | 8 |
| Drill screens | 6 |
| Chart series (overlay) | 4 · pie slices 8 |
| Table columns | 6 |
| Watches | 20 · Sources 8 |
| Photo captures | rate-limited per flowlet + globally |
| `agent` taps | rate-limited per flowlet + globally |

## See also

- [Flowlets feature guide](../features/flowlets.md) — concepts and how to ask.
- [Tools reference](tools.md) — the `flowlet` tool the agent uses to author them.
- [Cron](../features/cron.md) — scheduled reminders not tied to a flowlet.
