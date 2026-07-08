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

- **state** — mutable values. `type` ∈ `number | bool | string`; `number` takes
  `default`/`min`/`max`, `string` takes `default`/`maxLength` (≤ 500).
- **series** — append-only event logs (`{unit?}`). Charts and totals read these.
- **computed** — derived scalars. Either a series aggregation
  (`{series, agg, window}`) or a safe arithmetic `expr` over other scalar keys.
  Resolved in dependency order (declaration order doesn't matter).
- **layout** — the component tree.

**aggregations** (`agg`): `sum · count · avg · min · max · last`
**windows**: `today · 7d · 30d · 90d · all` (local timezone; `today` rolls over
at the user's midnight — no reset job).
**buckets** (chart `data.bucket`): `hour · day · week`

`expr` grammar is `+ - * / % ** //`, numeric literals, scalar key names, and the
functions `min max abs round floor ceil`. No attribute access, indexing, or
other calls — anything else is rejected at author time.

## Values & interpolation

Renderers receive `values`: a flat map of scalar keys (state + computed) plus,
for each chart/sparkline/heatmap, its `id` → `[{t, v}]` buckets. A label
interpolates any scalar with `{key}` (e.g. `"{today_ml} / {goal_ml} ml"`).
Whole numbers arrive as integers (rendered `750`, not `750.0`); locale
formatting (separators/units) is the client's job.

## Components

Common props: `id` (required on anything with an `action` or a chart), `type`.
A `value`/`max`/`min` prop is either a number literal or the name of a
scalar key. Unknown component types render a neutral placeholder.

### Layout (7)
| type | notes |
|---|---|
| `card` | rounded container; `children` |
| `row` | horizontal; `children` |
| `column` | vertical; `children` |
| `grid` | `columns` (default 2); `children` |
| `list` | stacked rows; `children` |
| `divider` | hairline |
| `spacer` | `size` px |

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
| `chart` | `data` + `kind` (`bar`/`line`/`area`) |
| `sparkline` | `data` |
| `heatmap` | `data` |
| `table` | `rows` (array of rows) |
| `clock` | `seconds?` |
| `countdown` | `target` (epoch-ms or ISO), `label?` |

`data` = `{ series, agg, bucket, window }`.

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
| `agent` | `message` | run a normal agent turn (e.g. "analyze my week"); the reply lands in chat |
| `batch` | `ops: [...]` | apply several ops in order (no nesting) |

A button with a fixed `value` ignores any client-sent value. Free inputs
(slider/input/number_input/rating) supply their value, validated to the
component's and the state key's bounds.

## Icons

Platform-neutral names mapped to SF Symbols (iOS) / lucide (Desktop); unknown →
a neutral dot:

`droplet flame check heart star moon sun pill book dumbbell coffee leaf bell
clock calendar target trophy zap smile cloud cup wallet cart run walk bed brain
music camera phone mail pen trash undo plus minus arrow-up arrow-down sparkles
activity`

## Limits

definition ≤ 64 KB · ≤ 200 components · nesting depth ≤ 8 · ≤ 50 computed keys ·
≤ 50 state keys · ≤ 20 series · input value ≤ 500 chars.

## Sync surface

`flowlets.list · flowlets.get · flowlets.state · flowlets.action · flowlets.pin
· flowlets.delete` over feature_rpc (gateway + relay). Events:
`flowlet.created · flowlet.updated · flowlet.deleted · flowlet.state`.
Creation/definition edits are agent-only (via the `flowlet` tool).
