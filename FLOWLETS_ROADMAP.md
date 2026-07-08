# Flowlets — Roadmap & Design (v2)

Status of the base system (shipped on `feat/flowlets` across bot / desktop / iOS):
a personal, persistent mini-screen the agent authors from a versioned catalog,
rendered natively (React + SwiftUI), state owned by the bot, deterministic
LLM-free actions, cross-device sync, and reactive **watches** (self-firing
reminders). Base is solid but **narrow**: great for numeric/boolean personal
trackers, weak on dynamic data, adaptive layout, and "feels like a real app".

This doc is the plan to close those gaps. **Part B (native app feel) is the
one we build now**; Part A is the capability roadmap, sequenced by
value-to-effort.

---

## Where we are — honest capability map

**Great at:** counters/streaks, time-series logging + aggregation + charts,
goals with visual feedback, deterministic taps, reactive reminders, live sync.

**Can't do today (the ceiling):**

| Gap | Exposed by | Root cause |
|---|---|---|
| Dynamic lists (add/remove items at runtime) | todo, shopping, tasks, project | state is flat `key→scalar`; `checklist` items are fixed at author time |
| Text-entry log (append + list timestamped notes) | journal, gratitude, CRM notes | series values are numeric (`REAL`) |
| Edit/delete an arbitrary past entry | "fix Tuesday's weight" | only `remove_last` |
| Conditional visibility (show/hide by state) | "warn only when over budget" | layout is static |
| Conditional/computed text | "Ahead" vs "Behind" | labels only interpolate `{key}` numbers |
| Multi-series / categorical charts (overlay, pie, stacked) | spend-by-category, weight-vs-goal | chart = single-series aggregation |
| External data (HealthKit, weather, calendar) | steps, sleep, live dashboards | no source binding; only user/agent-logged data |
| Cross-flowlet reference / unified dashboard | "behind on water AND a workout day" | computed/watch see only their own flowlet |
| Notification action buttons | log/snooze from the reminder itself | notify is title+body only |
| Multi-screen / tabs / wizard | drill-down (workout → sets → reps) | one screen per flowlet |

**UX risks:** quality is only as good as the agent's authoring (it fumbles
validation, reaches for the wrong tool); static layout makes screens look
"dumb"; every edit round-trips through chat; no discovery/template gallery; and
— the immediate one — **it doesn't feel like an app** (modal on desktop, sheet
on iOS).

---

## Part A — Capability roadmap (design, sequenced)

### A1. Conditional visibility — `visibleWhen` (cheap, highest ratio)
Any component node may carry `"visibleWhen": "<expr>"`. Evaluated against live
`values`; a falsy result renders nothing. Enables adaptive screens (warnings,
empty-section hiding, mode toggles).

- **Bot:** schema validates the expr with the existing safe grammar (names ⊆
  scalar/computed keys). No resolve change — visibility is client-side because
  it depends on live values the renderer already holds.
- **Clients:** each needs a tiny expr evaluator mirroring the bot grammar
  (arith + `< <= > >= == !=` + `and/or/not`, names from `values`). ~120 lines
  TS + ~120 lines Swift, unit-tested against the same cases as the bot.
- **Catalog:** bump; old clients ignore `visibleWhen` (fail-open → always show),
  which is safe.

### A2. Conditional / computed text (cheap)
Extend `computed` with a string-producing form:
```json
"statusText": { "cases": [{ "when": "today_ml >= goal_ml", "text": "Ahead 🎉" }],
                "else": "Keep going" }
```
Resolves to a string in `values`, consumed via `{statusText}`. **Server-side
only** (no client change) — the bot already resolves `computed`. Pairs with A1.

### A3. Dynamic collections — `list` state + `repeater` (transformative)
The big unlock: todo / shopping / tasks / journal.

- **State type `list`** with a declared item schema:
  ```json
  "tasks": { "type": "list",
             "item": { "title": "string", "done": "bool", "due": "date" },
             "max": 100 }
  ```
  Stored as a JSON array of `{id, ...fields}` in `flowlet_state` (already JSON).
- **Actions:** `item_add {key, values}`, `item_update {key, itemId, values}`,
  `item_remove {key, itemId}`, `item_toggle {key, itemId, field}`, optional
  `item_move {key, itemId, toIndex}`. All deterministic (no LLM).
- **Component `repeater`:** renders a template once per item:
  ```json
  { "type": "repeater", "source": "tasks", "empty": "No tasks yet",
    "item": { "type": "row", "children": [
      { "type": "toggle", "value": "$.done", "action": { "op": "item_toggle", "field": "done" } },
      { "type": "text", "text": "{$.title}" },
      { "type": "icon_button", "icon": "trash", "action": { "op": "item_remove" } } ] } }
  ```
  `$.field` binds to the current item; item actions carry the `itemId`
  automatically. An `input`/`textarea` + `item_add` button forms the "add row".
- **Text journal = a list of `{text, ts}`** rendered by a repeater/timeline;
  A4 folds into A3 (no separate mechanism).
- **Scope of work:** bot (state type, 4–5 ops, schema, resolve of `$.` binds,
  limits), both renderers (repeater + item-scoped actions), sync. Largest item;
  do after Part B + A1/A2.

### A4. (folded into A3)

### A5. Richer charts — multi-series, categorical, pie (medium-deep)
- Events gain an optional `category` (via `meta.category`); a group-by
  aggregation feeds a `pie`/`donut` and stacked bars.
- Chart `data` accepts a `series: [...]` array for overlay (weight vs goal line).
- Renderer work on both platforms (Swift Charts handles most; the desktop SVG
  charts need multi-series + pie paths).

### A6. Notification action buttons (medium)
`notify.actions: [{ label, op }]` → the push shows buttons that apply a
deterministic op (e.g. "Log 250 ml", "Snooze") without opening the app. iOS via
`UNNotificationCategory`/actions; desktop via `Notification` actions; the action
callback routes to `apply_action` on the bot. Closes the reminder → one-tap-log
loop.

### A7. External data sources (deep — later)
Two tiers: (a) **works today**, coarse — a cron+`agent` job logs into a series
(steps, weather); (b) **new infra** — a `source` binding on a series the client
fills from a device API (HealthKit steps/sleep/heart) and syncs up. Ship (a) as
a recipe now; design (b) later.

### A8. Cross-flowlet / unified dashboard (deep — later)
Allow a computed/watch to reference another flowlet's resolved values
(namespaced, e.g. `water.today_ml`). Enables a roll-up dashboard flowlet and
cross-tracker conditions. Requires a resolve-time dependency graph; defer.

**Suggested order:** Part B → A1 → A2 → A6 → A3 → A5 → A7(a) → A7(b)/A8.

---

## Part B — Make it feel like a real app (BUILD NOW)

Today a flowlet opens as a **desktop modal** and an **iOS sheet** — it reads as
a popup, not a place in the app. Goal: a flowlet is a **first-class screen** in
each host app, rendered in that platform's own design language, so its
components look like native Flowly UI rather than a generic renderer.

### B1. Desktop — modal → routed in-app screen
- Replace the `FlowletDetail` modal overlay with a real route
  `/flowlets/:id`, rendered inside the normal app shell (sidebar + top chrome
  persist). The tile grid navigates (React Router) instead of opening an
  overlay; a proper back affordance returns to the grid.
- Page chrome uses the desktop design system: real page header (icon + name +
  actions in the app's header style), content max-width and spacing matching
  other desktop pages (Board/Artifacts), the app's surface/card tokens, focus
  states, keyboard nav. No `bg-black/40` scrim, no centered card.
- The "Reminders" section and detail actions (pin/delete) move into the page
  header / a proper section, not modal buttons.

### B2. iOS — sheet → pushed screen
- Present the flowlet by **pushing onto the NavigationStack** (a real screen
  with a large/inline title + system back), not `.sheet`. The hub tile and a
  tapped reminder both navigate via `navigationDestination`.
- Adopt iOS conventions: large title, system materials, `.toolbar` for
  pin/delete, `List`/`Form` idioms where they fit, safe-area + Dynamic Type
  respect, swipe-back.

### B3. Component design-language alignment (both platforms)
Audit every component so it reads as native to the host:
- **Desktop:** use the app's actual surface/card/border/typography tokens and
  spacing scale (not renderer-local values); match button/input styling to the
  rest of desktop; respect light/dark tokens.
- **iOS:** system fonts + Dynamic Type, `Color`/material system, native control
  metaphors (Toggle, Stepper, Slider look like UIKit/SwiftUI system controls),
  SF Symbols weights consistent with the app.
- Keep the renderer a pure `(definition, values)` function — only the **styling
  primitives** change to reference host tokens.

**Acceptance for Part B:** opening a flowlet feels like navigating to a page in
Flowly (desktop) / pushing a screen (iOS); components are visually
indistinguishable from hand-built Flowly UI on each platform; builds green;
existing sync/watches/actions unaffected.

---

## Guardrails (unchanged)
Worktrees on `feat/flowlets`; never push without say-so; never touch installed
`~/.flowly`; UI plain language; each catalog change bumps the version with
forward-compatible client fallbacks.
