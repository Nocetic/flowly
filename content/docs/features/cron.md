---
title: Cron — scheduled tasks
eyebrow: Features
description: Run a prompt or a deterministic tool call on a schedule and deliver the result to a channel. Jobs run in-process inside the gateway and survive restarts.
group: Automation
---

## Schedule kinds

A job's schedule is one of three kinds:

| Kind | Meaning | Stored field |
| --- | --- | --- |
| `at` | One-shot at a wall-clock timestamp. | `at_ms` |
| `every` | Fixed repeating interval. | `every_ms` |
| `cron` | Standard 5-field cron expression, with optional IANA timezone. | `expr` (+ `tz`) |

### Strings the agent `cron` tool accepts

The agent-facing `cron` tool parses **prefixed human strings**, not free-form natural language:

- **Durations** (for `every`): `30s`, `5m`, `2h`, `1d`, `1w`. A bare number is seconds.
- **Times** (for `at`): `14:30` (today, or tomorrow if already past), `2024-12-25 09:00`, `tomorrow 09:00`, `+2h`.
- **Schedule dispatch:** a string starting with `every ` → `every`; starting with `at ` → `at`; otherwise it must be a valid 5-token cron expression (validated with `croniter`). Cron detection requires exactly 5 space-separated fields.

Examples:

```text
every 30m
at 14:30
at tomorrow 09:00
0 9 * * 1-5        # weekdays at 09:00
```

`every` jobs have a minimum interval of **60s**. One-shot `at` jobs are automatically capped to a single scheduled run (`repeat_times = 1`). After that run succeeds or reaches a terminal failure, the schedule becomes `completed` and remains in History.

## Lifecycle and run status

A job has one scheduling lifecycle: `scheduled`, `paused`, `completed`, or `archived`. This is separate from `lastStatus`, which describes the latest run (`ok`, `error`, or `skipped`). A recurring job can therefore have `lastStatus: ok` while remaining `scheduled`.

- A one-shot or N-run-limited schedule becomes `completed`, with no next run, after its final terminal attempt.
- A retrying failure remains `scheduled`; its persisted `nextRunAtMs` is the retry backoff deadline and survives gateway restart/reload.
- A manual run adds one result without consuming a schedule slot or changing a paused/completed schedule back to active.
- Editing a title or prompt does not reactivate terminal history. Replacing it with a materially different future schedule is an explicit reschedule.
- Removing a schedule archives it by default. Permanent purge is a separate operation.

## Job storage and per-run archives

Jobs live in:

```text
~/.flowly/cron/jobs.json
```

The file has the shape `{ "version": ..., "jobs": [ ... ] }` with camelCase keys, and is written atomically (temp file + replace) so a crash mid-save never corrupts it.

Every run writes a Markdown transcript and a small metadata record with a durable run ID:

```text
~/.flowly/cron/output/<job_id>/<started_ms>-<run_id>.md
~/.flowly/cron/output/<job_id>/<run_id>.json
```

Transcript bodies are retained for **30 days** by default (override with `FLOWLY_CRON_RETENTION_DAYS`). Set `FLOWLY_CRON_RETENTION_DAYS=0` to disable time-based expiry. When a body expires, its metadata remains so clients can distinguish an expired result from a job that never ran or a load error. Archived jobs keep their cron output. Only explicit permanent purge deletes the cron job record and its cron output; files produced elsewhere by a run are never deleted by cron-history cleanup.

Legacy timestamp-only Markdown archives remain readable. If an older release removed a job record but its output directory remains, startup recovers an inert archived History entry. It never invents an executable schedule from that output.

Synced jobs may also store `source` and `sourceId`. These fields are the stable ownership key for reconciliation; job names and delivery targets are not ownership. Firestore imports use `source = firestore:<uid>:<serverId>` and the task document ID as `sourceId`, keeping ownership account- and server-scoped.

## How a job runs and delivers

When a job fires, the gateway runs the job's prompt as an isolated agent turn (`session_key = cron:<job_id>`) and publishes the plain-text reply to a channel. The default delivery channel is **telegram**. A job captures its originating chat (platform, chat id, name, thread) at creation time, so output can route back to the chat that created it even after the session ends.

Two reply sentinels affect delivery:

- A `[SILENT]` reply suppresses delivery but is still archived.
- An internal error reply is recorded as a failed run.

### Extras (agent `cron` tool)

Beyond a plain prompt, a job may carry:

- **`tool_name` / `tool_args`** — run a deterministic tool directly instead of a prompt. For example a `voice_call` job (which must set `action: call` and a `to` number in E.164 format) places a scheduled outbound call.
- **`script`** — a pre-run script whose stdout is injected into the turn as a `## Script Output` section. Script paths must stay under `~/.flowly/workspace/`. A script returning `{"wakeAgent": false}` makes the job silent.
- **`skills`** — SKILL.md bodies injected as a system preamble before the turn.
- **`model`** / **`provider`** — override the model/provider for that job.
- **`repeat_times`** — limit how many scheduled terminal runs occur before the schedule completes and moves to History.

> [!IMPORTANT]
> Prompts are scanned for injection attempts before they are persisted.

## CLI: `flowly cron`

```bash
flowly cron list [--all]
flowly cron add --name "Morning digest" --message "Summarize my inbox" --every 86400 [--deliver --to <chat> --channel telegram]
flowly cron add --name "Standup ping" --message "Post standup reminder" --cron "0 9 * * 1-5"
flowly cron add --name "One-off" --message "Reminder" --at 2026-06-10T09:00:00
flowly cron remove <job_id> [--purge]
flowly cron output <job_id> [--run-id <run_id>] [--limit 10]
flowly cron enable <job_id> [--disable]
flowly cron run <job_id> [--force | -f | --no-force] [--port 18790]
```

> [!NOTE]
> The CLI `--every` flag is an **integer number of seconds** (`86400` = 1 day, above).
> Human strings like `1d` or `30m` work only in the agent `cron` tool, not in the
> CLI `--every` flag.

`flowly cron list --all` includes paused, completed, and archived History. `flowly cron remove` archives by default; add `--purge` only when the cron definition and retained cron output should be permanently deleted. `flowly cron output` can select an exact durable run ID and reports expired bodies.

`flowly cron run` delegates to a running gateway via `POST http://localhost:<port>/api/cron/run`, so a manual run goes through the same per-job locking path as a scheduled fire. It does not reactivate a paused/completed schedule. The CLI preserves its legacy default (`force=false`); use `--force` or the compatible `-f` alias to run a paused/completed job once.

## Agent `cron` tool

The agent can manage jobs directly with the `cron` tool. Actions: `list`, `add`, `update`, `remove` (archive), `purge`, `run`, `enable`, `disable`, `status`. This is the surface that accepts the human schedule strings and the extras above. `list` shows lifecycle labels; pass `include_archived: true` to include archived entries.

> [!NOTE]
> There is no cron-specific slash command — manage cron via `flowly cron …` or the agent `cron` tool. (`/subagents` (alias `/subs`) toggles the background **subagent** sidebar, which is unrelated to cron jobs.)

## Reliability behavior

- **Crash recovery, without an exactly-once claim.** For recurring (`every` / `cron`) jobs, the next run time is advanced before execution. A run's output metadata is persisted before final job state; startup reconciles a newer metadata record if the final jobs file save failed. A one-shot with no execution evidence remains due after downtime. External side effects cannot be made globally exactly-once.
- **Grace-window fast-forward.** If the gateway was offline and a recurring job is more than its grace window late (half the period, clamped 2 min–2 h), the schedule fast-forwards instead of cascading every missed run.
- **Double-fire guard.** An in-process per-job reservation plus a cross-process tick lock (`~/.flowly/cron/.tick.lock`) prevent overlapping manual/timer runs for the same job in one gateway and duplicate scheduler ticks across gateway processes.
- **Inactivity watchdog.** A running job is killed only after a window of no agent activity (default 600s, override with `FLOWLY_CRON_TIMEOUT`), not on a fixed wall clock — long legitimate turns are not cut off prematurely.
- **Retry / backoff.** Failed runs retry up to `retry_max_attempts` with backoff defaults of `[30s, 60s, 5min]`, scheduled on later ticks.
- **Failure alerts.** After a number of consecutive failures (default 3), an alert fires, rate-limited by a cooldown (default 24h).
- **Persistence before notification.** `cron.completed` and run-targeted notifications are emitted only after the transcript metadata and final jobs state are saved. Delivery errors remain separate from agent execution errors.

## Heartbeat (related, separate)

> [!NOTE]
> The heartbeat poller is a **separate** workspace-task mechanism, not part of cron. On an interval (default ~30 min, within configured active hours) it wakes the agent to read `HEARTBEAT.md` in the workspace and act on any actionable content. It is a task poller — not a health/liveness monitor. See the heartbeat configuration under `agents.defaults.heartbeat`.

## Related

- [Channels overview](../channels/overview.md)
- [Feature overview](overview.md)
- [Voice](voice.md)
- [CLI commands reference](../reference/cli-commands.md)
- [Slash commands reference](../reference/slash-commands.md)
