# Skill Self-Improvement Architecture

How Flowly **grows and maintains its own skill library** autonomously: it mines
recurring procedures from past conversations into new skills (creation), and
consolidates the library over time (merge narrow siblings into umbrellas, demote
session-specific detail into references, archive stale skills). Changes are
**auto-applied** — but under heavy safety rails: a pre-run snapshot + rollback,
**archive-only (never delete)**, dry-run preview, pinned-skill protection,
first-run deferral, a full op log, and per-op undo.

This is the deliberate symmetry to the memory layer
([`memory-governance-architecture.md`](memory-governance-architecture.md)): the
memory dreamer auto-applies consolidation (LLM proposes → deterministic validated
`apply_operations` → audited, never delete). The skill layer mirrors that exactly
for skills, in a new top-level package `flowly/skills/`.

> **Status (2026-06).** Built, unit-tested (deterministic core with fake
> proposers), and **verified live end-to-end**: a repeated "weekly sales report"
> request across sessions → real LLM proposed `weekly-sales-report-generator` →
> auto-applied (skill written, op logged, snapshot taken) → `undo` archived it
> (not deleted). **OFF by default** (`agents.defaults.skill_improvement.enabled`).

---

## Table of contents
1. [TL;DR](#tldr)
2. [Relationship to memory governance](#relationship-to-memory-governance)
3. [Why auto-apply (and the safety rails)](#why-auto-apply-and-the-safety-rails)
4. [Components & code layout](#components--code-layout)
5. [Storage & file inventory](#storage--file-inventory)
6. [Data model](#data-model)
7. [API reference](#api-reference)
8. [The flows](#the-flows)
9. [Live-verified end-to-end run](#live-verified-end-to-end-run)
10. [Concurrency model](#concurrency-model)
11. [Failure modes & recovery](#failure-modes--recovery)
12. [Security & safety invariants](#security--safety-invariants)
13. [Configuration](#configuration)
14. [Rollout & defaults](#rollout--defaults)
15. [CLI reference](#cli-reference)
16. [Extension points](#extension-points)
17. [What is not wired / known limitations](#what-is-not-wired--known-limitations)
18. [Design decisions (rationale)](#design-decisions-rationale)
19. [Runbook / FAQ](#runbook--faq)
20. [Testing](#testing)

---

## TL;DR

```
                       ┌─ mine (creation) ──────────────────────────────┐
session deltas         │ detect_signals (deterministic gate, no LLM      │
(session_index.sqlite) │   unless a procedure recurs ≥N× across ≥M sess) │
        │              │   → LLM proposes create ops                     │
        ▼              └─────────────────────────────────────────────────┘
   SkillImproveTool ──┤                                                   ├─▶ apply_ops
   (in-gateway,       │                                                   │   (snapshot → validate
    chat_stream)      └─ curate (consolidation) ─────────────────────────┘    → SkillManageTool
                        build_context(agent-created skills + usage)            → never delete
                        → LLM proposes merge/demote/archive ops                → log each op)
                                                                                      │
   telemetry hook: skill_view → usage.bump_use ; skill_manage write → mark dirty      ▼
   lifecycle: age-based staling (deterministic)                          op log + snapshots
                                                                          → undo / rollback
```

Two cost profiles: telemetry + staling + apply are deterministic and free; the
**proposal** step is an LLM call (mine/curate). detect_signals gates the LLM so
it never fires on a static or non-repeating history.

---

## Relationship to memory governance

| Memory layer (`flowly/memory/`) | Skill layer (`flowly/skills/`) |
|---|---|
| `GovernanceStore` (SQLite, RLock, WAL, audit) | `SkillOpLog` (op history + rollback ledger) |
| `consolidate.apply_operations` (LLM proposes → validated apply) | `apply.apply_ops` (same shape, snapshot-guarded) |
| `consolidate.parse_operations` | `proposer.parse_specs` |
| `MemoryDreamerService` (delta → candidates, watermark) | `miner.detect_signals` + the `skill_improve` tool's mine mode |
| `consolidate.build_context` | `curator.build_curate_context` |
| `MemoryGovernance` facade | `SkillGovernance` facade |
| `memory_consolidate` in-gateway tool (chat_stream + retry) | `skill_improve` tool (mode mine\|curate, chat_stream + retry) |
| dreamer temporal stale (deterministic) | `SkillLifecycle` age-based staling |
| `_maybe_consolidate` / `_consolidation_timer` | `_maybe_skill_pass` / `_skill_timer` |
| `memory_cmd.py` CLI | `skill_gov_cmd.py` CLI (`flowly skill`) |
| `touch_used` | `SkillUsageStore.bump_use` |

The patterns, retry workaround (Flowly proxy 504/empty on long non-streamed
calls), single-writer discipline, dirty-gating, and fire-and-forget timers are
reused verbatim.

---

## Why auto-apply (and the safety rails)

The reference agent auto-applies skill changes; the user chose the same posture
("like the reference") rather than a proposal-first approval queue. Auto-apply is
made safe the same way memory consolidation is — the LLM only *proposes*; a
deterministic, validated apply layer does the writing and can never delete:

- **Pre-run snapshot + rollback** — every pass tars the skills tree first;
  `flowly skill rollback` restores the whole tree.
- **Archive-only, never delete** — `apply.py` calls only `create/edit/write_file/
  archive` on `SkillManageTool`; archived skills move to `~/.flowly/skills_archive/`
  and are restorable. A test asserts `delete` is never referenced.
- **Per-op undo** — `flowly skill undo <op_id>` reverses one op
  (create→archive, archive→restore, merge→restore siblings + archive umbrella).
- **Agent-created only** — mine/curate operate solely on skills the apply layer
  itself stamped `provenance=agent-created`; the user's installed library
  (bundled/hub) is never touched.
- **Pinned protection** + **first-run deferral** + **dirty-gating** (curate only
  runs when skills changed) + **OFF by default**.

---

## Components & code layout

```
flowly/skills/                          (NEW package, parallel to flowly/memory/)
├── op_log.py     — SkillOpLog: op history + rollback ledger (SQLite, status
│                   machine applied→undone, audit, meta kv). The store.
├── snapshot.py   — SkillSnapshots: tar.gz the skills tree before a pass; restore.
├── apply.py      — SkillOpSpec + apply_ops: deterministic auto-apply, validate,
│                   never delete, log applied/failed. The ONLY writer of skills.
├── governance.py — SkillGovernance facade: apply_specs / log / undo / rollback /
│                   usage / archive / restore / run_staling / dirty flags.
├── miner.py      — detect_signals (deterministic recurrence gate) + MINE_PROMPT +
│                   reuses MessageRow/SessionIndexDeltaSource from memory dreamer.
├── curator.py    — build_curate_context + CURATE_PROMPT.
└── proposer.py   — parse_specs: fence-tolerant LLM-JSON → SkillOpSpecs.

flowly/agent/
├── skill_usage.py     — SkillUsageStore (~/.flowly/skills/.usage.json): use_count,
│                        last_used_at, created_at, provenance, pinned, state.
├── skill_lifecycle.py — SkillLifecycle: deterministic active→stale by age.
├── tools/skill_improve.py — in-gateway tool (mode mine|curate, dry_run): streams
│                            via the agent's authenticated provider + retry, parses,
│                            AUTO-APPLIES via the facade.
├── tools/skill_manage.py  — (MODIFIED) added archive/restore actions + dot-dir skip.
└── skills.py              — (MODIFIED) dotfilter: list_skills + manifest rglob skip
                             dot-dirs so .usage.json / archive never pollute prompt.

flowly/cli/skill_gov_cmd.py — `flowly skill usage|log|undo|rollback|archive|
                              restore|stale|mine|curate`. Registered as `skill`.
flowly/agent/loop.py        — _maybe_enable_skill_improvement, _skill_telemetry_post_tool,
                              _start_skill_maintenance_timer, _skill_timer, _maybe_skill_pass.
flowly/config/schema.py     — SkillImprovementConfig.
```

Dependency direction: `loop.py` / `skill_improve.py` / `skill_gov_cmd.py` →
`SkillGovernance` → `{SkillOpLog, apply_ops, SkillSnapshots, SkillUsageStore,
SkillLifecycle, SkillManageTool}`. `miner`/`curator`/`proposer` are leaf modules
feeding the tool.

---

## Storage & file inventory

Under `state_dir = get_data_dir()` (profile data dir) and `get_flowly_home()/skills`:

| Path | Written by | Notes |
|---|---|---|
| `<data_dir>/skill_governance.sqlite3` | `SkillOpLog` | op log + audit + meta (watermark/locks/dirty) |
| `~/.flowly/skills/.usage.json` | `SkillUsageStore` | per-skill telemetry; atomic replace |
| `~/.flowly/skills/<name>/` | `SkillManageTool` (via apply) | agent-created skills |
| `~/.flowly/skills_archive/<name>/` | archive action | archived skills (restorable); **outside** `skills/` so the scanner ignores them |
| `~/.flowly/skills_backups/<id>.tar.gz` | `SkillSnapshots` | pre-pass snapshots for rollback; keep last N |
| `<data_dir>/session_index.sqlite` | `SessionIndexer` (pre-existing) | the miner's delta source; **rebuilt on gateway startup** (see limitations) |

> The dotfilter fix in `skills.py` is **required**: without it, `.usage.json` and
> any dot-dir under `~/.flowly/skills` would be scanned as skills and pollute the
> system-prompt skill list + the prompt snapshot manifest.

---

## Data model

### `skill_ops` (op log + rollback ledger)

```sql
CREATE TABLE skill_ops (
    id              TEXT PRIMARY KEY,        -- so_<uuid12>
    kind            TEXT NOT NULL,           -- create | merge | archive | demote
    status          TEXT NOT NULL,           -- applied | failed | undone
    targets         TEXT NOT NULL DEFAULT '[]',   -- JSON list of skill names
    draft_name      TEXT,                    -- new/umbrella skill name
    applied_content TEXT,                    -- SKILL.md written
    applied_files   TEXT NOT NULL DEFAULT '{}',   -- JSON {path: content}
    rationale       TEXT, evidence TEXT,
    snapshot_id     TEXT,                    -- the pre-pass snapshot (rollback anchor)
    created_at TEXT, updated_at TEXT
);
CREATE TABLE skill_op_audit ( id PK, op_id→skill_ops, from_status, to_status,
                              actor, reason, at );   -- one row per transition
CREATE TABLE skill_meta (key PK, value);   -- mine_watermark, curate_dirty, locks
```

Status machine (enforced; illegal transitions raise `InvalidTransition`):
`applied → undone`; `failed` terminal; `undone` terminal.
Actors: `miner`, `curator`, `user`, `system`.

### `.usage.json` (telemetry)

Per skill: `use_count`, `last_used_at`, `created_at`, `provenance`
(`agent-created | bundled | workspace`), `pinned`, `state`
(`active | stale | archived`). `bump_use` reactivates a stale skill. Only
`use_count` is bumped on the hot path (skill load / skill_view) to avoid write
amplification in `build_skills_summary`.

### Lifecycle (deterministic, `SkillLifecycle`)

`active → stale` when unused longer than `stale_after_days` (default 60), not
pinned, and (default) only for `agent-created` provenance — a bundled skill's
non-use ≠ irrelevance. **Never auto-archives** (archive is a curator op or an
explicit CLI action). Stale skills are filtered out of the system-prompt skill
list and become prime archive candidates for the curator.

---

## API reference

### `SkillOpLog` (`op_log.py`)
```python
add_op(*, kind, status='applied', targets=[], draft_name='', applied_content='',
       applied_files={}, rationale='', evidence={}, snapshot_id='', actor, reason) -> SkillOp
transition(op_id, to, *, actor, reason) -> SkillOp     # enforced machine
get(op_id) / list_ops(*, status=None, limit=None)
get_meta(key, default=None) / set_meta(key, value)     # watermark, dirty, locks
```

### `SkillUsageStore` (`skill_usage.py`)
```python
bump_use(name, *, provenance=None)   # reactivates stale; best-effort
set_state / set_pinned / set_provenance / forget
get(name) -> SkillUsage | None ; all() -> list[SkillUsage]
```

### `SkillLifecycle(usage, *, stale_after_days, stale_min_uses, agent_only, now).run() -> LifecycleResult`

### `SkillSnapshots(skills_dir, backups_dir, keep)`
```python
snapshot(reason='') -> snap_id | None
restore(snap_id) -> bool            # snapshots current tree first, then restores
list_snapshots() -> [id...]
```

### `apply.py`
```python
@dataclass SkillOpSpec(kind, targets=[], draft_name='', draft_content='',
                       draft_files={}, rationale='', evidence={})
apply_ops(specs, *, skill_manage, op_log, snapshots, usage, actor, reason) -> ApplyResult
    # snapshot once → apply each → log applied/failed. Validates; never deletes.
```

### `SkillGovernance` facade (`governance.py`)
```python
await apply_specs(specs, *, actor, reason) -> ApplyResult
list_ops(*, status=None, limit=50)
await undo(op_id) -> str            # reverse one op (create→archive, archive→restore,
                                    #   merge→restore siblings+archive umbrella;
                                    #   demote→rollback its snapshot)
rollback(snapshot_id=None) -> str   # whole-tree restore (latest if None)
usage_report() ; run_staling()
await archive(name) / await restore(name)
mark_dirty / is_dirty / clear_dirty  # curate_dirty
```

### miner / curator / proposer
```python
detect_signals(delta, *, min_evidence_sessions, min_repeat_count) -> MinedSignals | None
build_curate_context(skill_rows) -> {skills}
parse_specs(raw) -> [SkillOpSpec]   # fence-tolerant; per-kind sanity; never raises
```

### `SkillImproveTool` (`tools/skill_improve.py`)
`execute(mode='mine'|'curate', dry_run=False)`. Mine: read delta since watermark →
`detect_signals` (skip LLM if no signal) → LLM → `parse_specs` → apply (unless
dry_run) → advance watermark. Curate: build context from **agent-created** skills
→ LLM → parse → apply. Streams via `provider.chat_stream` with 3× retry-on-empty.

---

## The flows

### Telemetry (always-on when enabled)
`_skill_telemetry_post_tool` (a `post_tool_call` hook): `skill_view` → `bump_use`;
`skill_manage` create/edit/patch/archive → `mark_dirty` (so curate has work).

### Mine (creation)
```
read session_index delta since mine_watermark
  → detect_signals: repeated user requests across ≥min_evidence_sessions sessions,
    ≥min_repeat_count total  (else: advance watermark, return — NO LLM)
  → MINE_PROMPT(signals) → chat_stream(retry) → parse_specs (create ops)
  → apply_ops (snapshot → SkillManageTool create + write_file; stamp provenance)
  → advance watermark
```

### Curate (consolidation)
```
build_curate_context(agent-created skills + usage + lifecycle state)
  → CURATE_PROMPT → chat_stream(retry) → parse_specs (merge/demote/archive)
  → apply_ops (merge: create umbrella + archive siblings; demote: edit + write_file
    references; archive: archive)  → clear curate_dirty
```

### Autonomy (background, OFF by default)
`_start_skill_maintenance_timer` (from `run()`) launches `_skill_timer("mine",
mine_every_minutes)` and `_skill_timer("curate", curate_every_minutes)` when those
are >0. `_maybe_skill_pass` is dirty-gated (curate), `asyncio.Lock`-guarded (no
overlap), fire-and-forget (never blocks a turn). Low frequency by design
(defaults 360m / 720m).

### Undo / rollback
`flowly skill undo <op>` reverses one op surgically; `flowly skill rollback [--id]`
restores the whole tree from a snapshot. All audited.

---

## Live-verified end-to-end run

Reproduced in an isolated `memchat` profile (thresholds lowered for the demo):

```
# repeated procedure in history: "haftalık satış raporunu hazırla" ×3 across 2 sessions
$ flowly skill mine --dry-run
Proposed (dry-run, not applied):
- create weekly-sales-report-generator: This recurring weekly sales report
  preparation task is requested repeatedly across sessions, so a standardized
  skill eliminates redundant manual data processing...
$ flowly skill mine            → Skill mine: applied=1 failed=0   (skill written, op logged, snapshot taken)
$ flowly skill undo so_0b16…   → undone so_0b16… (create)         (skill archived, NOT deleted)
```

This exercised the full path: deterministic recurrence gate → real LLM proposal →
parse → snapshot → apply (write) → op log → undo (archive). The deterministic core
is additionally covered by `tests/test_skill_*.py` with fake proposers.

---

## Concurrency model

- **Single writer** for the op log (one `SkillOpLog`/SQLite conn behind an
  `RLock`); `.usage.json` writes are lock + atomic-replace.
- **Autonomous passes serialized** by an `asyncio.Lock` (`_skill_lock`); a pass
  bails if the lock is held; curate re-checks dirty.
- **Snapshot before mutate** makes a pass atomically reversible even if it dies
  mid-way (rollback to the pre-pass snapshot).
- **Mine watermark** advances only after the pass; a crash re-reads the same delta
  (idempotent — dedup against existing skills at apply).

---

## Failure modes & recovery

| Failure | Behavior |
|---|---|
| LLM empty / 504 | `chat_stream` retried 3×; if still empty → "no ops proposed", no writes. (Same Flowly-proxy flakiness as memory consolidate.) |
| A proposed op invalid / write fails | logged `failed` (not `applied`), surfaced in `ApplyResult.errors`; other ops in the pass still apply. |
| Bad auto-applied skill | `flowly skill undo <op>` (archive) or `flowly skill rollback` (whole tree). Never deleted. |
| `_skill_telemetry_post_tool` raises | caught + logged; tool result unaffected. |
| Snapshot fails | apply still proceeds (logged); rollback for that pass unavailable — bounded risk since ops are archive-only. |

---

## Security & safety invariants

- **Never delete** — apply only archives; `delete` is never called (test-asserted).
- **Agent-created only** — mine/curate scope to `provenance=agent-created`; the
  user's installed bundled/hub library is never reasoned over or touched. (This
  also fixed a live issue where curate reasoned over 68 pre-installed skills.)
- **Pinned** skills are exempt from staling and curation.
- **content_guard + frontmatter validation** run on every skill write (inherited
  from `SkillManageTool`).
- **OFF by default**; first-run deferral avoids day-1 mutation.
- Note: `skill_manage` is intentionally **not** added to the subagent blocklist —
  the no-auto-write guarantee is structural (the miner uses the apply layer, never
  a skill-writing subagent), and blocking it would regress legitimate
  user-directed subagent skill work.

---

## Configuration

`agents.defaults.skill_improvement` (`SkillImprovementConfig`):
```
enabled               = false   # master switch (OFF until rollout)
mine_enabled          = true    # creation source
curate_enabled        = true    # consolidation source
mine_turn_interval    = 0       # also run every N turns (0=off, timer-preferred)
mine_every_minutes    = 360     # background mine timer (0=off) — low frequency
curate_every_minutes  = 720
stale_after_days      = 60
stale_min_uses        = 1
max_messages_per_run  = 1000
min_evidence_sessions = 2       # don't propose a skill from a single session
min_repeat_count      = 3       # repeated-procedure threshold
snapshot_keep         = 10
```
camelCase on disk (`skillImprovement`, `mineEveryMinutes`, …).

---

## Rollout & defaults

OFF by default (`enabled=false`) — skill changes are higher-stakes than memory, so
unlike the memory layer this does not auto-enable on update. Turn it on per
profile/user. Backward/forward compatible via the config model's `extra="ignore"`.
When enabled, the autonomous timers are low-frequency and the apply path is
snapshot+rollback safe.

---

## CLI reference

```
flowly skill usage                 # per-skill counters + lifecycle state
flowly skill log [--status]        # op history
flowly skill undo <op_id>          # reverse one applied op
flowly skill rollback [--id]       # whole-tree restore from snapshot (latest if no --id)
flowly skill archive <name>        # archive a skill (restorable)
flowly skill restore <name>
flowly skill stale                 # run deterministic age-based staling
flowly skill mine   [--dry-run]    # creation pass (needs LLM provider)
flowly skill curate [--dry-run]    # consolidation pass (needs LLM provider)
```
`undo/rollback/archive/restore/stale/usage/log` are pure file ops (offline);
`mine/curate` stream via the active provider (same 504-avoiding pattern as
`memory consolidate`).

---

## Extension points

- **New op kind** → add to `op_log.VALID_KINDS`, handle in `apply._apply_one` +
  `governance.undo`, document in the proposer prompts. Keep never-delete.
- **Smarter detect_signals** → it's a pure function over `MessageRow`s; richer
  signals (tool-sequence n-grams, correction patterns) plug in there without
  touching the LLM/apply layers.
- **Faster/cheaper proposer model** → thread a dedicated model into
  `SkillImproveTool`/`_maybe_enable_skill_improvement` (the consolidation model is
  the agent's main model today; a fast model would cut pass latency).
- **Proposal-first variant** → if a future product wants approval gating, add a
  `proposed` status to the op log and gate `apply_ops` behind `flowly skill accept`.

---

## What is not wired / known limitations

- **session_index rebuild cadence** — `session_index.sqlite` (the miner's delta
  source) is rebuilt on gateway *startup*; messages from the current run aren't
  mined until the next restart. (For background timers over long-lived gateways
  this is fine; for instant manual demos, restart first.)
- **LLM proposal quality** is model-dependent and subject to the Flowly proxy's
  intermittent empty-stream (retry mitigates). Verify per model.
- **demote undo** isn't surgical — it rolls back the op's snapshot (whole tree to
  that point). create/archive/merge undo are surgical.
- **Curate model latency** — large prompts are slow; scoping to agent-created
  skills keeps the prompt small.
- No Desktop UI; no per-op approval queue (auto-apply by design).

---

## Design decisions (rationale)

- **Auto-apply + rails over proposal-first** — user decision ("like the
  reference"). Made safe by snapshot/rollback + never-delete + undo, exactly as
  memory consolidation is auto-applied safely.
- **LLM proposes, code applies** — correctness can't depend on the model; the
  validated `apply_ops` is the only writer and can't delete.
- **Deterministic recurrence gate before any LLM** — keeps token cost zero on
  static/non-repeating histories; the LLM only sees genuine recurrences.
- **Agent-created scope** — the agent curates *its own* skills, never the user's
  installed library (the reference's bundled/hub off-limits model).
- **Archive outside `skills/`** — sidesteps the skill scanner entirely (vs. a
  dotfile inside, which needed the dotfilter fix anyway for `.usage.json`).
- **Mirror the memory layer** — same stores/patterns/CLI shape so the two
  subsystems are learnable as one.

---

## Runbook / FAQ

- **"`skill mine` says no recurring procedures."** Correct unless a user request
  recurs ≥`min_repeat_count` across ≥`min_evidence_sessions` sessions in the
  *indexed* history. Remember session_index rebuilds on restart.
- **"`skill mine` says no new conversation."** The mine watermark caught up; only
  new indexed messages are mined.
- **"curate hangs ~3 min."** It was reasoning over the installed library — fixed
  to agent-created only. If still slow, it's the slow proposer model + proxy
  retries; use `--dry-run` and/or a faster model.
- **"A skill got created I don't want."** `flowly skill undo <op_id>` (archives
  it) or `flowly skill rollback`. Nothing is ever deleted.
- **"Turn it off."** `agents.defaults.skillImprovement.enabled=false`.
- **Test safely** — isolated profile via `scripts/memchat-setup.sh` +
  `FLOWLY_PROFILE`; never the real workspace.

---

## Testing

`tests/test_skill_foundation.py` (usage, lifecycle, op_log status machine,
snapshot round-trip, config, archive/restore action), `tests/test_skill_apply.py`
(real `SkillManageTool` over tmp; create/merge/demote/archive; **asserts no
delete**; failure→failed; undo; rollback), `tests/test_skill_miner.py`
(detect_signals thresholds, parse_specs fence/garbage/invalid filtering,
build_curate_context, the `skill_improve` tool mine apply + dry-run with a fake
provider). The LLM seam is faked; live proposal quality is verified manually
(see [Live-verified end-to-end run](#live-verified-end-to-end-run)). Run:
`uv run --extra dev python -m pytest tests/test_skill_*.py`.
```
