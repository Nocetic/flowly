---
title: Skill self-improvement
eyebrow: Features
description: Opt-in. Flowly mines its own conversations for recurring procedures and writes them up as reusable skills, then keeps the library tidy — every change snapshotted, archive-only, and reversible.
---

Most agents only use the skills you give them. With self-improvement turned on,
Flowly also **grows and grooms its own skill library**: it notices procedures you
repeat, writes them up as [skills](/docs/features/skills), and later consolidates
overlapping ones and archives the dead weight. It runs in the background while
you're not using it, and every change is snapshotted so nothing is ever lost.

> [!NOTE]
> This is **off by default** — it's opt-in. Turn it on with
> `skill_improvement.enabled: true`. (Self-maintaining [memory](/docs/features/memory)
> is the on-by-default cousin; skills stay manual until you ask for this.)

For one-off, user-directed skill creation, use
[`/learn`](/docs/reference/slash-commands#learn) instead. `/learn` turns a
specific conversation, path, URL, or notes into a skill immediately; skill
self-improvement waits for repeated evidence across sessions before proposing or
applying changes.

## Two modes

| Mode | What it does | Cadence |
| --- | --- | --- |
| **mine** | Extract a new skill from a procedure that recurs across recent conversations. | every 6h |
| **curate** | Consolidate the library — merge narrow siblings into umbrella skills, archive stale ones. | every 12h |

A procedure has to actually *recur* before it becomes a skill: by default it must
appear at least **3 times** across at least **2 sessions** before mining proposes
it. That keeps one-off tasks out of the library.

## Safety rails

Self-improvement writes to your skill library, so it's deliberately conservative:

- **Pre-run snapshot + rollback.** Every apply takes a snapshot first; the last
  `snapshotKeep` (default 10) are retained so any change can be reverted.
- **Archive-only.** Curate never deletes — stale skills are archived, not removed.
- **Pinned protection.** Skills you pin are never touched by curate.
- **Dry-run.** Preview exactly what would change before anything is written.
- **Governed.** Like memory, changes go through a governance layer with an op log,
  so "why does this skill exist / why was it archived" is always answerable.

## Configuration

Set under `skillImprovement` in `~/.flowly/config.json`:

```json
{
  "skillImprovement": {
    "enabled": true,
    "mineEnabled": true,
    "curateEnabled": true,
    "mineEveryMinutes": 360,
    "curateEveryMinutes": 720,
    "minRepeatCount": 3,
    "minEvidenceSessions": 2,
    "staleAfterDays": 60,
    "snapshotKeep": 10
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for the whole subsystem. |
| `mineEnabled` | bool | `true` | Allow mining new skills (once `enabled`). |
| `curateEnabled` | bool | `true` | Allow library consolidation (once `enabled`). |
| `mineEveryMinutes` | int | `360` | Background mine cadence (6h). |
| `curateEveryMinutes` | int | `720` | Background curate cadence (12h). |
| `mineTurnInterval` | int | `0` | Also mine every N user turns (0 = time-based only). |
| `minRepeatCount` | int | `3` | A procedure must recur this many times to qualify. |
| `minEvidenceSessions` | int | `2` | …across at least this many distinct sessions. |
| `staleAfterDays` | int | `60` | Curate archives skills unused this long… |
| `staleMinUses` | int | `1` | …with fewer than this many uses. |
| `maxMessagesPerRun` | int | `1000` | Cap on how much history a single run scans. |
| `snapshotKeep` | int | `10` | How many rollback snapshots to retain. |

## Run it manually

Both modes are exposed on the CLI, and `--dry-run` shows what would happen without
writing anything:

```bash
flowly skill mine --dry-run     # preview proposed new skills
flowly skill mine               # mine + apply (with snapshot)
flowly skill curate --dry-run   # preview consolidation / archival
flowly skill curate             # curate + apply
```

The agent can also trigger this itself through the `skill_improve` tool
(`mode: "mine" | "curate"`) when it notices it's been repeating work.

## How mining works

Mining reads recent conversation deltas (capped at `maxMessagesPerRun`), looks for
procedures that clear the repeat/evidence thresholds, drafts a `SKILL.md` for each,
snapshots the library, and applies. The draft is a normal skill afterward — you can
read, edit, pin, or remove it like any other (see [Skills](/docs/features/skills)).

## Pitfalls

- **Nothing happens until you enable it.** `enabled` defaults to `false`.
- **It needs material to learn from.** With little history, mining proposes
  little — the evidence thresholds are intentionally strict.
- **Review the first runs.** Use `--dry-run` early to get a feel for what it
  proposes before letting it apply on a schedule.
