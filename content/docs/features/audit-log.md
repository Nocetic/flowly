---
title: Audit Log
eyebrow: Features
description: A local, append-only record of what the agent did — every run and tool call — on disk as daily JSON lines you fully own.
---

Flowly keeps a plain, append-only **audit log** of what the agent actually did:
every tool it ran and every model call it made. Each record is one line of JSON,
written to a daily file on your own machine. Nothing is uploaded anywhere — the
log is yours to read, grep, or delete.

It's the answer to "what happened, and when?" — useful for debugging a weird run,
auditing tool usage, or just keeping an honest history of agent activity.

## What it is

The audit log is a set of **daily JSONL files** (one JSON object per line). The
logger is intentionally simple and best-effort: it appends records as activity
happens, never blocks the agent, and never raises an error back into normal
operation — if a write fails, it's silently skipped rather than interrupting your
session.

> [!NOTE]
> "JSONL" means **JSON Lines**: each line is a complete, self-contained JSON
> object. You can read it with any text editor, or process it line by line with
> tools like `jq`.

## What's recorded

Each line has a `type` field identifying the event, plus a `ts` timestamp (UTC,
ISO 8601) and usually a `session` key (`{channel}:{chat_id}`) so lines can be
grouped by conversation. The event types are:

- **`tool_call`** — a tool the agent executed. Fields: `tool` (name), `args`,
  `result_snippet` (first 300 characters of the output), `duration_ms`, and
  `success` (true/false).
- **`llm_call`** — a model API call. Fields include `model`, `prompt_tokens`,
  `completion_tokens`, `total_tokens`, `duration_ms`, `tool_choice`, and
  `iteration`. Optional fields appear only when relevant: `finish_reason`,
  `families`, `cache_read_tokens`, `cache_write_tokens`, `streamed`, `purpose`.
- **`overflow_recovery`** — a context-overflow recovery event. Fields:
  `tokens_before`, `tokens_after`, `messages_dropped`.
- **`key_rotation`** — an API-key rotation. Fields: `provider`, `reason`,
  `from_index`, `to_index`.

> [!NOTE]
> The audit log records **tool calls and model calls**, not your conversation
> transcript. It is not a chat history — there is no record of approval prompts
> or full message bodies. Tool results are truncated to a 300-character snippet,
> and sensitive argument keys (`password`, `token`, `secret`, `key`, `api_key`)
> are redacted to `***` before writing.

## Where it's stored

Records are written to daily files under your Flowly home:

```
<FLOWLY_HOME>/audit/YYYY-MM-DD.jsonl
```

By default `<FLOWLY_HOME>` is `~/.flowly`, so a file looks like
`~/.flowly/audit/2026-06-05.jsonl`. A new file is started each calendar day; the
filename's date comes from your local time.

Each file is created with `0600` permissions (read/write for the owner only), so
other users on the machine can't read your audit log.

## Retention

So the log doesn't grow without bound, Flowly prunes old files **once at gateway
startup**, using a two-tier policy:

1. **Age cap** — any daily file older than `retentionDays` is deleted.
2. **Size cap** — if the `audit/` folder is still larger than `maxSizeMb`, the
   oldest remaining files are deleted until the total is back under the cap.

Pruning only manages files named `YYYY-MM-DD.jsonl`. Anything else you put in
that folder (a manual export, for example) is left untouched. Like the logger,
retention is best-effort and never blocks startup.

> [!TIP]
> Set `retentionDays` to `-1` to disable the age cap, or `maxSizeMb` to `0` to
> disable the size cap. Set `audit.enabled` to `false` to skip pruning entirely —
> in that case files accumulate forever until you remove them yourself.

## Viewing it

In the terminal UI, open the **Activity** modal to browse recent activity:

- Press **`F2`**, or
- Type the **`/activity`** slash command.

The modal lists recent LLM and tool calls together with summary stats (including
the configured retention window).

## Configuration

The audit logger **always writes** records; these keys only control retention.
They live under the `audit` block in your config (camelCase on disk):

```json
{
  "audit": {
    "enabled": true,
    "retentionDays": 90,
    "maxSizeMb": 100
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Whether retention pruning runs at all. When `false`, files accumulate forever. |
| `retentionDays` | `90` | Delete daily files older than this many days. `-1` disables the age cap. |
| `maxSizeMb` | `100` | Keep the `audit/` folder under this size, deleting oldest files first. `0` disables the size cap. |

## Privacy

The audit log is **local-only**. Files live under your Flowly home, are owner-only
(`0600`), and are never synced or sent to any server. You're free to inspect them,
back them up, or delete them at any time.

## Related

- [Configuration](../using-flowly/configuration.md)
- [Slash commands reference](../reference/slash-commands.md)
- [Feature overview](overview.md)
- [Artifacts](artifacts.md)
