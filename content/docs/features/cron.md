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

`every` jobs have a minimum interval of **60s**. One-shot `at` jobs are automatically capped to a single run (`repeat_times = 1`).

## Job storage and per-run archives

Jobs live in:

```text
~/.flowly/cron/jobs.json
```

The file has the shape `{ "version": ..., "jobs": [ ... ] }` with camelCase keys, and is written atomically (temp file + replace) so a crash mid-save never corrupts it.

Every run writes a Markdown transcript archive:

```text
~/.flowly/cron/output/<job_id>/<timestamp>.md
```

Archives are retained for **30 days** by default (override with `FLOWLY_CRON_RETENTION_DAYS`), pruned on gateway boot, and cascade-deleted when you remove the job.

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
- **`repeat_times`** — limit how many times a recurring job runs before it is deleted.

> [!IMPORTANT]
> Prompts are scanned for injection attempts before they are persisted.

## CLI: `flowly cron`

```bash
flowly cron list [--all]
flowly cron add --name "Morning digest" --message "Summarize my inbox" --every 86400 [--deliver --to <chat> --channel telegram]
flowly cron add --name "Standup ping" --message "Post standup reminder" --cron "0 9 * * 1-5"
flowly cron add --name "One-off" --message "Reminder" --at 2026-06-10T09:00:00
flowly cron remove <job_id>
flowly cron enable <job_id> [--disable]
flowly cron run <job_id> [--force --port 18790]
```

> [!NOTE]
> The CLI `--every` flag is an **integer number of seconds** (`86400` = 1 day, above).
> Human strings like `1d` or `30m` work only in the agent `cron` tool, not in the
> CLI `--every` flag.

`flowly cron run` delegates to a running gateway via `POST http://localhost:<port>/api/cron/run`, so a manual run goes through the same execution and locking path as a scheduled fire.

## Agent `cron` tool

The agent can manage jobs directly with the `cron` tool. Actions: `list`, `add`, `update`, `remove`, `enable`, `disable`, `status`. This is the surface that accepts the human schedule strings and the extras above.

> [!NOTE]
> There is no cron-specific slash command — manage cron via `flowly cron …` or the agent `cron` tool. (`/subagents` (alias `/subs`) toggles the background **subagent** sidebar, which is unrelated to cron jobs.)

## Reliability behavior

- **At-most-once on crash.** For recurring (`every` / `cron`) jobs, the next run time is advanced *before* the job executes, so a crash mid-run skips the run rather than repeating it. One-shot `at` jobs intentionally still fire after downtime.
- **Grace-window fast-forward.** If the gateway was offline and a recurring job is more than its grace window late (half the period, clamped 2 min–2 h), the schedule fast-forwards instead of cascading every missed run.
- **Double-fire guard.** An in-process flag plus a cross-process file lock (`~/.flowly/cron/.tick.lock`) ensure the timer, a manual `flowly cron run`, and any daemon never double-fire the same tick.
- **Inactivity watchdog.** A running job is killed only after a window of no agent activity (default 600s, override with `FLOWLY_CRON_TIMEOUT`), not on a fixed wall clock — long legitimate turns are not cut off prematurely.
- **Retry / backoff.** Failed runs retry up to `retry_max_attempts` with backoff defaults of `[30s, 60s, 5min]`, scheduled on later ticks.
- **Failure alerts.** After a number of consecutive failures (default 3), an alert fires, rate-limited by a cooldown (default 24h).

## Heartbeat (related, separate)

> [!NOTE]
> The heartbeat poller is a **separate** workspace-task mechanism, not part of cron. On an interval (default ~30 min, within configured active hours) it wakes the agent to read `HEARTBEAT.md` in the workspace and act on any actionable content. It is a task poller — not a health/liveness monitor. See the heartbeat configuration under `agents.defaults.heartbeat`.

## Related

- [Channels overview](../channels/overview.md)
- [Feature overview](overview.md)
- [Voice](voice.md)
- [CLI commands reference](../reference/cli-commands.md)
- [Slash commands reference](../reference/slash-commands.md)
