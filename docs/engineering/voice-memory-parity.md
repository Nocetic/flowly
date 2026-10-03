# Voice memory parity: a call knows what the agent knows

Status: in progress on branches (not merged), started 2026-10-03.
Core `codex/voice-memory-parity` (worktree `.codex-worktrees/voice-memory-core`).
Client and service work is listed under "Rollout" and updated as it lands.

This document is the contract. Clients follow it; when it changes, change it
here first.

## Problem

A Live Voice call runs two models (GPT-Live "Responses delegation"):

- the **speaking model** answers within a breath and decides when to delegate;
- the **backend** (`gpt-5.6-luna` today) reasons and calls Flowly tools.

Before this work the backend started from `voice.context` with an empty query
and `limit: 8`, so a call knew at most eight memory paragraphs, shared
between governed items, `USER.md`, `MEMORY.md` and the knowledge graph. The
agent's identity files never reached it. The speaking model received no memory
at all. Meanwhile the agent's own chat prompt carries its identity files, every
governed memory item, the KG summary and its notes. Owner-visible symptoms:
the call did not know facts the agent knew, sounded like a different agent,
and answered personal questions without them.

Option "make the user's own agent the backend" (GPT-Live client delegation)
was rejected: the agent's turn latency would set the call's response time.

## Contract

### `voice.memory.snapshot` (per runtime)

Served by every runtime for its own profile, exactly like `voice.context`
(`_PER_RUNTIME_VOICE_METHODS`, `PROFILE_RPC_TIMEOUTS` 10 s, reachable through
`profiles.rpc`; advertised in `profiles.capabilities.profileRpcMethods`).

Params: `{}` or `{"knownRevision": "<24 hex>"}`. Nothing else: the runtime owns
its scope.

Result:

```json
{
  "scope": {"profile": "default", "botId": "…"},
  "contentRole": "reference_data",
  "revision": "<24 hex>",
  "sections": [{"kind": "soul|persona|identity|user|memory|notes|recent",
                "title": "…", "text": "…", "sourceRef": "memory://…"}],
  "profile": "<= 2,000 UTF-8 bytes for the speaking model",
  "truncated": false,
  "partial": false,
  "sources": {"governance": {"status": "ok|empty|unavailable", "revision": "…"}, "…": {}}
}
```

With a matching `knownRevision` the reply is only
`{scope, contentRole, revision, unchanged: true}`.

### What the sections are

The same sources the agent's chat prompt carries, built the same way, in the
order the prompt loads them:

| kind | source | built by |
|---|---|---|
| `soul` | `SOUL.md` | as in `ContextBuilder._load_bootstrap_files` |
| `persona` | `personas/<active>.md` (when not `default`) | same rule |
| `identity` | `IDENTITY.md` | same |
| `user` | `USER.md` | same |
| `memory` | governed items + KG summary | `render_generated_block` (MEMORY.md's own renderer) and `KnowledgeGraph.summary(max_entities=20)` |
| `notes` | `memory/MEMORY.md` outside the generated block | the human-written notes |
| `recent` | `memory/YYYY-MM-DD.md`, last 3 days | only when the agent has no memory search, as in its prompt |

`AGENTS.md` and `TOOLS.md` are the chat agent's operating instructions for its
own tools, not knowledge, and are not exported.

### What a call may not see (governance)

`flowly/live_voice/memory_view.py` `GovernedMemory` is the single rule for both
`voice.context` and the snapshot:

- an item reaches a call only while `active`, `privacy_level == normal` and
  inside its validity window;
- every other item is withheld: a paragraph (snapshot) or file (recall)
  carrying its words is left out; its KG triple is excluded
  (`KnowledgeGraph.summary(exclude_triple_ids=…)`);
- an unreadable or malformed governance index exports nothing (fail closed).

Files also pass the agent's prompt-injection scan (`scan_context_file`, a
`[BLOCKED …]` placeholder) and `redact_secrets`.

Decision (2026-10-03, owner): private items stay out of calls; this is the one
intended difference from the chat prompt. Calls run on Flowly's OpenAI
account, not the owner's chosen provider.

### Budget

Sections keep the priority order above within `SNAPSHOT_BUDGET` = 30,000 UTF-8
bytes, cut only at a paragraph with a note pointing to `flowly_recall`, and
`truncated: true`. This leaves room for tasks and recent conversation in the
client's 48 KB initial-context frame. The profile is at most 2,000 UTF-8 bytes:
agent, character, user, and what is known about the user.

### Recall

`flowly_recall` (`voice.context`) keeps using the agent's own memory index (the
hybrid search `memory_search` uses in chat). A memory-search match is kept even
without the query's literal words and ranks one above keyword overlap.
Previously such matches were dropped.

### Which app holds the call

`voice.open` takes an optional `client` (`ios`, `android`, `desktop`, `web`),
stored on the connection, so `lastConnection.client` tells another device
where an unended call runs. Older clients send none; older Cores ignore it.
A replayed open of the same connection cannot change its app.

A client reading another device's call must treat a connection whose
`openedAt` is older than 11 minutes without `endedAt` as ended (a call segment
lasts at most 10 minutes; a crashed client never writes `endedAt`).

## Rollout

| Surface | Change | Status |
|---|---|---|
| Core | `GovernedMemory`, KG exclusion, `voice.memory.snapshot`, recall matches, `client` | committed on `codex/voice-memory-parity` |
| Desktop | read the snapshot at call start, send `client: desktop`, "call is on another device" | pending |
| iOS | read the snapshot, send `client: ios` | pending |
| Android | read the snapshot, send `client: android` | pending |
| Relay | pass the profile to the speaking model; mirror notes; delegate-by-default rule | pending |
| Web | backend prompt: the memory in context is the agent's own | pending |
| Evaluation | fixed scenarios, repeated runs (memory, attachments, commands, delegation) | pending |

## Core commits

- `2e7cd582` refactor: one governance view for what memory may leave the agent
- `0d2a210a` the KG summary can leave out withheld triples
- `4167c6ce` `voice.memory.snapshot`
- `6b8bc3c5` a voice connection records which app holds the call
- `f4979a4e` recall keeps the agent's memory-search matches
- `168544bd` the snapshot budgets bytes, not characters

Tests: `tests/test_voice_memory_snapshot.py`, `tests/test_knowledge_graph_summary.py`,
additions in `tests/test_voice_context.py` and `tests/test_voice_sessions.py`.
Run from the worktree:
`FLOWLY_HOME=$(mktemp -d) ~/flowly-repos/flowly/.venv/bin/python -m pytest -q tests/ -k voice`.
