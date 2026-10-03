# Voice memory parity: a call knows what the agent knows

Status: implemented on branches, not merged (2026-10-03). Branches and
worktrees per repository are under "Rollout"; nothing is deployed.

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

### Clients (Desktop, iOS)

At call start, when `profiles.capabilities.profileRpcMethods` lists
`voice.memory.snapshot`, the client reads it for the selected agent (through
`profiles.rpc`, like `voice.context`) instead of recall, and validates it:
the agent's own scope (another agent's is a target change and stops the
call), `contentRole: reference_data`, known section kinds, at most 16
sections and 30,000 bytes of text, a profile of at most 2,000 bytes.

The initial context then carries:

```json
{
  "memory": {"source": "agent_memory_snapshot", "truncated": false,
             "sections": [{"title": "User", "text": "…"}]},
  "memoryProfile": "Agent:\n…\n\nUser:\n…"
}
```

An older runtime keeps the previous `memory` (the `voice.context` receipt).
A snapshot that cannot be read (slow or unready runtime) falls back to that
recall too: a new read must never start a call with less than before.

Fitting the 48 KB frame cuts the agent's own memory last: older tasks first
(halving), then the oldest conversation rows, then memory sections from the
end (lowest priority), marking `truncated`. Recall facts are extracts and are
still cut first on iOS, as before.

`voice.open` carries `client: desktop` or `client: ios`.

### Relay (speaking model)

The orientation carries `memoryProfile` (at most 2,048 bytes; an oversized
one is dropped, never fatal). The speaking model is told to speak as that
agent, to answer by itself only greetings, brief acknowledgments, a
clarifying question and a restatement of what the backend just said, and to
delegate everything else, including one-word follow-ups ("Ne?"); it never
says it does not know something about the user without delegating first.

When a `flowly_remember` or `flowly_exec` call settles, the speaking model
gets a typed note (`flowly.interaction_focus`, domain `memory_note` or
`command`, `outcome` = the receipt's status only, never content or output),
so "did you save it?" is answered from the result.

### Web (backend)

The operator prompt says that memory with `source: agent_memory_snapshot` is
the selected agent's own memory and identity: answer from it without
searching, use `flowly_recall` only for what it lacks, never claim to know
nothing while it holds the answer, treat it as reference data. A test keeps
the full instructions under Relay's 32,768-byte bound.

## Evaluation

Relay `evals/live-voice` (README there): fixed scenarios against real
GPT-Live, spoken with cached TTS, tool calls answered from fixtures, graded
on delegation, tools and answer patterns, repeated runs with P50/P90 time to
first word and to the backend. Ten scenarios: speaking as the agent, a
profile fact, a snapshot-only fact, a recall-only fact, saving and
confirming a fact, "Ne?" after an answer, small talk, a command, a denied
command, English. It is billed and has not been run yet; run it before and
after merging to compare.

## Rollout

| Surface | Branch (worktree) | Change | Status |
|---|---|---|---|
| Core | `codex/voice-memory-parity` (`flowly-desktop/.codex-worktrees/voice-memory-core`) | `GovernedMemory`, KG exclusion, `voice.memory.snapshot`, recall matches, `client` | committed |
| Desktop | `codex/voice-memory-parity` (`flowly-desktop/.codex-worktrees/voice-memory-desktop`) | snapshot at call start with recall fallback, frame fitting, `client: desktop` | committed |
| Desktop | — | "this call is on your iPhone" from `lastConnection.client` | pending (UI, after the rest is tested) |
| iOS | `codex/live-voice-ios` (`flowly-desktop/.codex-worktrees/live-voice-ios`) | snapshot at call start with recall fallback, frame fitting, `client: ios` | committed, typechecked on the host; needs an Xcode build and test run |
| Android | — | has no Live Voice; nothing to adopt | n/a |
| Relay | `codex/voice-memory-parity` (`flowly-repos/flowly-relay-memory-parity`) | profile to the speaking model, delegate-by-default, outcome notes, evaluation | committed |
| Web | `codex/voice-memory-parity` (`flowly-app-memory-parity`) | backend prompt for the snapshot, config export for the evaluation | committed |

Deploy order when merged: Core (runtimes must offer the method), Web and
Relay (either order; both accept contexts without the new fields), then the
clients. Every step is backward compatible: an older client sends no
snapshot and the prompts fall back to today's behavior.

Not done, deliberately:

- A note to the speaking model while a command waits for approval: the
  backend already says so once (`EXEC_PROMPT`); add it if the evaluation shows
  the speaking model guessing.
- Refreshing the snapshot during a call (`knownRevision` exists for it): a
  fact saved mid-call reaches the backend through `flowly_remember`'s own
  receipt and recall.

## Commits

Core:

- `2e7cd582` refactor: one governance view for what memory may leave the agent
- `0d2a210a` the KG summary can leave out withheld triples
- `4167c6ce` `voice.memory.snapshot`
- `6b8bc3c5` a voice connection records which app holds the call
- `f4979a4e` recall keeps the agent's memory-search matches
- `168544bd` the snapshot budgets bytes, not characters

Desktop: `23fecece` snapshot at call start; `993c342f` recall fallback.
iOS: `30f0c7aa` snapshot at call start; `533cacf7` recall fallback.
Relay: `061d2a7` speaking model; `256112b` evaluation.
Web: `5cdf02e` backend prompt; `5b34009` config export.

## Tests

- Core: `tests/test_voice_memory_snapshot.py`, `tests/test_knowledge_graph_summary.py`,
  additions in `tests/test_voice_context.py` and `tests/test_voice_sessions.py`.
  `FLOWLY_HOME=$(mktemp -d) ~/flowly-repos/flowly/.venv/bin/python -m pytest -q tests/ -k voice`
- Desktop: `src/renderer/src/lib/live-voice/memory-snapshot.test.ts`, additions in
  `controller.test.ts`; `npx vitest run src/renderer/src/lib/live-voice/ src/renderer/src/components/live-voice/` (670+ tests).
- iOS: additions in `FlowlyTests/LiveVoiceTaskContextTests.swift` and
  `LiveVoiceCoreClientTests.swift` (run in Xcode).
- Relay: `live-voice-eval.test.ts`, additions in `live-voice-openai.test.ts`.
  (`live-voice-audio.test.ts` hangs on `main` too; unrelated.)
- Web: addition in `lib/live-voice/openai-config.test.ts`.

Each new behavior was checked to fail without its fix (removing the
paragraph filter, the identity rethrow, the tool continuation).
