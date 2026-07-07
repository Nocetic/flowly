# Memory Governance & Consolidation Architecture

How Flowly turns "the agent wrote something to memory" into a **governed,
self-maintaining** long-term memory: every write gets a lifecycle, contradictions
supersede instead of pile up, and a background pass cleans out duplicates and
stale notes without the user asking.

This is an *additive governance layer* on top of the memory engines that already
existed (the temporal knowledge graph, `MEMORY.md`/daily notes, the FTS/vector
index). It does **not** replace them — it wraps them. **On by default**
(`agents.defaults.memory_dreaming.enabled` defaults to `true`); users opt out by
setting it `false`. See [Rollout & defaults](#rollout--defaults).

> **Status (2026-06).** Live capture (main agent + background self-review),
> auto-supersede, manual + autonomous consolidation, and the full CLI are wired
> and verified in chat. The offline cross-session `MemoryDreamerService` engine is
> **now wired to live triggers** (idle / daily / turn) through a streaming-provider
> extractor (`flowly/memory/extractor.py`) — see
> [Wiring the offline dreamer](#wiring-the-offline-dreamer). The extraction *prompt*
> is v1; its at-scale quality is still being tuned.

---

## Table of contents

1. [TL;DR](#tldr)
2. [Why a governance layer](#why-a-governance-layer-vs-just-writing-to-memory)
3. [Glossary](#glossary)
4. [Components & code layout](#components--code-layout)
5. [Relationship to the engines it wraps](#relationship-to-the-engines-it-wraps)
6. [Storage & file inventory](#storage--file-inventory)
7. [Data model](#data-model)
8. [API reference](#api-reference)
9. [The four flows](#the-four-flows)
10. [Worked example: lifecycle of one fact](#worked-example-lifecycle-of-one-fact)
11. [Concurrency model](#concurrency-model)
12. [Failure modes & recovery](#failure-modes--recovery)
13. [Observability](#observability)
14. [Performance & cost](#performance--cost)
15. [Security & threat model](#security--threat-model)
16. [Configuration](#configuration)
17. [Rollout & defaults](#rollout--defaults)
18. [Extension points](#extension-points)
19. [Wiring the offline dreamer](#wiring-the-offline-dreamer)
20. [What is not wired](#what-is-not-wired)
21. [Design decisions (rationale)](#design-decisions-rationale)
22. [Runbook / FAQ](#runbook--faq)
23. [Testing](#testing)

---

## TL;DR

```
agent writes (memory_append / knowledge_graph)
        │  post_tool_call hook (main agent AND subagents)
        ▼
MemoryGovernance.ingest_*  ──▶ governed MemoryItem (status=active)
        │                         │
        │                         ├─▶ supersede older same-key item (+ close its KG triple)
        │                         ├─▶ regenerate MEMORY.md generated block (omits secrets)
        │                         └─▶ mark store "dirty"
        ▼
autonomous consolidation (every N turns + every ~30m, gated on dirty + lock)
        │  LLM proposes cleanup ops  →  deterministic apply_operations (audited, never deletes)
        ▼
merged / superseded / staled items   ──▶ MEMORY.md regenerated
```

Structured facts stay in the knowledge graph (system of record); the governance
DB owns *lifecycle* (status, calibrated confidence, provenance, audit) and points
back at the KG triple it governs. Two layers, two cost profiles: **capture +
supersede** is deterministic and free; **consolidation** is an LLM call.

Three later additions extend this core: **F3** coalesces `MEMORY.md` regeneration
to once per turn (and, flag-gated, freezes the injected memory block) to protect the
Anthropic prefix cache; **F2** adds a trust feedback loop that ranks by confidence
and nudges it on helpful/unhelpful signals; and a sibling **skill-governance**
subsystem applies the same wrapper-store + snapshot + never-delete + audit model to
the agent's *skill library* (creation + consolidation) — documented separately in
[`skill-self-improvement-architecture.md`](skill-self-improvement-architecture.md).

---

## Why a governance layer (vs. just writing to memory)

The pre-existing memory was append-mostly: `memory_append` wrote timestamped
lines to `MEMORY.md`; the KG stored triples. That captures facts but accumulates
problems that a real chat surfaces fast:

- the agent writes a free-form note that just duplicates a KG fact;
- a fact changes (new email) but the old free-form note referencing it goes stale;
- the agent emits a malformed triple (`subject == object`) before it knows a name;
- nothing ever retires anything, so memory only grows.

Fixing these needs *judgement* and *lifecycle*, not just storage. The governance
layer adds both, while keeping the underlying engines as the source of record so
the change is reversible (drop the governance DB → old behavior intact).

---

## Glossary

| Term | Meaning |
|---|---|
| **Governance store** | The new SQLite DB (`memory_governance.sqlite3`) that owns memory lifecycle. |
| **MemoryItem** | One governed unit: a fact or a free-form note, with status + provenance. |
| **Wrapper / ref** | An item doesn't store the fact; it *references* a KG triple (`kg_triple`) or a MEMORY.md anchor (`memory_md`), or carries text `inline`. |
| **Capture / ingest** | The live, deterministic path: an agent write becomes an active item. |
| **Supersede** | Retire an item in favor of a newer one (records `supersedes`, sets KG `valid_to`). Never deletes. |
| **Consolidation** | The LLM-proposed, governance-applied cleanup pass (merge/supersede/stale). |
| **Dirty** | A flag meaning "memory changed since the last consolidation". Gates autonomous runs. |
| **Dreamer** | The offline cross-session engine (`dreamer.py`) — built, not wired live. |
| **Generated block** | The auto-written region of `MEMORY.md` between the sentinels. |

---

## Components & code layout

```
flowly/memory/
├── governance.py    — GovernanceStore: SQLite lifecycle store (the new primitive)
│                       MemoryItem, status machine, append-only audit, meta kv
├── summary.py       — generated MEMORY.md block (sentinels) + manual-content
│                       preservation + regenerate_memory_md (omits secrets)
├── migration.py     — one-time legacy MEMORY.md → candidate items (backup, KG-dedup)
├── calibration.py   — confidence from signals (explicit/repeat/recency/conflict)
├── kg_mirror.py     — SqliteKGMirror: close/reopen a KG triple by id
├── coordinator.py   — MemoryGovernance facade: user actions, recall, live ingest,
│                       dirty tracking, refresh  (backs the CLI + the live hook)
├── consolidate.py   — LLM-proposed cleanup: ConsolidateOp, build_context,
│                       parse_operations, apply_operations (deterministic), Consolidator
└── dreamer.py       — MemoryDreamerService: cross-session offline engine (NOT wired live)

flowly/agent/
├── loop.py          — _maybe_enable_memory_governance (hook + tool + timers),
│                       _governance_post_tool, _maybe_consolidate, _consolidation_timer,
│                       turn trigger inside _maybe_spawn_review
├── subagent.py      — SubagentManager.governance_post_tool → SubagentToolRegistry(hooks=…)
└── tools/
    └── memory_consolidate.py — agent-facing tool; runs consolidation through the
                                 loop's already-authenticated provider.chat_stream

flowly/cli/memory_cmd.py     — `flowly memory list/accept/reject/correct/undo/
                                refresh/status/stats/migrate/consolidate`
flowly/config/schema.py      — MemoryDreamingConfig

scripts/memlab.sh            — isolated CLI sandbox runner for testing
scripts/memchat-setup.sh     — isolated gateway profile for live chat testing
```

### Module dependency direction

```
loop.py / subagent.py / memory_consolidate.py  (wiring)
        │  depend on
        ▼
coordinator.py (MemoryGovernance facade)
        │  composes
        ├──▶ governance.py (GovernanceStore)      ← the only writer of the gov DB
        ├──▶ summary.py (regenerate_memory_md)
        ├──▶ kg_mirror.py (SqliteKGMirror)
        └──▶ consolidate.py (apply_operations)     ← also used directly by the tool

dreamer.py depends on governance.py only (self-contained; no live wiring yet)
calibration.py / migration.py are leaf utilities
```

`governance.py` is the foundation and has **no** intra-package dependencies on the
other memory modules — everything else points *down* at it.

---

## Relationship to the engines it wraps

| Substrate | Module | Role after governance |
|---|---|---|
| Temporal knowledge graph | `flowly/memory/knowledge_graph.py` | **System of record for structured facts.** Governance references triple ids; never duplicates the triple store. `knowledge_graph.py` is **unmodified** by this work. |
| `MEMORY.md` + daily notes | `flowly/agent/memory.py` | Becomes a **generated, human-readable summary** of active items (+ KG headline). Manual content outside the sentinels is preserved. |
| FTS5 + vector index | `flowly/memory/{manager,indexer}.py` | Unchanged. The search layer; reindexes the regenerated `MEMORY.md`. |

The governance layer touches the KG only through `SqliteKGMirror` (a temporal
`UPDATE triples SET valid_to=…` — exactly what `KnowledgeGraph.invalidate` does,
but addressable by triple id) so it never reaches into KG internals.

---

## Storage & file inventory

Everything lives under `state_dir = get_data_dir()` (the **profile data dir** —
`~/.flowly`, or `~/.flowly/profiles/<name>`), except `MEMORY.md` which is in the
workspace.

| Path | Written by | Notes |
|---|---|---|
| `<state_dir>/memory_governance.sqlite3` | `GovernanceStore` | new; WAL; single writer |
| `<state_dir>/knowledge_graph.sqlite3` | KG tool + `SqliteKGMirror` | pre-existing; mirror only closes/reopens triples |
| `<workspace>/memory/MEMORY.md` | `regenerate_memory_md` + legacy `memory_append` | generated block + manual content |
| `<workspace>/memory/MEMORY.md.bak-<runid>` | `migrate_memory_md` | backup before the one-time import |
| `<state_dir>/session_index.sqlite` | `SessionIndexer` | pre-existing; the dreamer's watermark source. Indexing is **incremental / id-stable** — a save or a startup rebuild appends only new tail rows and preserves existing `messages.id`, re-id'ing a session only when its stored prefix diverges (compaction/edit). This is what makes the id-based watermark reliable; the old delete-all + reinsert churned ids and made the dreamer reprocess history forever. |

> **`state_dir` gotcha (caused a real bug).** `state_dir` is **not**
> `workspace/.flowly_state`. The gateway constructs `AgentLoop(state_dir=get_data_dir())`
> (`flowly/cli/gateway_cmd.py`). The KG and the governance DB live under
> `get_data_dir()`. CLI code **must** resolve the same way (`memory_cmd.py` uses
> `get_data_dir()`), or it opens an empty DB while the agent writes elsewhere.

---

## Data model

DB: `<state_dir>/memory_governance.sqlite3` — WAL, foreign keys on, a single
persistent connection guarded by a `threading.RLock` (mirrors
`flowly/board/store.py`).

### Exact schema

```sql
CREATE TABLE memory_items (
    id                 TEXT PRIMARY KEY,            -- m_<uuid12>
    kind               TEXT NOT NULL,              -- see kinds below
    text               TEXT NOT NULL,              -- human-readable rendering
    status             TEXT NOT NULL,              -- lifecycle state
    ref_kind           TEXT NOT NULL DEFAULT 'inline',  -- kg_triple | memory_md | inline
    ref_id             TEXT,                       -- triple id / anchor / NULL
    normalized_key     TEXT NOT NULL DEFAULT '',   -- clustering key for dedup/contradiction
    confidence         REAL NOT NULL DEFAULT 0.0,  -- calibrated, not raw LLM
    privacy_level      TEXT NOT NULL DEFAULT 'normal',  -- normal | sensitive | secret
    source_session     TEXT NOT NULL DEFAULT '',   -- provenance: channel:chat_id
    source_message_ids TEXT NOT NULL DEFAULT '[]', -- JSON array
    supersedes         TEXT REFERENCES memory_items(id),
    valid_from         TEXT,
    valid_to           TEXT,
    last_seen_at       TEXT,
    last_used_at       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE memory_audit (               -- append-only; one row per transition
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,            -- dreamer | user | system | migration
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);

CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE memory_feedback (            -- append-only; trust signal per item (F2)
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    helpful   INTEGER NOT NULL,           -- 1 = helpful, 0 = unhelpful
    note      TEXT NOT NULL DEFAULT '',
    given_at  TEXT NOT NULL
);

-- indices: idx_items_status, idx_items_kind, idx_items_key,
--          idx_items_ref(ref_kind,ref_id), idx_items_privacy, idx_audit_item,
--          idx_feedback_item
```

`memory_meta` keys in use:

| Key | Owner | Meaning |
|---|---|---|
| `consolidate_dirty` | `coordinator.py` | `"1"` ⇒ memory changed since last consolidation |
| `dreamer_watermark` | `dreamer.py` | max session-index message id processed |
| `dreamer_lock` | `dreamer.py` | `owner@isotimestamp` advisory lock (stale after 30m) |
| `memory_md_migrated` | `migration.py` | `"1"` ⇒ legacy import already ran (idempotent) |

The `memory_feedback` table mirrors `memory_audit`'s append-only shape and is the
store for the **trust feedback loop** (F2): each helpful/unhelpful signal lands a
row, then nudges the referenced item's `confidence` (see API ref → `ingest_feedback`).
The in-memory `_summary_dirty` flag (a coordinator field, not a meta key — it lives
for the duration of a process) coalesces `MEMORY.md` regeneration to once per turn
(F3a); it is distinct from the persisted `consolidate_dirty` meta key.

### Kinds

`profile`, `preference`, `project`, `environment`, `correction`, `temporal`,
`relationship`, `procedure`, `fact`. `fact` items reference the KG; the rest are
free-form/inline. `temporal` items decay under calibration; the others don't.

### Status machine (enforced)

`GovernanceStore.transition()` consults a fixed transition table and raises
`InvalidTransition` on any move not listed — illegal transitions can't silently
corrupt state. Every applied move writes a `memory_audit` row.

| From → allowed To | Typical trigger |
|---|---|
| `candidate` → `active` | auto-commit: conf ≥ auto_floor, no conflict, not sensitive |
| `candidate` → `needs_review` | mid confidence / conflict / sensitive |
| `candidate` → `rejected` | conf < review_floor or injection-flagged |
| `candidate` → `superseded` | a brand-new candidate that immediately loses arbitration |
| `needs_review` → `active` / `rejected` / `stale` | user accept / reject; aging |
| `active` → `superseded` | a newer explicit fact wins arbitration |
| `active` → `stale` | `valid_to` passed / not seen (temporal); consolidation `stale` op |
| `active` → `needs_review` / `rejected` | re-queue; user reject |
| `stale` → `active` / `superseded` / `rejected` | undo / late supersede |
| `superseded` → `active` | **undo only** |
| `rejected` → *(terminal)* | reopen by creating a fresh candidate |

### normalized_key conventions

The dedup/contradiction cluster key. Conventions in the live path:

- facts: `fact:<subject_lower>|<predicate_lower>` (e.g. `fact:hakan|email`)
- preferences: `pref:<slug-of-first-6-words>`
- legacy import: `memory_md:<slug>`

Two items with the same `normalized_key` are candidates for dedup (same text) or
contradiction (different text); see [Flow 1](#flow-1--live-capture-automatic-no-user-prompt).

### Confidence calibration

`calibration.py` replaces the unreliable LLM-self-reported number:

```
score = base(0.60)
      + explicit_bonus(0.25)  if the user stated it (vs agent inferred)
      + repeat (min 0.15, +0.05 per extra cross-session sighting)
      × recency_decay          (temporal kinds only; half-life 30d)
      − conflict_penalty(0.30)
clamp [0,1]
```

Tuned so a single explicit, unconflicted fact clears `auto_floor` (0.80) while an
inferred-once fact lands in the `needs_review` band (`review_floor` 0.55). Used by
the dreamer engine (opt-in via `calibrate=True`). The **live ingest path uses
fixed trusted scores** — 0.85 free-form, 0.90 fact — because the agent explicitly
chose to write them; calibration matters most for the speculative dreamer.

---

## API reference

### `GovernanceStore` (`governance.py`) — the only writer of the gov DB

```python
add_item(*, kind, text, status='candidate', ref_kind='inline', ref_id=None,
         normalized_key='', confidence=0.0, privacy_level='normal',
         source_session='', source_message_ids=None, supersedes=None,
         valid_from=None, valid_to=None, actor='system', reason='created') -> MemoryItem
transition(item_id, to_status, *, actor='system', reason='', supersedes=None) -> MemoryItem
update_fields(item_id, **fields) -> MemoryItem        # text/confidence/… ; NOT status
touch_seen(item_id) / touch_used(item_id) -> None     # repetition / recall signals
get_item(item_id) -> MemoryItem | None
list_items(*, status=None, kind=None, ref_kind=None, privacy_level=None, limit=None)
find_by_key(normalized_key, *, statuses=None) -> list[MemoryItem]   # reconcile lookup
find_by_ref(ref_kind, ref_id) -> list[MemoryItem]
audit_log(item_id) -> list[AuditEntry]
stats() -> {total, by_status, by_kind, review_queue, active}
get_meta(key, default=None) / set_meta(key, value)    # watermark, dirty, locks
close()
```

Raises `GovernanceError` (bad enum / missing item) and `InvalidTransition`.

### `MemoryGovernance` facade (`coordinator.py`) — backs the CLI + the live hook

```python
# user actions (actor='user', audited)
accept(id) / reject(id) / correct(id, text, *, confidence=None) / undo(id) -> MemoryItem
# reads
list_items(*, status=None) / review_queue() / stats()
recall(*, include_sensitive=False, limit=None) -> {items[], kg_summary, count}
# live ingest (called by the post_tool_call hook)
ingest_append(content, *, source_session='') -> MemoryItem | None
ingest_kg_fact(subject, predicate, object, triple_id, *, source_session='') -> MemoryItem | None
# trust feedback (F2) — post-hoc nudge on stored confidence
ingest_feedback(item_id, helpful: bool, note='') -> MemoryItem | None
# maintenance
refresh() -> str | None                  # regenerate MEMORY.md (unconditional)
refresh_if_dirty() -> str | None         # regenerate iff _summary_dirty, then clear (F3a)
mark_dirty() / is_dirty() / clear_dirty()       # consolidate_dirty (persisted)
```

`undo` restores a superseded/stale item, demotes the current active sibling on its
key, and reflects both into the KG (restore + close). `recall` excludes `secret`
always and `sensitive` unless asked; it now **orders returned items by `confidence`
desc** (tie-break on text), so the highest-trust facts surface first.

`ingest_feedback` records a `memory_feedback` row, nudges the item's stored
confidence (**+0.10** helpful / **−0.15** unhelpful, clamped `[0,1]`) via
`update_fields`, writes an audit row, and — if confidence falls below `review_floor`
(0.55) — transitions the item `active → needs_review` (the behavioral payoff that
makes feedback meaningful). It then sets `_summary_dirty` so the trust change is
reflected in `MEMORY.md` at turn end (cheap, via F3a). It is intentionally separate
from `calibration.calibrate()` (which runs once at ingest); feedback is a post-hoc
adjustment on already-stored confidence, not a recalibration.

### `consolidate.py` — LLM proposes, governance applies

```python
build_context(gov, kg_summary='') -> {items[], kg_summary}    # snapshot for the LLM
parse_operations(raw) -> list[ConsolidateOp]                   # tolerant JSON parse
apply_operations(gov, ops, *, kg_mirror=None) -> ConsolidateResult   # deterministic, audited
class Consolidator(gov, propose_fn, *, kg_mirror=None, memory_store=None, kg_summary_fn=None)
    .run(*, dry_run=False) -> (ops, ConsolidateResult)

ConsolidateOp(op, item_id, into_id=None, reason='')   # op ∈ {merge, supersede, stale}
ConsolidateResult(proposed, superseded, staled, merged, skipped, errors[])
```

`apply_operations` validates every op (target must be active; merge survivor must
be active and distinct), mirrors KG closes, never deletes. `propose_fn` is the LLM
seam — injected so the apply logic is testable with a fake proposer.

### `dreamer.py` — offline cross-session engine (built, not wired)

```python
class MemoryDreamerService(gov, delta_source, extractor, *, auto_floor, review_floor,
        injection_check, on_committed=None, lock_owner='dreamer',
        calibrate=False, calibration_weights=None, kg_mirror=None)
    .run(*, max_messages=500) -> DreamResult

DeltaSource(Protocol): read_since(watermark_id, limit) -> Sequence[MessageRow]
Extractor(Protocol):   extract(delta) -> Sequence[Candidate]
SessionIndexDeltaSource(db_path)   # live adapter, read-only over session_index.sqlite
                                   # filters automation sessions in SQL
                                   # (heartbeat:/cron:/subagent:/system: + .full
                                   # mirror twins) so the dreamer only learns from
                                   # real user conversation, and the row limit
                                   # applies to user messages, not noise
```

See [Wiring the offline dreamer](#wiring-the-offline-dreamer).

---

## The four flows

### Flow 1 — live capture (automatic, no user prompt)

When `memory_dreaming.enabled`, `AgentLoop._maybe_enable_memory_governance`:
builds the facade, registers `_governance_post_tool` on `self.hooks` for the
`post_tool_call` event, **pushes the same hook to `SubagentManager.governance_post_tool`**
(so background self-review's separate registry fires it too — `subagent.py` builds
its `SubagentToolRegistry(hooks=HookRegistry().register('post_tool_call', …))`),
registers the `memory_consolidate` tool, and starts the autonomy timer.

Sequence for a `memory_append`:

```
LLM emits memory_append(content)
 └─ ToolRegistry.execute("memory_append", …)
     ├─ MemoryAppendTool.execute()          # writes the timestamped line to MEMORY.md
     └─ hooks.fire_post_tool(ctx)            # ctx.tool_name/params/result/success
         └─ _governance_post_tool(ctx)
             └─ MemoryGovernance.ingest_append(content)
                 ├─ dedup vs active items (normalized text) → touch_seen + return if dup
                 ├─ add_item(kind=preference, status=candidate, ref_kind=memory_md, conf=0.85)
                 ├─ transition(active)
                 ├─ refresh()  → regenerate_memory_md (splice; omits secrets)
                 └─ mark_dirty()
```

Sequence for a `knowledge_graph` add:

```
LLM emits knowledge_graph(action=add, subject, predicate, object)
 └─ KnowledgeGraphTool.execute() → "Fact added: … (id: t_…)"
     └─ post_tool hook → _governance_post_tool
         └─ regex pull triple id from result → ingest_kg_fact(subject, predicate, object, t_id)
             ├─ skip if subject == object (self-referential garbage)
             ├─ skip if this triple already governed (find_by_ref)
             ├─ for each ACTIVE item on fact:<subj>|<pred> with a different triple:
             │     transition(SUPERSEDED) + kg_mirror.supersede(old_triple_id)
             ├─ add_item(kind=fact, ref_kind=kg_triple, ref_id=t_id, conf=0.90) → active
             ├─ refresh() + mark_dirty()
```

This path is intentionally dumb — faithful record + same-key supersede, nothing
semantic. Semantic cleanup is Flow 2.

### Flow 2 — consolidation (cleanup / GC)

`consolidate.py`. An LLM reads all active items + the KG summary and **proposes**
`ConsolidateOp`s; it never writes. `apply_operations` does the writing — validating
each op against the live store, mirroring KG closes, auditing every move, never
deleting.

| Op | Meaning | Effect |
|---|---|---|
| `merge` | same fact under different keys | loser → superseded into survivor; KG triple closed |
| `supersede` | free-form fully duplicated by KG facts | item → superseded |
| `stale` | free-form referencing outdated info | item → stale |

**Where the LLM call happens.** The standalone `flowly memory consolidate` CLI
builds its own provider — but the Flowly proxy **504s on long non-streamed
completions** and intermittently returns an empty stream for these ~40-90s
reasoning-model calls. So the production path is the **`memory_consolidate` tool**,
which runs through the loop's already-authenticated `self.provider.chat_stream`
and **retries up to 3× on empty**. The agent calls it on user request or it runs
autonomously (Flow 3).

### Flow 3 — autonomy (self-maintenance)

Ingests `mark_dirty()`. A background consolidation pass fires on two triggers, both
**gated on dirty + an `asyncio.Lock`** (no overlap) and **fire-and-forget** (never
blocks a user turn):

| Trigger | Where | Default |
|---|---|---|
| Turn-based | counter in `_maybe_spawn_review`, incremented each user turn | every `consolidate_turn_interval` = 50 turns |
| Background timer | `_consolidation_timer` task started in `AgentLoop.run()` | every `consolidate_every_minutes` = 30 min |

```
_maybe_consolidate(trigger):
    if not enabled / facade None / tool None: return
    if not is_dirty(): return
    if lock.locked(): return
    async with lock:
        if not is_dirty(): return          # re-check inside the lock
        result = await memory_consolidate_tool.execute(dry_run=False)
        clear_dirty()
        log "[memory-gov] auto-consolidate (trigger): …"   # errors swallowed
```

### Flow 4 — user governance (CLI)

`flowly memory …` (thin wrappers over `MemoryGovernance`):

```
list [--status]      review        stats / status
accept <id>          reject <id>   correct <id> "text"
undo <id>            refresh       migrate
consolidate [--dry-run] [--raw]
```

All mutating actions are audited with `actor=user`. `--raw` on `consolidate` prints
the raw LLM output (debug for the standalone provider path).

---

## Worked example: lifecycle of one fact

User, over two sessions, says "my email is a@x.com", then later "actually it's
b@x.com", then a consolidation runs, then the user undoes it.

```
T0  ingest_kg_fact("Hakan","email","a@x.com", t_a)
      memory_items: m1 {fact, ref=t_a, key=fact:hakan|email, status=active, conf=0.90}
      audit:        m1 (None→candidate, system), m1 (candidate→active, system)

T1  ingest_kg_fact("Hakan","email","b@x.com", t_b)
      finds active m1 on same key, different triple → arbitrate
      m1 → superseded ; kg_mirror.supersede(t_a)   (KG: t_a.valid_to set)
      m2 {fact, ref=t_b, status=active, conf=0.90}
      audit: m1 (active→superseded, system, "superseded_by_newer_kg_fact")
             m2 (None→candidate), m2 (candidate→active)
      mark_dirty()

T2  autonomous consolidation (dirty): LLM also flags a stale free-form note m3
      apply_operations: m3 (active→stale, system, "consolidate: outdated email")
      MEMORY.md regenerated (active set = m2 only for email)
      clear_dirty()

T3  user: flowly memory undo m1
      demote current active sibling m2 → superseded ; kg_mirror.supersede(t_b)
      m1 → active ; kg_mirror.restore(t_a)
      audit: m2 (active→superseded, user, "demoted_by_undo")
             m1 (superseded→active, user, "user_undo")
```

The audit table is the full forensic trail; nothing was ever deleted.

---

## Concurrency model

- **Single writer, one process.** All gov-DB writes go through one `GovernanceStore`
  instance holding one SQLite connection behind a `threading.RLock`. There is no
  claim-lock / CAS / heartbeat machinery because there are no competing writer
  *processes* — the agent, subagents, and timers are all in-process.
- **`check_same_thread=False` + RLock.** aiohttp handlers, tool coroutines, the
  background timer, and tests may touch the store from different threads/the event
  loop; the RLock serializes them. WAL lets dashboard reads proceed during writes.
- **Autonomous passes serialized by an `asyncio.Lock`.** The turn trigger and the
  30-min timer both call `_maybe_consolidate`, which bails if the lock is held and
  re-checks `is_dirty()` inside the lock — so two triggers can't double-run, and a
  pass that clears the work isn't immediately repeated.
- **Re-entrancy / ordering.** Subagents run as `asyncio.create_task` on the same
  loop (no true parallelism); the hook's `ingest_*` is synchronous and quick. A
  subagent's `memory_append` and the main agent's are serialized by the event loop
  + the RLock.
- **The dreamer** (when wired) uses its own meta-row advisory lock (`dreamer_lock`,
  stale-takeover after 30 min) so it is single-runner and crash-resumable.

---

## Failure modes & recovery

| Failure | Behavior |
|---|---|
| Consolidation LLM returns empty / 504 | retried up to 3×; if still empty, the tool returns "no operations proposed" and the autonomous pass logs a warning — **dirty stays set**, so the next trigger retries. |
| `_governance_post_tool` raises | caught + logged (`[memory-gov] post_tool sync failed`); the agent's tool result is unaffected. |
| `on_committed` / MEMORY.md refresh raises | caught + logged; the governance write already committed. |
| Crash mid-consolidation | `apply_operations` commits per transition; partial progress is valid (each op is independent). Dirty stays set → next pass finishes. |
| Crash mid-dreamer-run | watermark only advances after the commit pass → re-run reprocesses the same delta safely (idempotent via dedup). |
| Extractor infra failure (LLM bridge / empty after retries) | raises `ExtractionError`; the engine **holds the watermark** (no advance) so the delta is retried, instead of being silently skipped forever. A genuine empty extraction (parseable `[]`) advances normally. |
| Injection scanner unavailable (raises) | **fails closed** — the candidate is routed to `needs_review` (never silently activated, never silently dropped); a genuine injection flag still rejects. |
| Migration interrupted | `memory_md_migrated` flag is set only at the end; a re-run re-imports. The original is already backed up. Internal + KG dedup keep it from duplicating. |
| User edits inside the generated block | overwritten on next `refresh()`. The block carries a "edits inside the markers are overwritten" warning; manual notes belong outside the sentinels (preserved by `splice`). |
| Governance DB deleted | recreated empty on next start; capture resumes; existing KG/MEMORY.md intact. Pure rollback. |
| Bad auto-supersede / stale | `flowly memory undo <id>` (or accept from review); full audit trail to diagnose. |

---

## Observability

- **Logs.** Grep `[memory-gov]` for: enable line, `post_tool sync failed`,
  `auto-consolidate (turn|timer): …`, `auto-consolidate timer every Nm`. The
  consolidate tool logs `[consolidate]` on LLM/refresh failures.
- **Audit table** is the forensic record: `gov.audit_log(item_id)` →
  `(from_status, to_status, actor, reason, at)` per transition.
- **`flowly memory stats`** → counts by status + kind, review-queue depth, active
  count. The metrics surface for "is it working / is the queue growing".
- **`flowly memory list --status <s>`** to inspect any cohort;
  `flowly memory consolidate --dry-run --raw` to see exactly what the LLM proposes
  and returns.

---

## Performance & cost

- **Per ingest:** a few SQLite writes; `MEMORY.md` regeneration is **coalesced to
  once per turn** (F3a). `ingest_append`/`ingest_kg_fact`/`ingest_feedback` set an
  in-process `_summary_dirty` flag instead of regenerating inline; `loop.py` calls
  `refresh_if_dirty()` once at turn end, so a turn with 8 KG adds now rewrites
  `MEMORY.md` once, not 8×. Behavior-neutral (the rendered content is identical; only
  the write cadence changed). The payoff is **prompt-cache stability**: the system
  prompt reads `MEMORY.md` fresh each turn and `apply_cache_control` marks it
  cacheable, so every mid-turn byte-diff was a full cache *write* (≈2× tokens)
  instead of a *read* (≈0.1×). One write/turn keeps the prefix stable within a turn.
- **Frozen injected-memory snapshot (F3b, flag-gated, default OFF):** when
  `memory_dreaming.freeze_injected_memory=true`, the memory block is snapshotted
  once per session (and re-captured after each compaction) and that frozen string is
  injected for the rest of the window, so even an *end-of-turn* regeneration doesn't
  bust the cached prefix mid-session. New writes still land on disk and remain
  reachable via `memory_search`/`memory_recall`; they just aren't re-injected into
  the *system prompt* until the next snapshot boundary. Ships **dark** until a
  measured cache-read before/after proves a gain with identical outputs (HARD
  REQUIREMENT — the cache must never regress); coalescing alone is the safe default.
- **Per consolidation:** one LLM call (~40-90s on the slow reasoning model used by
  default), ≤ ~2K output tokens, plus the active-set + KG summary as input. Gated on
  dirty, so it runs at most once per turn-interval / timer-tick *and only when there
  is new memory*. This is recurring **token spend on the user's provider** — the
  main reason full-auto is a conscious product choice (see Rollout).
- **Steady state:** the only always-on background work is the 30-min timer (a sleep
  loop) and the governance writes on memory tool calls. Negligible when idle.
- **Tuning levers:** `consolidate_turn_interval`, `consolidate_every_minutes`
  (raise to reduce frequency), `auto_consolidate=false` (capture stays free,
  consolidation manual), and a future cheaper consolidation model.

---

## Security & threat model

- **`secret` items are never written into `MEMORY.md`** (it's injected into
  prompts) and never returned by `recall` — defense in depth on top of the dreamer
  never auto-activating sensitive candidates.
- **Prompt-injection** candidates are rejected with an audit trail (`scan_context_file`
  patterns from `flowly/cron/guard.py`), never activated. The scan is applied on
  **both** write paths — the dreamer and the live `ingest_append`/`ingest_kg_fact`
  hook — and **fails closed**: if the scanner errors, the candidate is routed to
  `needs_review` rather than trusted as clean.
- **Autonomous live writes are not auto-trusted.** A `memory_append` /
  `knowledge_graph` write the agent makes during its own background run
  (heartbeat / cron / subagent — detected via the source session key) lands in
  `needs_review`, not `active`; only real user-channel writes stay auto-active.
- **The consolidation LLM only proposes.** `apply_operations` validates every op
  and never deletes — a hallucinated op targeting a missing/non-active id is
  skipped, not obeyed. The LLM cannot escalate beyond merge/supersede/stale on
  existing active items.
- **Subagent blocklist intact.** Passing a `HookRegistry` to `SubagentToolRegistry`
  does not relax `_BLOCKED_SUBAGENT_TOOLS`; self-review still gets only
  `memory_append` + `knowledge_graph`. The hook observes results; it does not grant
  tools.
- **No new external surface.** The governance DB is local; no network. The only LLM
  calls are consolidation, through the already-authenticated provider.

---

## Configuration

`agents.defaults.memory_dreaming` (`MemoryDreamingConfig`):

```
enabled                    = true    # master switch (ON by default — see Rollout)
commit_mode                = "selective"
auto_floor                 = 0.80    # ≥ → auto-active
review_floor               = 0.55    # < → rejected
auto_consolidate           = true
consolidate_turn_interval  = 50      # 0 = off
consolidate_every_minutes  = 30      # 0 = off
freeze_injected_memory     = false   # F3b — flip ON only after a measured cache-read gain
# dreamer triggers (now live): idle_minutes, daily_time, turn_interval,
# max_messages_per_run, auto_floor, review_floor
```

Config keys are camelCase on disk (`memoryDreaming`, `consolidateTurnInterval`),
converted to snake_case by `flowly/config/loader.py::convert_keys`.

---

## Rollout & defaults

The feature is **on by default** and auto-enables on update with **no migration
or detection code** — the config default *is* the rollout mechanism:

- `MemoryDreamingConfig.enabled` defaults to `true`.
- No existing `config.json` carries a `memory_dreaming` key, so the Python loader
  fills the default on load → the feature is active on the next gateway start.
- This holds for **both** distribution paths: the CLI/bot (Python package update)
  and Desktop (the Nuitka-compiled Python embedded in the Electron app uses the
  same `schema.py`, so the same default applies).

Why the Desktop needs zero changes: `flowly-desktop` never writes the
`memoryDreaming` key. `flowlyai-service.ts::writeConfig` merges only the specific
fields it knows (providers, model, channels…), and `buildDefaultConfig()` (the
TS-side default scaffold deep-merged by `ensureValidConfig`) contains no memory
keys. So the key stays absent in `config.json` and the Python default always wins.

**Backward/forward compatible.** The top-level config model is `extra = "ignore"`,
so a *downgrade* (older bot/Desktop reading a config that gained the key) silently
ignores it rather than erroring. Missing key → default; empty/broken config →
`Config()` defaults + `.bak` recovery.

**Always-on by design.** Per product decision there is **no UI toggle** — smart
memory is treated like autosave. `enabled` remains a hidden config kill-switch for
support/power users.

**Non-destructive on first enable.** Nothing auto-runs `flowly memory migrate`;
existing `MEMORY.md`/KG/daily notes are untouched. The first governed write appends
the generated block via `splice` (manual content preserved). Users who want their
pre-existing `MEMORY.md` entries imported as governed items run
`flowly memory migrate` (backs up the original first).

**Cost note.** Full-auto includes autonomous consolidation, a background LLM call
on its triggers when the store is dirty — recurring token spend on the user's
provider. `auto_consolidate = false` keeps capture/supersede/MEMORY.md (all free +
deterministic) while making consolidation manual.

---

## Extension points

- **New `kind`.** Add to `VALID_KINDS` in `governance.py` and to the display order
  in `summary.py::_KIND_ORDER`. Decide whether it decays (temporal-like) in
  `calibration.py`.
- **New consolidation op.** Add a constant to `consolidate.py::VALID_OPS`, handle it
  in `apply_operations`, and document it in the `PROMPT`. Keep the invariant: the
  op only moves an existing active item to a non-`active` state; never delete.
- **New autonomous trigger.** Call `await loop._maybe_consolidate("<name>")` from
  your trigger; the dirty + lock gating is centralized there.
- **Swap the consolidation model.** `memory_consolidate.py` uses `self._model`
  (the agent's model). Thread a dedicated cheaper model id through
  `_maybe_enable_memory_governance` → `MemoryConsolidateTool(model=…)` and the
  CLI's `consolidate_cmd`.
- **Change capture confidence / policy.** Live ingest scores live in
  `coordinator.ingest_*`; the dreamer's commit policy (`_decide`) and thresholds
  live in `dreamer.py` / config.

---

## Wiring the offline dreamer

**Implemented.** The extractor is `flowly/memory/extractor.py::SubagentExtractor`;
the triggers are `AgentLoop._start_dreamer_timers` (construction + idle/daily timers),
`_maybe_run_dreamer` (gated runner, run in a worker thread), and the turn counter in
`_maybe_spawn_review`. The recipe it followed:

1. **DeltaSource:** `SessionIndexDeltaSource(state_dir/"session_index.sqlite")`
   (already implemented; reads `messages WHERE id > watermark`).
2. **Extractor:** implement `Extractor.extract(delta) -> [Candidate]` by spawning a
   **tool-less, structured-output subagent** (the engine owns all writes, so the
   extractor needs no memory/exec/cron tools — it just returns `Candidate`s). The
   subagent's restricted toolset pattern is in `subagent.py`.
3. **Service:** `MemoryDreamerService(gov, delta_source, extractor, calibrate=True,
   kg_mirror=SqliteKGMirror(kg_path), on_committed=lambda: regenerate_memory_md(...))`.
4. **Trigger:** call `service.run()` from idle/daily (it self-locks via
   `dreamer_lock` and advances `dreamer_watermark` only after commit, so it is
   single-runner and crash-resumable).

The dreamer's `run()` does: acquire lock → read delta since watermark → extract →
for each candidate: injection-scan, dedup (touch+bump), contradiction arbitration
(explicit + ≥ auto_floor wins → supersede loser + KG mirror; else needs_review),
commit by policy → advance watermark → `on_committed`. It differs from live
consolidation in that it *re-reads session history* to extract memories the agent
never explicitly saved.

---

## What is **not** wired

Honest scope so nobody assumes more than is true:

- ~~`MemoryDreamerService` is not connected to a live trigger~~ — **now wired.** A
  streaming-provider `SubagentExtractor` (`flowly/memory/extractor.py`) feeds the
  engine, and `AgentLoop` fires it on idle / daily / turn (the previously-dead
  `idle_minutes` / `daily_time` / `turn_interval` config fields are now live).
  Remaining: the extraction *prompt* is v1 (quality tuning), and a manual
  `memory.dream` RPC + "Learn from chats" UI action is still to come.
- **Consolidation model** is the agent's main model (often a slow reasoning model,
  ~40-90s/pass). A faster model for this structured task is a config change away.
- **Consolidation is conservative** — it leaves borderline notes and pre-existing
  self-referential garbage triples (the `subject==object` filter only prevents
  *new* ones). Clear those with `flowly memory reject <id>` or tune the prompt.
- **Desktop UI** — no "what Flowly remembers" panel and (by decision) no toggle.

Now wired (formerly listed here): **`MEMORY.md` regen coalescing** (F3a, see
Performance), the **trust feedback loop** (F2 — `ingest_feedback` + `memory_feedback`
table + confidence-ordered rendering/recall + the `memory_recall`/`memory_feedback`
tools and `flowly memory feedback` CLI), and **skill self-improvement** (the
former "P5 trajectory miner" generalized into a full skill-governance subsystem
that *auto-applies* skill ops — see
[`skill-self-improvement-architecture.md`](skill-self-improvement-architecture.md)).
The frozen injected-memory snapshot (F3b) is wired but ships **flag-gated OFF**.

---

## Design decisions (rationale)

- **Wrapper over a parallel store.** A `MemoryItem` references a KG triple rather
  than copying it. Flowly already had 3 substrates; a 4th *fact* store would mean
  4-way sync. Wrapper keeps the KG as system-of-record and makes the whole layer
  droppable (delete the gov DB → old behavior).
- **Single-writer, no CAS.** All writers are in-process; a `threading.RLock` +
  `asyncio.Lock` is sufficient and far simpler than claim/heartbeat machinery.
- **LLM proposes, code applies.** Consolidation correctness can't depend on the LLM
  behaving — `apply_operations` validates and never deletes, so a bad proposal is
  inert.
- **Calibrated confidence over raw LLM score.** Self-reported confidence is noise;
  signals (explicit/repeat/recency/conflict) are observable and stable.
- **Capture is dumb on purpose.** The live hook records faithfully and only handles
  same-key supersede; semantic judgement is deferred to the consolidation LLM,
  where a wrong call is reversible and audited.
- **In-gateway consolidation tool, not a CLI LLM call.** The Flowly proxy 504s on
  long non-streamed completions; reusing the loop's authenticated streaming
  provider is the only reliable path.
- **On by default, no toggle.** Treated as a quality feature (like autosave). The
  one real cost — autonomous consolidation tokens — is mitigated by dirty-gating
  and is config-tunable; `enabled` stays as a hidden kill-switch.

---

## Runbook / FAQ

- **"`flowly memory list` is empty but the agent clearly remembered things."** You're
  reading the wrong `state_dir`. Use the same profile as the gateway
  (`FLOWLY_PROFILE=…`); the DB is under `get_data_dir()`, not `workspace/.flowly_state`.
- **"Consolidation says 'no operations proposed (0 chars)'."** The proxy returned an
  empty stream; the tool retries 3×. Persistent emptiness ⇒ check the provider /
  stream timeout (`FLOWLY_LLM_STREAM_TIMEOUT_SECONDS`). The CLI path is flakier than
  the in-gateway tool by design.
- **"A fact I wanted got retired."** `flowly memory undo <id>` (restores it, demotes
  the sibling, fixes the KG). `flowly memory audit`-style history via
  `gov.audit_log(id)`.
- **"How do I turn it off for a user?"** Set `memoryDreaming.enabled=false` (whole
  feature) or `autoConsolidate=false` (keep free capture, drop background LLM) in
  `config.json`.
- **"Will updating break existing users?"** No — see Rollout. Off-by-absence config
  is filled by the new default; nothing auto-migrates; existing `MEMORY.md`/KG are
  preserved.
- **"Test it without touching my real memory."** `scripts/memlab.sh` /
  `scripts/memchat-setup.sh` — isolated profiles. `FLOWLY_PROFILE` is the only
  isolation lever that survives `entry.py`'s `set_profile`.

---

## Testing

- Unit/integration: `tests/test_memory_*.py` — governance store + status machine,
  summary/migration, dreamer engine, calibration/supersede, coordinator, the
  `post_tool_call → governance` integration (incl. the subagent registry path),
  and consolidation parse/apply. Run:
  `uv run --extra dev python -m pytest tests/test_memory_*.py`.
- The deterministic core is fully covered; LLM-dependent paths are tested with fake
  proposers/extractors and verified live once against the Flowly-hosted model.
- Isolated manual testing **must not touch the real workspace.** Use
  `scripts/memlab.sh` (CLI sandbox) and `scripts/memchat-setup.sh` (gateway
  profile). `FLOWLY_PROFILE` is the only isolation lever that survives — the CLI's
  `entry.py` overwrites a bare `FLOWLY_HOME` on every run, and a profile alone does
  not isolate `workspace_path` (it defaults to the absolute `~/.flowly/workspace`);
  the scripts also drop a profile `config.json` whose workspace points inside the
  profile dir.
```
