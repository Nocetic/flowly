---
title: Obsidian
eyebrow: Integrations
description: Give your agent first-class access to an Obsidian vault — search, read, list, and write notes, with optional context injection and governed ingestion of vault facts into long-term memory.
---

## What it does

Point Flowly at an Obsidian vault and it becomes a working surface for the agent:
it can **search** across notes, **read** them, **list** a folder, and **write**
new notes back into the vault. A local full-text index keeps search fast, and —
when you allow it — relevant snippets are injected into the conversation
automatically, and durable facts from the vault can flow into Flowly's governed
memory.

## Tools

| Tool | What it does |
| --- | --- |
| `obsidian_search` | Free-text search across the vault (local FTS index). |
| `obsidian_read` | Read a note (optionally the first *N* lines). |
| `obsidian_list` | List notes in a folder. |
| `obsidian_write` | Create or update a note in the vault. |
| `obsidian_append` | Append content to the end of an existing note. |

## Configuration

Set under `integrations.obsidian` in `~/.flowly/config.json`:

```json
{
  "integrations": {
    "obsidian": {
      "enabled": true,
      "vaultPath": "/Users/you/Documents/Obsidian Vault",
      "indexEnabled": true,
      "autoInject": "on_demand",
      "ingestionPolicy": "review_gated",
      "includeGlobs": ["**/*.md"],
      "excludeGlobs": [".obsidian/**", ".trash/**", ".git/**", "node_modules/**"],
      "maxNoteBytes": 1000000
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Turn the Obsidian tools on. |
| `vaultPath` | string | `""` | Absolute path to the vault. Empty → `OBSIDIAN_VAULT_PATH` env, then `~/Documents/Obsidian Vault`. |
| `indexEnabled` | bool | `true` | Build a local full-text index for fast search. |
| `autoInject` | `off` \| `on_demand` | `on_demand` | `on_demand` injects top-k vault snippets only when the message looks like it needs the vault (keyword-gated). `off` never auto-injects — the agent must call a tool. |
| `ingestionPolicy` | `manual_only` \| `review_gated` \| `selective_auto` | `review_gated` | How aggressively vault-derived facts may enter long-term memory (see below). |
| `includeGlobs` | string[] | `["**/*.md"]` | Which files are part of the vault surface. |
| `excludeGlobs` | string[] | `[".obsidian/**", ".trash/**", ".git/**", "node_modules/**"]` | Files to skip (private folders, templates, archives). Defaults already exclude Obsidian/Git internals. |
| `maxNoteBytes` | int | `1000000` | Skip notes larger than this (bytes). |

## Context injection

With `autoInject: "on_demand"` (the default), Flowly looks at each user message
and, when it reads as vault-relevant, pulls the top matching snippets into that
turn so the agent answers from your notes without you having to ask it to search.
Set `autoInject: "off"` to keep the vault purely tool-driven (the agent searches
only when it decides to).

## Ingestion into memory

Vault notes can feed Flowly's [governed memory](/docs/features/memory), but never
silently. `ingestionPolicy` controls how far that goes:

- **`manual_only`** — nothing from the vault becomes memory unless you ask.
- **`review_gated`** *(default)* — vault-derived facts are proposed as
  *candidates* and only become active memory after governance review. This keeps
  the vault from quietly rewriting what the agent believes.
- **`selective_auto`** — reserved for future use; treated as `review_gated` today.

This mirrors Flowly's memory philosophy: facts have a lifecycle and an audit
trail, so "the agent learned it from my notes" is always traceable.

## Setup

```bash
flowly setup            # → Tools / Integrations → Obsidian → point at your vault
```

Or edit `integrations.obsidian` directly and `flowly restart`. On first run with
`indexEnabled: true`, Flowly builds the search index over `includeGlobs`.

## Pitfalls

- **Wrong vault path = empty tools.** If `vaultPath` is unset and the fallbacks
  don't resolve, the tools report "not ready". Set an absolute path.
- **Large/binary notes.** `maxNoteBytes` skips oversized files; keep attachments
  out of `includeGlobs`.
- **Privacy.** `excludeGlobs` is your friend — keep private folders, daily-journal
  archives, or templates out of the searchable/injectable surface.
