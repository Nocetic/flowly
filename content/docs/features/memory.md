---
title: Memory
eyebrow: Features
description: Flowly's durable memory lets the agent remember preferences, decisions, and context across sessions. It is organized into three independent layers — raw markdown, a search index, and the knowledge graph — each with its own storage and access path.
---

All storage lives under your Flowly home (`~/.flowly` by default, or `~/.flowly/profiles/<name>/` for named profiles). Configuration is at `~/.flowly/config.json` under `agents.defaults.memorySearch` and `agents.defaults.memoryNudgeInterval`.

## The three layers

### 1. Raw markdown memory

Human- and agent-readable `.md` files on disk:

| File | Purpose |
|------|---------|
| `~/.flowly/workspace/memory/MEMORY.md` | Long-term, curated notes |
| `~/.flowly/workspace/memory/YYYY-MM-DD.md` | Daily notes |

These are plain markdown you can open and edit directly. `MEMORY.md` is the curated long-term store; daily notes capture per-day context.

### 2. Memory search index

The same markdown files chunked into a SQLite index for fast retrieval:

- **Path:** `~/.flowly/memory_index.sqlite` (plus `-wal` / `-shm` sidecar files).
- Files are chunked (default 400 tokens per chunk, 80-token overlap), with each chunk stored alongside an **FTS5** keyword entry and, when an embedding provider is active, a JSON-stored embedding vector.
- Indexing is **lazy and debounced to 1 second** — it runs on each `memory_search` call. There is no always-on file watcher, so edits to your markdown are picked up on the next search.
- Re-indexing is change-detected via a SHA-256 hash per file; only changed files are re-chunked.

### 3. Knowledge graph

A structured temporal triple store of facts about people, companies, projects, and relationships, at `~/.flowly/knowledge_graph.sqlite3`. See **[knowledge-graph.md](./knowledge-graph.md)** for the full data model and tool.

## How memory enters the system prompt

When memory is not skipped, the agent's context is built with:

- **`MEMORY.md` is always injected** as a `# Memory` section — no tool call required. It is scanned for prompt-injection payloads before injection.
- **Knowledge graph summary** is injected as `# Knowledge Graph` (top entities by current-fact count). See the knowledge-graph doc for the workspace caveat.
- **Recent daily notes** (last 3 days) are injected as `# Recent Notes` **only when the `memory_search` tool is not available**. When `memory_search` is enabled, the agent is expected to search on demand instead.

## Automatic curation (background self-review)

Flowly curates memory automatically. Every `memoryNudgeInterval` user turns (**default 10**; set to `0` to disable), a fire-and-forget background "self-review" subagent runs. It receives the last 20 messages plus the current `MEMORY.md` (truncated to ~8000 chars) and the knowledge-graph summary, and is instructed to:

- Extract structured facts into the `knowledge_graph` tool.
- Append only genuinely-new free-form preferences via `memory_append`.
- Otherwise reply "Nothing to save."

The review runs silently and does not interrupt your session. This is the primary automatic write path; the main agent may also write via tools directly.

## Tools

### `memory_search`

Searches the memory index. Always call before answering about prior conversations, preferences, names, dates, or projects.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `query` | string | — | Required |
| `max_results` | int | 6 | Max results returned |

Returns JSON:

```json
{
  "results": [
    { "path": "...", "lines": "...", "score": 0.0, "snippet": "..." }
  ],
  "provider": "openai",
  "vector_enabled": true
}
```

`vector_enabled` tells you whether semantic vector scoring actually ran for this query (see the embeddings caveat below).

### `memory_get`

Reads exact lines from a memory file (falls back to the index if not on disk).

| Param | Type | Default |
|-------|------|---------|
| `path` | string | — (required) |
| `from_line` | int | 1 |
| `lines` | int | 30 |

### `memory_append`

The wired markdown writer — appends to **`MEMORY.md`** (not daily notes). It enforces several guards:

- Content-injection scanning before writing.
- **Exact-duplicate** rejection (normalized SHA-256) and **near-duplicate** rejection (character-trigram Jaccard similarity ≥ 0.75).
- **Size cap** of 12000 chars; oldest timestamped entries are evicted first.
- Each entry is delimited by an HTML-comment timestamp (`<!-- YYYY-MM-DD ... -->`).

For structured facts (people, companies, relationships), the agent is directed to use the `knowledge_graph` tool instead of `memory_append`.

## How search works

`memory_search` always runs **FTS5 BM25 keyword search**. When an OpenAI embedding provider is active, it also runs vector (cosine) similarity over chunk embeddings and merges the two with a hybrid score:

```
final = vectorWeight * vector_score + textWeight * text_score
```

Results below `minScore` are dropped; the rest are sorted descending and capped to `maxResults`. Vector search is a brute-force linear scan over all chunk embeddings (no ANN index) — fine for typical memory sizes, but it scales linearly with history.

### Embeddings caveat — read this

> [!WARNING]
> Semantic/vector search only works with OpenAI embeddings. Selecting Gemini logs a warning and silently falls back to **FTS5 keyword-only** search.

Search behavior depends on which embedding provider is configured:

- **OpenAI embeddings configured** → true **hybrid BM25 + vector** search (`text-embedding-3-small`, 1536-dim).
- **Gemini selected** → embeddings are **non-functional** after the litellm migration. Selecting Gemini logs a warning and silently falls back to **FTS5 keyword-only** search.
- **No provider / `provider: none`** → **FTS5 keyword-only** search.
- **`provider: auto`** picks OpenAI then Gemini from your configured keys. If only a Gemini key is present, `auto` resolves to Gemini and you get **keyword-only** search despite a provider appearing "configured."

To verify what actually ran, check the `provider` and `vector_enabled` fields in `memory_search` output.

## Configuration

Under `agents.defaults.memorySearch` in `~/.flowly/config.json` (keys are camelCase on disk):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Register `memory_search` / `memory_get` tools |
| `provider` | `auto` | `auto` \| `openai` \| `gemini` \| `none` |
| `model` | `""` | Embedding model override |
| `chunkTokens` | `400` | Tokens per chunk |
| `overlapTokens` | `80` | Overlap between chunks |
| `maxResults` | `6` | Default result count |
| `minScore` | `0.35` | Minimum score to return a result |
| `vectorWeight` | `0.7` | Weight of vector score in hybrid merge |
| `textWeight` | `0.3` | Weight of BM25 score in hybrid merge |

`agents.defaults.memoryNudgeInterval` (default `10`) controls the background self-review cadence; `0` disables it.

## Session search

Distinct from memory, the `session_search` tool searches **past conversation transcripts** in `~/.flowly/session_index.sqlite` (FTS5 over messages). It operates in three modes inferred from its arguments:

- **DISCOVER** — pass `query`: keyword search across sessions, deduped by session, returning a snippet plus surrounding context and an `anchor_id`.
- **SCROLL** — pass `target_session` + `around_message_id` (optional `window`, default 5, max 20): a window of messages around an anchor. Refuses to scroll the active session.
- **BROWSE** — no args: recent sessions chronologically.

| Param | Type | Default |
|-------|------|---------|
| `query` | string | — |
| `target_session` | string | — |
| `around_message_id` | int | — |
| `window` | int | 5 |
| `limit` | int | 5 (capped at 10) |

None of these are required. The runtime injects the active conversation id internally so the agent cannot scroll into its own in-context session.

> [!NOTE]
> `sessions_list` is **not** part of memory — it lists and cancels background subagent tasks. See [delegation](../features/delegation.md).

## Governance, the dreamer & the `flowly memory` CLI

Behind the markdown, memories are governed in a SQLite store
(`~/.flowly/memory_governance.sqlite3`) with a real lifecycle — candidates,
calibrated **trust scores**, conflict reconciliation, and an append-only audit
trail. `MEMORY.md` is the human-readable projection of the **active** set. Three
processes write into it, each with a distinct job — don't confuse them:

### Per-turn self-review (live, single-session)

The [automatic curation](#automatic-curation-background-self-review) above: every
`memoryNudgeInterval` turns (default 10) a background subagent reads the recent
messages and appends genuinely-new facts via `memory_append` / `knowledge_graph`.
Fast and greedy — it captures facts you state explicitly the moment you say them,
straight to **active**.

### Cross-session "dreaming" (offline, across sessions)

A separate pass that reads conversation **deltas across sessions** (watermarked,
so it never re-reads the same messages), extracts durable candidates the live
path missed, and **reconciles them against what's already known** — both the
governed store and your `USER.md` profile — before committing. On by default
(`agents.defaults.memoryDreaming.enabled = true`), it fires:

- after **30 min** with no user activity (idle — background heartbeats don't count),
- once **daily** at 03:30 local,
- every **10 user turns** (a coarse pass),
- or on demand: **`flowly memory dream`**, or the desktop / iOS **"Learn from chats"** button.

Each candidate is routed by confidence and privacy:

- **explicitly stated + high confidence** → **active** (remembered immediately);
- an explicit **contradiction** of a known fact → **supersedes** it (the old one is closed and linked to the new active one);
- **inferred**, **mid-confidence**, or **sensitive** → the **review queue** (`needs_review`), for you to approve;
- **already known** (same fact in the store or `USER.md`) → **skipped**, never duplicated.

The review queue surfaces in the CLI (`flowly memory review`), the desktop memory
panel, and the iOS app (a sheet on app open). Accepting promotes an item to
active; rejecting discards it.

Tune it under `agents.defaults.memoryDreaming` (camelCase on disk): `enabled`,
`commitMode` (`"selective"` default — `"manual"` queues *everything* for review,
`"aggressive"` lowers the bar), `idleMinutes` (30), `dailyEnabled` (true) +
`dailyTime` (`"03:30"`), `turnInterval` (10), `autoFloor` (0.80 — at/above ⇒
auto-active when unconflicted and not sensitive), `reviewFloor` (0.55 — below ⇒
dropped), `maxMessagesPerRun` (500). Set `turnInterval: 0` to drop the per-turn
pass and rely on idle + daily only; `enabled: false` turns the layer off entirely.
The full config table is in [Configuration](../using-flowly/configuration.md#agents).

### External memory import (ChatGPT / Gemini)

Flowly can import a copied memory/profile dump from ChatGPT or Google Gemini:

```bash
flowly memory import-prompt --source chatgpt
flowly memory import --source chatgpt path/to/dump.md
```

The first command prints the prompt to paste into ChatGPT/Gemini. Paste the
model's response into a file (or pipe it on stdin) and run `flowly memory import`.
The importer uses the same governed commit path as the dreamer:

- the pasted dump is treated as untrusted text and prompt-injection scanned;
- compound bullets are normalized into memory candidates;
- duplicates are skipped or bumped, not re-added;
- contradictions with active Flowly memory go to review instead of silently
  overwriting local memory;
- imported items are never auto-activated in v1 — they land in the review queue.

The same flow is available to clients via `memory.import_prompt` and
`memory.import`, and to the agent through the `memory-import` skill when memory
governance is enabled.

### Consolidation (cleanup of the existing store)

Distinct from dreaming, consolidation is an LLM-driven **cleanup** of what's
already stored — it merges cross-key duplicate facts, retires free-form that
duplicates the knowledge graph, and marks outdated notes stale. It runs in the
background (every ~50 turns / ~30 min, gated on new writes since the last pass)
and on demand via **`flowly memory consolidate`** or the desktop **"Clean now"**
button. Configured under the same block: `autoConsolidate`,
`consolidateTurnInterval` (50), `consolidateEveryMinutes` (30).

### The CLI

Inspect and correct everything from the CLI (or `/memory` in a chat):

```bash
flowly memory list                  # active long-term memories
flowly memory review                # the review queue (pending candidates)
flowly memory accept <id>           # or: reject <id>
flowly memory dream                 # learn from recent chats now (cross-session)
flowly memory feedback <id>         # 👍/👎 to retune a memory's trust score
flowly memory correct <id> "..."    # fix a memory's content
flowly memory undo                  # revert the last change
flowly memory consolidate           # merge duplicates / retire stale now
flowly memory import-prompt         # get a ChatGPT/Gemini export prompt
flowly memory import dump.md        # import external memory into review
flowly memory refresh               # rebuild the MEMORY.md block from the store
flowly memory status                # store statistics
```

See [CLI commands](../reference/cli-commands.md) for the full group.

## Related

- [Knowledge graph](./knowledge-graph.md)
- [Delegation](../features/delegation.md)
- [Features overview](../features/overview.md)
- [Sessions](../using-flowly/sessions.md)
- [Tools reference](../reference/tools.md)
