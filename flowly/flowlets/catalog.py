"""The flowlet component catalog — the single source of truth for which
components exist, what props they take, and which actions they can carry.

Both the schema validator (bot) and the native renderers (Desktop/iOS) are
written against this catalog. The renderers hard-code their own copy of the
component list; this module is what the *validator* enforces so the agent
can never persist a definition a client can't render.

Bumping :data:`CATALOG_VERSION` means new component types or props were added.
Old clients render unknown types as a graceful placeholder, so a higher
catalog is forward-compatible.
"""

from __future__ import annotations

#: Bump when component types or props change. Definitions carry ``catalog``;
#: a client hides / placeholders anything it doesn't understand.
CATALOG_VERSION = 1

# ── Structural limits (defence against runaway / malformed definitions) ───────
MAX_DEFINITION_BYTES = 64 * 1024
MAX_COMPONENTS = 200
MAX_DEPTH = 8
MAX_COMPUTED = 50
MAX_STATE_KEYS = 50
MAX_SERIES = 20
MAX_STRING_INPUT = 500          # cap for `input` component values
MAX_NAME_LEN = 100
MAX_LABEL_LEN = 500

# ── Aggregation vocabulary (used by computed + chart data) ────────────────────
AGGS = frozenset({"sum", "count", "avg", "min", "max", "last"})
BUCKETS = frozenset({"hour", "day", "week"})
WINDOWS = frozenset({"today", "7d", "30d", "90d", "all"})

# ── State value types ─────────────────────────────────────────────────────────
# `timer` is a structured state: {running, since_ms, accum_s}; resolve_values
# exposes it to clients as {running, elapsed} so a running timer ticks live.
# `list` is a dynamic collection: an array of {id, ...fields} items whose field
# schema the definition declares — the foundation for todo/shopping/journal
# screens. Rendered by the `repeater` component; mutated by the item_* ops.
STATE_TYPES = frozenset({"number", "bool", "string", "timer", "list"})

#: Field types a `list` item schema may declare.
ITEM_FIELD_TYPES = frozenset({"string", "number", "bool", "date"})
MAX_LIST_ITEMS = 200
MAX_ITEM_FIELDS = 8

# ── Action ops the interpreter understands (see actions.py) ───────────────────
ACTION_OPS = frozenset({
    "set", "increment", "decrement", "toggle",
    "log", "remove_last", "reset", "agent", "batch", "timer_toggle",
    # dynamic-list ops — item_add anywhere; the rest live inside a repeater's
    # item template (they need the tapped row's itemId from the client).
    "item_add", "item_update", "item_remove", "item_toggle", "item_move",
})

# ── Watches (declarative reactive rules; evaluated LLM-free) ──────────────────
# A definition may carry a top-level `watches` array. Each rule is evaluated by
# the bot on a heartbeat (and on client taps) and, when it fires, sends a push /
# desktop reminder. See flowly/flowlets/watches.py.
WATCH_TRIGGERS = frozenset({"schedule", "condition", "goal", "stale"})
WATCH_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
MAX_WATCHES = 20
MAX_WATCH_MESSAGE_LEN = 300
#: Default minimum gap between two fires of the same watch (minutes), by trigger.
#: `schedule` is de-duped per local day (`at`) or by its own `everyMinutes`, so
#: it needs no default cooldown.
WATCH_DEFAULT_COOLDOWN_MIN: dict[str, int] = {
    "condition": 360,   # 6h — a threshold nudge shouldn't nag
    "goal": 720,        # 12h — a celebration fires once per real achievement
    "stale": 720,       # 12h — pair with idleMinutes; re-fires only after new activity
}
#: Agent-wake watches (`also: {op: "agent"}`) are throttled at least this hard,
#: regardless of the watch's own cooldown — a model call must never be cheap to
#: trigger on a tight loop.
WATCH_AGENT_MIN_COOLDOWN_MIN = 30

# ── Live data sources (bring the outside world onto a flowlet) ────────────────
# A top-level `sources` object declares named bindings the bot refreshes on a
# schedule (LLM-free where possible) and writes into a *source-owned* state key
# a component then renders. See flowly/flowlets/sources.py.
#   * agent  — a model turn returns structured data (reuses the agent's tools;
#              same privilege as a cron self-prompt). Shipping first.
#   * tool   — a whitelisted read-only tool called directly, LLM-free (Phase 3).
#   * device — a client-side OS sensor synced up (HealthKit/location; Phase 3).
SOURCE_KINDS = frozenset({"agent"})            # `tool`/`device` land in later phases
MAX_SOURCES = 8
MAX_SOURCE_PROMPT_LEN = 1000
#: Minimum refresh interval per kind (minutes) — a scheduled fetch must never be
#: cheap to hammer. `manual` (refresh only on tap/open) is always allowed.
SOURCE_MIN_REFRESH_MIN = {"agent": 10}
SOURCE_DEFAULT_REFRESH_MIN = 30

# ── Icon names (platform-neutral; mapped to SF Symbols / lucide per client) ───
# Unknown names are allowed — the client falls back to a neutral dot — but
# these are the vetted set documented in docs/flowlets-catalog.md.
ICON_NAMES = frozenset({
    "droplet", "flame", "check", "heart", "star", "moon", "sun", "pill",
    "book", "dumbbell", "coffee", "leaf", "bell", "clock", "calendar",
    "target", "trophy", "zap", "smile", "cloud", "cup", "wallet", "cart",
    "run", "walk", "bed", "brain", "music", "camera", "phone", "mail",
    "pen", "trash", "undo", "plus", "minus", "arrow-up", "arrow-down",
    "sparkles", "activity",
})


# ── Component specs ───────────────────────────────────────────────────────────
# Each entry:
#   container      — has a `children` array (recursed into)
#   action         — may carry an `action` object
#   required       — prop names that must be present (besides `type`)
#   binds          — prop names whose *string* value references a scalar key
#                    (state/computed) OR is a numeric literal — validated to be
#                    a known key when non-numeric.
#   category       — layout | display | input (documentation only)
#
# The validator uses `container`, `action`, `required`, `binds`; other keys
# are free-form and passed through to the client untouched.

COMPONENTS: dict[str, dict] = {
    # ── Layout (7) ────────────────────────────────────────────────────────────
    "card":     {"category": "layout", "container": True,  "action": False},
    "row":      {"category": "layout", "container": True,  "action": False},
    "column":   {"category": "layout", "container": True,  "action": False},
    "grid":     {"category": "layout", "container": True,  "action": False},
    "list":     {"category": "layout", "container": True,  "action": False},
    "divider":  {"category": "layout", "container": False, "action": False},
    "spacer":   {"category": "layout", "container": False, "action": False},
    # Renders its `item` template once per item of a `list` state key. Inside
    # the template, `$.field` binds to the current item and `{$.field}`
    # interpolates it; inner actions automatically carry the row's itemId.
    "repeater": {"category": "layout", "container": False, "action": False,
                 "required": ["source", "item"]},

    # ── Display (14) ──────────────────────────────────────────────────────────
    "header":    {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "text":      {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "badge":     {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "icon":      {"category": "display", "container": False, "action": False,
                  "required": ["name"]},
    "stat":      {"category": "display", "container": False, "action": False,
                  "binds": ["value"]},
    "progress":  {"category": "display", "container": False, "action": False,
                  "required": ["value"], "binds": ["value", "max"]},
    "ring":      {"category": "display", "container": False, "action": False,
                  "required": ["value"], "binds": ["value", "max"]},
    "gauge":     {"category": "display", "container": False, "action": False,
                  "required": ["value"], "binds": ["value", "min", "max"]},
    "chart":     {"category": "display", "container": False, "action": False,
                  "required": ["data"]},
    "sparkline": {"category": "display", "container": False, "action": False,
                  "required": ["data"]},
    "heatmap":   {"category": "display", "container": False, "action": False,
                  "required": ["data"]},
    "table":     {"category": "display", "container": False, "action": False,
                  "required": ["rows"]},
    "clock":     {"category": "display", "container": False, "action": False},
    "countdown": {"category": "display", "container": False, "action": False,
                  "required": ["target"]},

    # ── Display v2 (structured / professional) ────────────────────────────────
    "metric":    {"category": "display", "container": False, "action": False,
                  "required": ["value"], "binds": ["value", "delta"]},
    "status":    {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "keyvalue":  {"category": "display", "container": False, "action": False,
                  "required": ["rows"]},
    "timeline":  {"category": "display", "container": False, "action": False,
                  "required": ["events"]},
    "callout":   {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "code":      {"category": "display", "container": False, "action": False,
                  "required": ["text"]},
    "link":      {"category": "display", "container": False, "action": False,
                  "required": ["text", "url"]},
    "image":     {"category": "display", "container": False, "action": False,
                  "required": ["src"]},
    "timer":     {"category": "display", "container": False, "action": True},

    # ── Input / interaction (10) ──────────────────────────────────────────────
    "button":       {"category": "input", "container": False, "action": True,
                     "required": ["text"]},
    "icon_button":  {"category": "input", "container": False, "action": True,
                     "required": ["icon"]},
    "stepper":      {"category": "input", "container": False, "action": True,
                     "binds": ["value"]},
    "slider":       {"category": "input", "container": False, "action": True,
                     "required": ["min", "max"], "binds": ["value"]},
    "toggle":       {"category": "input", "container": False, "action": True,
                     "binds": ["value"]},
    "checklist":    {"category": "input", "container": False, "action": False,
                     "required": ["items"]},
    "segmented":    {"category": "input", "container": False, "action": True,
                     "required": ["options"]},
    "input":        {"category": "input", "container": False, "action": True},
    "number_input": {"category": "input", "container": False, "action": True,
                     "binds": ["value"]},
    "rating":       {"category": "input", "container": False, "action": True,
                     "binds": ["value"]},
    # ── Input v2 ──────────────────────────────────────────────────────────────
    "select":       {"category": "input", "container": False, "action": True,
                     "required": ["options"]},
    "date":         {"category": "input", "container": False, "action": True},
    "textarea":     {"category": "input", "container": False, "action": True},
}

#: Chart-family components whose ``data`` prop resolves to a per-bucket series.
SERIES_COMPONENTS = frozenset({"chart", "sparkline", "heatmap"})

#: Component types that exist (for the renderer-agnostic membership check).
COMPONENT_TYPES = frozenset(COMPONENTS)

#: Types that hold a `children` array.
CONTAINER_TYPES = frozenset(t for t, s in COMPONENTS.items() if s.get("container"))
