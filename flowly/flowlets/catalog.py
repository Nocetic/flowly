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
STATE_TYPES = frozenset({"number", "bool", "string"})

# ── Action ops the interpreter understands (see actions.py) ───────────────────
ACTION_OPS = frozenset({
    "set", "increment", "decrement", "toggle",
    "log", "remove_last", "reset", "agent", "batch",
})

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
}

#: Chart-family components whose ``data`` prop resolves to a per-bucket series.
SERIES_COMPONENTS = frozenset({"chart", "sparkline", "heatmap"})

#: Component types that exist (for the renderer-agnostic membership check).
COMPONENT_TYPES = frozenset(COMPONENTS)

#: Types that hold a `children` array.
CONTAINER_TYPES = frozenset(t for t, s in COMPONENTS.items() if s.get("container"))
