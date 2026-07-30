---
title: File layout
eyebrow: Reference
description: Everything Flowly stores lives under ~/.flowly. This is the map — config, workspace, memory, skills, credentials, and the SQLite databases — useful for backups, debugging, and self-hosting.
---

Flowly keeps almost all of its state in one directory: **`~/.flowly/`**
(override with `FLOWLY_HOME`; named profiles live under
`~/.flowly/profiles/<name>/`). Buzz identity ownership is the deliberate
exception: its cross-profile locks always live under the canonical
`~/.flowly/locks/buzz/`, so isolated profiles cannot accidentally consume the
same relay/identity pair twice.

## Top level

| Path | What it is |
| --- | --- |
| `config.json` | Main configuration (camelCase keys). The one file you edit by hand. |
| `.env` | Secrets / environment overrides loaded at startup, including a Desktop/setup-managed Buzz key as `BUZZ_PRIVATE_KEY`. |
| `workspace/` | Context files, memory, skills, personas — see below. |
| `credentials/` | OAuth tokens and optional channel credential files (for example `gmail.json` or `buzz.json`, mode `0600`). |
| `plugins/` | User-installed [plugins](/docs/features/plugins). |
| `cron/` | Scheduled-job data. |
| `plan-mode/` | [Plan mode](/docs/features/plan-mode) state: per-session plans (`<session>/plan_<id>.json` plus an append-only `plan_<id>.revisions.log`) and `sticky.json`, which conversations have the standing mode on (what makes the mode survive restarts). |
| `audit/` | Command + decision [audit log](/docs/features/audit-log). |
| `sessions/` | Session routing index and transcripts. |
| `assistants/` | Saved assistant / multi-agent definitions. |
| `locks/` | Runtime ownership locks, including Buzz relay/identity locks. |

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
| `locks/buzz/<sha256>.lock` | Prevents two Flowly processes from consuming the same Buzz relay/identity pair. The filename is derived from the relay and public key; the private key is not part of the file. |

## Buzz credential files

The recommended Buzz setup stores `BUZZ_PRIVATE_KEY` in the active profile's `.env`. For headless or existing Buzz CLI installations, the adapter can instead read a JSON credentials file. Resolution works as follows:

1. `BUZZ_PRIVATE_KEY`, when present;
2. otherwise, one explicitly selected file—`BUZZ_CREDENTIALS_FILE` before `channels.buzz.credentialsFile`; or
3. when no explicit file is set, `<FLOWLY_HOME>/credentials/buzz.json`, then files matching `~/.config/buzz/*credentials*.json`.

An explicit file shadows the automatic fallbacks. The JSON may contain `nsec`, `private_key_hex`, or `private_key`. Keep these files owner-readable only and include them in the same secret-handling policy as `.env`. See [Buzz](../channels/buzz.md#identity-and-credential-resolution).

## The install itself (not your data)

The install script installs Flowly's **code** separately from your data, under
`~/.local/share/flowly/`:

| Path | What it is |
| --- | --- |
| `repo/` | The git checkout `flowly update` fast-forwards (`git pull`). |
| `venv/` | The uv-managed virtualenv Flowly runs from (editable install of `repo/`). |

The `flowly` launcher is a symlink into this venv, placed on your PATH. None of
this is your data — you don't back it up; re-running the install script (or
`flowly update`) reproduces it from git. A packaged `uv tool` / `pip` install
lives wherever that tool keeps it instead, and has no `repo/`.

## Backing up

A backup is just a copy of `~/.flowly/` while the gateway is stopped — that's
your data. (Flowly's code lives elsewhere; see above.) To move to a new machine:
stop the gateway, copy the directory, and start it there. Keep `config.json`,
`.env`, and `credentials/` private — they hold your keys and tokens.

> [!TIP]
> Use `FLOWLY_HOME=/path/to/dir` (or `-p <profile>`) to run an isolated instance
> without touching your real `~/.flowly` — handy for testing, a second bot, or a
> headless server.
