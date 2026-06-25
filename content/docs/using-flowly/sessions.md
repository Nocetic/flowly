---
title: Sessions
eyebrow: Using Flowly
description: A session is one conversation thread with the agent. Flowly stores each session as a plain JSONL file on disk with a derived search index alongside it, and CLI sessions stay entirely local.
---

## Storage

Each session is **one JSONL file per session**:

```
<FLOWLY_HOME>/sessions/<key>.jsonl
```

`<FLOWLY_HOME>` defaults to `~/.flowly` (or your active profile directory). The session key is `channel:chat_id`, with the `:` replaced by `_` and sanitized into a safe filename.

File layout:

- The **first line** is a metadata record (`_type` = `metadata`, with `created_at`, `updated_at`, and a `metadata` object).
- Every subsequent line is one message (role, content, timestamp, plus tool-protocol fields like `tool_calls`, `tool_call_id`, and `name` where relevant).

Files are written atomically (temp file + replace). Loading is tolerant: corrupt lines are skipped (warnings on the first few, aborting only after many failures).

Session-wide token totals are rolled into the metadata record (`token_totals`, `turn_count`, `last_turn_usage`), so you can see cumulative usage per session.

## Search index

A derived SQLite full-text index sits next to the sessions:

```
<FLOWLY_HOME>/session_index.sqlite
```

It uses SQLite **FTS5** for full-text search across message content.

> [!NOTE]
> The index is a **derived copy** — if you delete it, it rebuilds from the JSONL files. The database runs in WAL mode, so `.sqlite-wal` / `.sqlite-shm` sidecar files appear next to it. Index updates are best-effort and never block a session save.

## Resuming sessions in the TUI

By default, opening the TUI **resumes your last session** so the conversation continues where you left off.

```bash
flowly                  # resume last session
flowly --new            # start a fresh session
flowly -s <key>         # open a specific session by key
```

See [Terminal UI](./tui.md) for in-session slash commands. Within a turn, `/retry` drops the last assistant chain and `/undo` drops the last full turn.

## Compaction

Long conversations eventually approach the model's context window. When a session nears that limit, Flowly **summarizes older history** so the conversation can continue without overflowing.

- Compaction triggers when the running token total exceeds `contextWindow − reserveTokensFloor`. With defaults (`contextWindow=128000`, `reserveTokensFloor=20000`) that's around 108,000 tokens.
- Before compacting, a **memory flush** pass can run (just under the threshold, controlled by `memoryFlush.softThresholdTokens`, default `4000`) so durable facts are written to memory before older messages are condensed.
- Compaction preserves a verbatim tail of the most recent messages and replaces older history with a structured summary (decisions, open TODOs, last request, tool results, exact identifiers).

These knobs live under `agents.defaults.compaction` in `config.json`. See [Configuration](./configuration.md) for the full key map.

## Local vs synced

> [!NOTE]
> **CLI sessions stay local** — the JSONL files and the search index live only on your machine; there is no sync. If you sign in to Flowly Cloud, your iOS and desktop chats sync across devices via the managed relay. Self-hosted CLI use with your own keys never leaves your machine.

## Related

- [Terminal UI](./tui.md)
- [Configuration](./configuration.md)
- [Running as a service](./service.md)
- [Personas](./personas.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
- [Environment variables](../reference/environment-variables.md)
