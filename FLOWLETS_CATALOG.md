# Flowlets — Component Catalog (v1)

The single reference for the flowlet component catalog, shared by the bot
validator (`flowly/flowlets/`), the Desktop renderer (React), and the iOS
renderer (SwiftUI). All three MUST agree; the golden fixtures in
`tests/fixtures/flowlets/` are the conformance set every renderer is checked
against.

A flowlet is a JSON **definition** (a component tree) the agent authors against
this catalog. The bot resolves a flat **values** map on every change; renderers
are pure functions of `(definition, values)` — they substitute and draw, never
aggregate. `catalog: 1` is required at the top of every definition.

## Top-level shape

```json
{
  "catalog": 1,
  "name": "Water",
  "icon": "droplet",
  "accent": "#00A6C8",
  "state":   { "goal_ml": { "type": "number", "default": 2000, "min": 250, "max": 10000 } },
  "series":  { "water": { "unit": "ml" } },
  "computed": {
    "today_ml":  { "series": "water", "agg": "sum", "window": "today" },
    "remaining": { "expr": "max(0, goal_ml - today_ml)" }
  },
  "layout": [ ... components ... ]
}
```

- **state** — mutable values. `type` ∈ `number | bool | string | timer | list`;
  `number` takes `default`/`min`/`max`, `string` takes `default`/`maxLength`
  (≤ 500). `timer` is bot-managed (`timer_toggle` drives it; resolves to
  `{running, elapsed}`). `list` is a dynamic collection — it declares an
  `item` field schema (`{"title": "string", "done": "bool"}`; types
  `string|number|bool|date`, ≤ 8 fields, `id` reserved) plus optional `max`
  (≤ 200) and resolves to an array of `{id, ...fields}` rows, rendered by the
  `repeater` component and mutated by the `item_*` ops.
- **series** — append-only event logs (`{unit?}`). Charts and totals read these.
- **computed** — derived scalars, one of: a series aggregation
  (`{series, agg, window}`); a **list** aggregation (`{list, agg, field?, where?}`
  → count/sum/avg/min/max over a dynamic list, `where` an expr over item fields);
  a safe arithmetic **`expr`**; or conditional text **`cases`**. Resolved in
  dependency order (declaration order doesn't matter).
- **sources** — live/external data bindings (optional). Each
  `{kind:"agent", prompt, into, refresh?, limit?}` refreshes a **source-owned**
  state key (declared with `"source": true`) on a schedule: an agent turn
  fetches the data with its tools and returns JSON matching the target's schema
  (a `list`'s item fields, or a scalar). `refresh` is `"manual"` or `"15m"`/
  `"1h"` (min 10 m; throttled + backoff, keeps stale data on failure). A
  source key is read-only to the user (no `set`/`item_*` on it). Privilege = a
  cron self-prompt; shown transparently in the screen's "Data sources" section.
  (`tool`/`device` kinds — LLM-free / HealthKit — arrive in a later phase.)
- **layout** — the component tree.

**aggregations** (`agg`): `sum · count · avg · min · max · last`
**windows**: `today · 7d · 30d · 90d · all` (local timezone; `today` rolls over
at the user's midnight — no reset job).
**buckets** (chart `data.bucket`): `hour · day · week`

`expr` grammar is `+ - * / % ** //`, comparisons `< <= > >= == !=` (chained like
Python), `and / or / not`, numeric literals, scalar key names, the functions
`min max abs round floor ceil`, and the date functions `now() weekday()
days_until(d) days_since(d)` (`d` = a `"YYYY-MM-DD"` string literal or a key;
day math is a DST-free calendar-day count, identical on bot/desktop/iOS). String
literals are legal only as a date-fn argument. No attribute access, indexing, or
other calls — anything else is rejected at author time.

## Values & interpolation

Renderers receive `values`: a flat map of scalar keys (state + computed) plus,
for each chart/sparkline/heatmap, its `id` → its resolved data (single series →
`[{t,v}]`; multi → `{multi:[{k,points}]}`; categorical → `[{k,v}]`; a scatter
chart writes nothing — it reads its list directly). A label
interpolates any scalar with `{key}` (e.g. `"{today_ml} / {goal_ml} ml"`).
Whole numbers arrive as integers (rendered `750`, not `750.0`); locale
formatting (separators/units) is the client's job.

## Components

Common props: `id` (required on anything with an `action` or a chart), `type`.
A `value`/`max`/`min` prop is either a number literal or the name of a
scalar key. Unknown component types render a neutral placeholder.

### Layout (8)
| type | notes |
|---|---|
| `card` | rounded container; `children` |
| `row` | horizontal; `children` |
| `column` | vertical; `children` |
| `grid` | `columns` (default 2); `children` |
| `list` | stacked rows; `children` |
| `divider` | hairline |
| `spacer` | `size` px |
| `repeater` | one row per item of a `list` state key: `source`, `item` (row template — `$.field` binds, `{$.field}` interpolates), `empty?` |

### Display (14)
| type | key props |
|---|---|
| `header` | `text`, `subtitle?` |
| `text` | `text` (interpolates) |
| `badge` | `text` |
| `icon` | `name` (see icon list) |
| `stat` | `value` (bind) or `text`, `label?` |
| `progress` | `value`, `max` (binds), `label?` |
| `ring` | `value`, `max`, `label?` |
| `gauge` | `value`, `min`, `max`, `label?` |
| `chart` | `data` + `kind` (`bar`/`line`/`area`/`pie`/`scatter`) |
| `sparkline` | `data` (single series only) |
| `heatmap` | `data` (single series only) |
| `table` | static `rows`, **or** data-bound `source` (a list key) + `columns` |
| `clock` | `seconds?` |
| `countdown` | `target` (epoch-ms or ISO), `label?` |

A chart's `data` takes **one of four forms** (detected by shape; `pie`/`scatter`/
multi are `chart`-only):

| form | `data` | resolves to |
|---|---|---|
| single time series | `{ series:"k", agg?, bucket?, window? }` | `[{t,v}]` |
| multi-series overlay | `{ series:[{key,label?,color?}], agg?, bucket?, window?, stacked? }` (2–4) | `{multi:[{k,points:[{t,v}]}]}` |
| categorical pie/donut | `{ series:"k", by:"category", agg:sum\|count, window?, donut? }` | `[{k,v}]` (top 8 + "other") |
| list scatter | `{ list:"k", x, y }` (`x`/`y` = number item fields) | client reads the list rows |

- **Categories** come from an event's `category`, set on the `log` op:
  `{op:"log", series:"spend", value:"…", category:"food"}` (a literal or a
  `"{token}"` templated from live values). `stacked:true` is bar-only.
- **Colours** — a fixed 8-hex palette (`#8b5cf6 #22c55e #f59e0b #ef4444 #3b82f6
  #ec4899 #14b8a6 #a3a3a3`), same on every platform; series/slice 0 uses the
  flowlet accent, an explicit `color` on a series overrides.

**Data-bound `table`** — instead of static `rows`, bind to a list:
`{ "source": "<list key>", "columns": [{"field","label?","align?","width?"}],
"sortBy?": {"field","dir":"asc|desc"}, "empty?": "…" }` (1–6 columns; each
`field` must exist in the list's item schema). One row per item; tapping a
header sorts (client-local). Sort is deterministic + locale-independent
(numbers numerically, strings case-folded, missing last) — identical on every
platform.

### Input (13) — carry an `action`, need an `id`
| type | key props | typical action |
|---|---|---|
| `button` | `text`, `style` (`primary`/`secondary`/`ghost`/`destructive`) | any op |
| `icon_button` | `icon` | any op |
| `stepper` | `value` (bind), `min?`, `max?` | `increment` (−/+ pass ∓1) |
| `slider` | `min`, `max`, `step?`, `value` (bind), `label?` | `set` |
| `toggle` | `label?` | `toggle` |
| `checklist` | `items: [{key,label,icon?}]` | toggles each item's bool state key (no single action) |
| `segmented` | `options: [str \| {value,label}]` | `set` |
| `input` | `label?`, `maxLength?` | `set` |
| `number_input` | `label?`, `min?`, `max?` | `set` |
| `rating` | `max` (default 5), `value?` (bind) | `log` or `set` |
| `select` | `options: [str \| {value,label}]`, `label?` | `set` (use over segmented for many options) |
| `date` | `label?` | `set` (stores `YYYY-MM-DD`) |
| `textarea` | `label?`, `maxLength?`, `rows?` | `set` |

### Display v2 — structured / professional (9)
| type | key props |
|---|---|
| `metric` | `value` (bind), `unit?`, `label?`, `delta?` (bind), `deltaLabel?`, `invert?` |
| `status` | `text`, `tone` (`ok`/`warn`/`bad`/`neutral`) |
| `keyvalue` | `rows: [{label, value}]` (value interpolates `{key}`) |
| `timeline` | `events: [{title, time?, tone?}]` (tone: `done`/`now`/`wait`) |
| `callout` | `text`, `tone` (`info`/`success`/`warn`/`bad`), `icon?` |
| `code` | `text`, `language?` |
| `link` | `text`, `url` (http/https) |
| `image` | `src` (http/https/data), `alt?`, `height?` |
| `timer` | `value` (a `timer` state key), `label?` · action `timer_toggle` |

**Timer** — declare a state key of `type:"timer"` and a `timer` component bound
to it with `action:{op:"timer_toggle", key}`. `resolve_values` exposes it as
`{running, elapsed}` (seconds); a running timer ticks live and the accumulated
time persists across sessions. For billable hours, an experiment, a workout.

## Actions (declared; applied deterministically on the bot — no LLM)

| op | fields | effect |
|---|---|---|
| `set` | `key` | write the component's value to a state key |
| `increment` / `decrement` | `key`, `by?` | ± `by` (default 1); a stepper passes ∓1 direction |
| `toggle` | `key` | flip a bool state key |
| `log` | `series`, `value?` | append an event (fixed `value` on a button; passed value for a rating) |
| `remove_last` | `series` | undo the last event |
| `reset` | `key?` / `series?` | reset a state key or clear a series |
| `agent` | `message` | run a normal agent turn; `message` templates `{value}` (typed/tapped value, ≤500 chars) + live `{key}`s — free text can reach the model |
| `batch` | `ops: [...]` | apply several ops in order (no nesting) |
| `item_add` | `key`, `item?` | append a row to a `list` (fixed fields + client value; bare string → the single string field) |
| `item_update` | `key`, `field` / `fields` | set a row field from the control, or fixed `fields` |
| `item_toggle` | `key`, `field` | flip a bool field on the tapped row |
| `item_remove` | `key` | delete the tapped row |
| `item_move` | `key` | reorder (value = new index) |

Row-scoped ops (`item_update/toggle/remove/move`) must sit inside the repeater
bound to the same list; the client sends their value as `{"itemId", "value"}`
(the bot unwraps that envelope for every other op, so any component works
inside a row template).

A button with a fixed `value` ignores any client-sent value. Free inputs
(slider/input/number_input/rating) supply their value, validated to the
component's and the state key's bounds.

## Adaptive screens — `visibleWhen` + conditional text

**`visibleWhen`** (any component): a boolean expression over declared
state/computed keys, evaluated client-side against live values — falsy hides
the node, any evaluation error fails open (shows it). Same safe grammar as
watch `when` (arith, `< <= > >= == !=`, `and/or/not`, `min/max/abs/round`).
Validated at author time (grammar + key existence).

**Conditional text** — a third `computed` form resolving to a *string*:

```json
"statusText": { "cases": [{ "when": "expr", "text": "… {key} …" }], "else": "…" }
```

First truthy `when` wins; `text`/`else` template `{key}` server-side. Consumed
like any scalar (`"text": "{statusText}"`). Unresolvable → `""`.

## Watches (reactive reminders — evaluated LLM-free)

A top-level `watches: [...]` array turns a flowlet from a passive screen into a
proactive one. Each rule is evaluated deterministically by the bot — on a 60s
heartbeat and immediately after any state change — and pushes a reminder
(APNs/FCM on mobile, native notification on desktop) when it fires. No cron job,
no model call.

| trigger | fires when | key fields |
|---|---|---|
| `schedule` | a time/interval arrives | `at:"HH:MM"` or `everyMinutes`, opt. `days:[...]` |
| `condition` | `when` expr flips false→true (edge) | `when`, opt. `after:"HH:MM"` |
| `goal` | `when` target reached (edge) | `when`, opt. `once` |
| `stale` | no activity for N minutes | `idleMinutes` |

Every watch: `id` (stable) + `notify:{title, body, compose?}`. With
`compose: true` the agent writes the notification at fire time (live data in
its prompt, sent via the flowlet notify action, ≥30-min throttle; the static
title/body are the fallback). `title`/`body` template
current values with `{key}`. `when` uses the safe expr grammar — arithmetic
`+ - * / % min max abs round`, comparisons `< <= > >= == !=`, and `and/or/not`
over declared state/computed keys. Fires are **edge-triggered** and
cooldown-gated (defaults: condition 6h, goal 12h, stale 12h; override with
`cooldownMinutes`). Optional `also:{op:"agent", message}` wakes the agent
(throttled ≥30 min) for reminders that must *do* something.

```json
"watches": [
  { "id": "nudge", "trigger": "condition", "when": "today_ml < goal_ml",
    "after": "18:00", "notify": { "title": "Water", "body": "{today_ml}/{goal_ml} ml" } },
  { "id": "win", "trigger": "goal", "when": "today_ml >= goal_ml", "once": true,
    "notify": { "title": "Goal 🎉" } }
]
```

Limits: ≤ 20 watches per flowlet · notify.body ≤ 500 chars · also.message ≤ 300 chars.

## Icons

Platform-neutral names mapped to SF Symbols (iOS) / lucide (Desktop); unknown →
a neutral dot:

`droplet flame check heart star moon sun pill book dumbbell coffee leaf bell
clock calendar target trophy zap smile cloud cup wallet cart run walk bed brain
music camera phone mail pen trash undo plus minus arrow-up arrow-down sparkles
activity`

## Limits

definition ≤ 64 KB · ≤ 200 components · nesting depth ≤ 8 · ≤ 200 list items ·
≤ 8 item fields · ≤ 50 computed keys ·
≤ 50 state keys · ≤ 20 series · input value ≤ 500 chars.

## Sync surface

`flowlets.list · flowlets.get · flowlets.state · flowlets.action ·
flowlets.refresh · flowlets.pin · flowlets.delete` over feature_rpc (gateway +
relay). `flowlets.get` also kicks a background refresh of due data sources;
`flowlets.refresh` force-refreshes them (pull-to-refresh). Events:
`flowlet.created · flowlet.updated · flowlet.deleted · flowlet.state ·
flowlet.reminder` (a watch fired → desktop notification). Creation/definition
edits are agent-only (via the `flowlet` tool); reactive `watches` fire on the
bot's 60s heartbeat + on every state change.
