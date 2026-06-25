---
title: File layout
eyebrow: Reference
description: Everything Flowly stores lives under ~/.flowly. This is the map — config, workspace, memory, skills, credentials, and the SQLite databases — useful for backups, debugging, and self-hosting.
---

Flowly keeps all of its state in one directory: **`~/.flowly/`** (override with
`FLOWLY_HOME`; named profiles live under `~/.flowly/profiles/<name>/`). Nothing is
written outside it without your involvement.

## Top level

| Path | What it is |
| --- | --- |
| `config.json` | Main configuration (camelCase keys). The one file you edit by hand. |
| `.env` | Secrets / environment overrides loaded at startup. |
| `workspace/` | Context files, memory, skills, personas — see below. |
| `credentials/` | OAuth tokens (e.g. `gmail.json`, mode `0600`). |
| `plugins/` | User-installed [plugins](/docs/features/plugins). |
| `cron/` | Scheduled-job data. |
| `audit/` | Command + decision [audit log](/docs/features/audit-log). |
| `sessions/` | Session routing index and transcripts. |
| `assistants/` | Saved assistant / multi-agent definitions. |

## Workspace (`~/.flowly/workspace/`)

| Path | What it is |
| --- | --- |
| `AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `IDENTITY.md` | [Context files](/docs/using-flowly/workspace) injected every turn. |
| `memory/MEMORY.md` | Human-readable curated [memory](/docs/features/memory). |
| `memory/YYYY-MM-DD.md` | Daily notes. |
| `skills/` | Built-in + installed + agent-created [skills](/docs/features/skills). |
| `personas/` | [Persona](/docs/using-flowly/personas) definitions. |

## Databases

Flowly uses local SQLite files (WAL mode, so you'll also see `-wal` / `-shm`
sidecars):

| File | Holds |
| --- | --- |
| `memory_governance.sqlite3` | Memory lifecycle + audit trail (governance). |
| `knowledge_graph.sqlite3` | Temporal [knowledge graph](/docs/features/knowledge-graph) (triples). |
| `memory_index.sqlite` | Hybrid search index (embeddings + FTS). |
| `board.db` | The cross-channel task [board](/docs/features/board). |
| `artifacts.sqlite` | Version-tracked [artifacts](/docs/features/artifacts). |
| session store | Session history + full-text search. |

## Runtime / IPC files

| File | What it is |
| --- | --- |
| `gateway-api.json` | Local gateway token (loopback auth). |
| `electron-api.json` | Shared-secret handshake with Flowly Desktop (screenshots, perms). |
| `imessage-state.json` | iMessage channel watermark/state. |
| `desktop-client-id` | Stable id for the paired desktop client. |

## Backing up

A backup is just a copy of `~/.flowly/` while the gateway is stopped. To move to a
new machine: stop the gateway, copy the directory, and start it there. Keep
`config.json`, `.env`, and `credentials/` private — they hold your keys and
tokens.

> [!TIP]
> Use `FLOWLY_HOME=/path/to/dir` (or `-p <profile>`) to run an isolated instance
> without touching your real `~/.flowly` — handy for testing, a second bot, or a
> headless server.
