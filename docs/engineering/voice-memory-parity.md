# Voice memory parity: a call knows what the agent knows

Status: implementation and startup hardening are on branches (2026-10-04).
The phone's reported missing-snapshot incident still needs a device retest;
passing the routing probe is not evidence of a phone call loading its memory.
See "Rollout" for merged/deployed pieces. At the owner's subsequent request,
Web and Relay were merged and pushed to main; deployment is owner-operated
and has not been verified. Core, Desktop and iOS remain on their worktree branches.

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

Follow-up (2026-10-04): lexical indexing now completes before bounded embedding
work (5 seconds for normal search, 0.8 seconds inside Voice's 1.5-second read).
Provider failures/timeouts retain keyword hits and back off for 30 seconds.
Missing vectors can be enriched later without reindexing unchanged files;
hash-conditional writes cannot attach an old vector to changed text. Full
indexed text, not the display snippet's synthetic ellipsis, is checked against
current, governance-filtered source before a Voice fact is exported.

Embedding credentials and endpoint resolve from the embedding provider's
configuration or explicit memory-search overrides. A configured OpenRouter
chat key alone selects keyword-only search; it is not sent to OpenAI. These
changes affect normal `memory_search` as well as Voice recall. Initial memory
snapshots remain independent of embeddings.

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

iOS now also probes the pinned snapshot RPC once when its advertisement is
missing (at most 2 seconds, within the caller's budget). The runtime's receipt
or error is authoritative; no other agent is tried. Desktop's discovery gate
is unchanged. On iOS's recall fallback, up to 12 verified facts also produce a
UTF-8-safe, at-most-2,000-byte `memoryProfile`, explicitly marked partial, so
the speaking model gets a bounded excerpt of the backend's available knowledge.
Empty recall does not invent a profile. Previously a successful fallback read
left the speaking model with no memory.

The initial context then carries:

```json
{
  "memory": {"source": "agent_memory_snapshot",
             "scope": {"profile": "default", "botId": "…"},
             "revision": "<24 hex>", "partial": false, "truncated": false,
             "sections": [{"title": "User", "text": "…"}]},
  "memoryProfile": "Agent:\n…\n\nUser:\n…"
}
```

An older runtime keeps the previous `memory` (the `voice.context` receipt).
A snapshot that cannot be read (slow or unready runtime) falls back to that
recall too. Cancellation and account/agent identity changes are terminal;
they never trigger a recall for another target. Each new call reads the
selected agent afresh, even if two hosts both call their profile `default`.
There is no cross-agent memory cache or transfer between agents' stores.

Clients retain the verified snapshot's scope, revision and partial flag in
the initial context. Final context serialization rejects a memory bot ID
that differs from the selected agent. Revision identifies Core's snapshot,
not a digest of the client's possibly budget-trimmed sections.

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

Relay checks a scoped snapshot's bot ID against `selectedAgent.id` before
starting the provider session. Legacy contexts without snapshot scope remain
accepted. This is a consistency check, not authorization: ownership and
identity remain enforced by the session and Core's pinned RPC. Relay inserts
the complete initial context into the backend prompt literally (a replacement
callback); `$&`, `$$`, ``$` `` and `$'` inside memory must not be interpreted as
JavaScript replacement-string syntax. Full sections go only to the backend;
the speaker receives the short profile.

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

## Incident 2026-10-04: the phone's memory requests were refused

The first device tests found every iOS call starting without memory. iOS
pins the agent on profile RPCs (expectedHostId, expectedBotId);
`ProfileHost.rpc` forwards the pin as `expectedBotId`; the runtime's
`feature_rpc.dispatch` checked it but left it in the params; the strict voice
validators (`validate_snapshot`, `validate_context`) refused the unknown field
(INVALID_PARAMS). Both landed in `5faedbf8` (2026-09-23), so iOS recall had
always been refused; Desktop sends no pin. The snapshot inherited the path.
No test sent the request a client actually sends.

Fix (`31dd01bd`): dispatch drops the pin for the per-runtime voice methods
after checking it. Prevention:

- **Contract** `tests/fixtures/live_voice_client_requests.json`: the exact
  envelopes Desktop and iOS send (snapshot and recall, default and named
  agent, pinned and unpinned). Desktop
  (`src/renderer/src/lib/live-voice/contracts/`) and iOS
  (`FlowlyTests/Contracts/`) keep byte-identical copies and test that their
  request builders produce them; `scripts/check_voice_client_contract.py`
  fails on a drifted copy.
- **Core contract test** `tests/test_voice_client_contract.py` sends every
  envelope through the real routing (ProfileHost.rpc, runtime dispatch,
  strict validators); without the fix exactly the iOS envelopes fail.
- **Probe** `scripts/voice_memory_probe.py`: asks the running local gateway
  the clients' way for every running agent and prints shapes (read-only).
  Run it before asking anyone to test a call. On 2026-10-04 all four agents
  answered both clients' envelopes.
- **Report** `scripts/voice_memory_report.py`: from gateway.log, how calls
  started (snapshot or recall, served or refused).
- **Logs**: Core logs what each snapshot and recall served (shape only);
  iOS logs how a call's memory started (`live-voice-memory`).

### Follow-up: prove selection, fallback and delivery separately

The 02:16 phone call still used empty-query recall without a snapshot request
after the pin fix. That evidence does **not** establish why the phone skipped
the snapshot. Previously, successful recall when the capability was absent
emitted no memory log, and snapshot failures logged raw error text. The latter
could include private server content.

Desktop and iOS now emit a single content-free startup result from the same
reader used by their call startup:

- `snapshotAdvertised`, `source` (`snapshot`, `recall`, `none`);
- `snapshot` and `recall` outcomes: `not_attempted`, `not_advertised`, `ok`,
  `cancelled`, `target_changed`, `account_changed`, `invalid_receipt`,
  `invalid_params`, `unsupported`, `timeout`, `unavailable`;
- section count, section text bytes, profile bytes, partial and truncated.

iOS uses the `live-voice-memory` category; Desktop uses
`[live-voice-memory]` / `live_voice_startup_memory` with the connection ID.
No text, profile, credential, arbitrary upstream code or error message is
logged. These are local client diagnostics; this change does not add a Web
ingestion endpoint, central delivery or dashboard. Desktop diagnostic sink
failures cannot affect the memory read's result.

The host's `PROFILE_IDENTITY_CHANGED` and `TASK_TARGET_CHANGED` refusals now
remain terminal through optional startup reads. They cannot silently become
a call without the selected agent's memory. Ordinary unavailable-memory
behavior is unchanged: recall is attempted; if it also fails, iOS may start
without enrichment, while Desktop startup reports the read failure.

Verification on 2026-10-04:

- Running Core: 16/16 read-only checks (four running agents, Desktop/iOS
  envelopes, snapshot/recall). Snapshot advertised; no call created.
- Core: 1,104 selected Voice/profile/feature-RPC tests passed. The sandbox
  denied loopback binding on the first run; the allowed local retry passed.
- Desktop: 688 Voice tests passed, plus six newly added diagnostic/fallback
  cases in a subsequent 26-test tools run; renderer TypeScript check passed.
- iOS: `scripts/verify-live-voice-startup.sh` builds actual Foundation routing,
  validation and context code in a temporary macOS Swift package: 57 tests
  passed, including default/named profiles, two hosts named `default`,
  cancellation, wrong-agent receipts and missing/failed snapshot fallback.
  `scripts/verify-live-voice-context.sh` passed. Coordinator source parsed.
  This does not compile the full UIKit/Firebase app or run a phone call.
- Relay: 231 tests and TypeScript check passed. New regression tests first
  failed on the old implementation for literal context corruption and a
  mismatched scoped snapshot, then passed with the fixes.
- Core/Desktop/iOS client request fixtures remain byte-identical.
- Merge verification: Web's 192 Voice library/API tests passed after fixing
  the existing access-endpoint test fixture's missing database/rate-limiter
  dependency (test-only change). Relay's 231 tests, typecheck, bundle build
  and bundle syntax check passed on the merged main.

No iOS simulator was run. Before claiming the incident resolved, the owner
must build the iOS worktree on a device and make a fresh call: verify
`source=snapshot snapshotAdvertised=true snapshot=ok`, then ask about a known
non-private fact without an explicit search. Repeat on another updated host
with a different known fact and verify the first host's fact is absent. Pair
these with Core's served/refused logs. `not_advertised` means inspect the
actual `profiles.capabilities` response on that phone connection;
`invalid_params` means inspect its request envelope. Neither should be
diagnosed from the model's answer alone. A healthy loaded snapshot still
requires model-behavior evaluation; the hosted eval suite has not been run.

## Incident 2026-10-04 (2): the phone's memory read lost a race on the relay

After the pin fix the phone still started calls without memory, and
gateway.log showed no snapshot request at all. Cause: every relay request
carrying voiceAccess takes a receive-order slot (RelayRecipients.begin) and
binds it after its certificate verifies asynchronously;
EventRecipients.bind accepted only the newest slot. The phone sends the
snapshot, tasks and focus at once, so the snapshot (and tasks) lost to a
later request and was refused with VOICE_AUTH_REQUIRED before any handler,
without a log line. The probe and local clients never take this path.

Fix `6f4dd732` (`flowly/channels/web.py` `_handle_rpc`): only the lease
methods (voice.events.bind/clear) are refused on a stale slot; other
requests are authorized by their own certificate and the live principal
check. Test: `tests/test_voice_relay_events.py`
`test_a_call_starting_with_several_reads_at_once_gets_all_of_them`.
The cloud channel now logs each voice profile RPC arriving and any refusal
(`90d445a0`). Verified on the owner's phone at 15:13: snapshot arrived and
was served (15 KB), and the agent knew the owner.

Lesson: the local probe proves the routing inside the gateway, not the
relay path; a phone's call must be checked in gateway.log
("voice profile RPC … arrived").

## Spoken language (2026-10-04)

A new conversation opened in the client's interface language, so a Turkish
speaker with an English interface was greeted in English. iOS ignored the
host's `spokenLanguage` and never reported one; Desktop reported it only via
`flowly_recall`, which the snapshot makes rare.

The host now owns it (`flowly/live_voice/language.py`):

- learned from the owner's own transcribed speech (`voice.append`, every
  client) and the model's reports (`voice.language`); kept per owner and
  agent in `voice_language.json` (atomic, bounded);
- conservative detection (Turkish, English, Spanish); speech moves the
  preference only when the same new language is heard twice in a row, a
  model report at once;
- `voice.open` opens a new conversation in it (the client's language only
  when none is known) and returns it as `spokenLanguage`; an existing
  conversation keeps its own.

Clients start the call in the returned language (Desktop already did; iOS
`75b0d0cf`). Relay's speaking model starts and stays in it and never
drifts into English on its own; the greeting names it (`eadc621`). Web
tells the backend to write in it (`d03b83f`).

Known limit: the first call to an agent, with nothing learned yet, opens
in the interface language; the owner's first sentence in another language
switches the call, and the next one opens in it.

## Corrections, dates and where we left off (2026-10-04)

Found on the owner's call ("I'm not allergic to penicillin") and memory:

- **A correction did not hold.** `knowledge_graph invalidate` closed the
  triple, but its governed copy stayed active, so MEMORY.md and the next
  call stated both facts. The panel's facade had no graph mirror, so a
  rejected fact stayed current in the graph. Fixed `83276e41`: a governed
  fact is active exactly while its triple is current (invalidate retires,
  reject and consolidation's stale close, one facade for agent, panel and
  CLI, a one-time startup repair `reconcile_kg_triples`), the graph
  summary leaves out non-active facts, a panel decision rebuilds
  MEMORY.md. Each note is stated once (the generated block skips a note's
  copy while the file states it; the prompt reads the graph once; a
  retired note is left out of the prompt as of a call).
- **Dates were guessed.** The prompt has no clock (cache stability). Fixed
  `fee23686`: each turn carries `<turn_time>` on its own message, never
  cached or stored.
- **A call did not know what was just said.** The snapshot now carries
  the end of the owner's latest conversation (kind `recent`, title
  "Latest conversation: <title> (<date>)", at most 12 messages / 6 KB,
  newest kept; never another call, a call's work, an agent room or a
  scheduled run; visible exactly as `sessions.list` shows it; governance
  withholding, secret redaction and the injection scan apply). The
  speaking model's profile starts with "Last talked about: …". Clients
  need no change: `recent` is already an accepted kind.

Not done, by decision: refreshing memory during a call (rare; a correction
made in the call reaches it through the task result).

## Rollout

The owner authorized Web and Relay merges after the startup hardening.
Core, Desktop and iOS remain unmerged; branches and worktrees:

| Surface | Branch (worktree) | State |
|---|---|---|
| Core | `codex/voice-memory-parity` (`flowly-desktop/.codex-worktrees/voice-memory-core`) | snapshot, pin fix, contract, probe, report, language |
| Desktop | `codex/voice-memory-parity` (`flowly-desktop/.codex-worktrees/voice-memory-desktop`) | snapshot, greeting (merged in), contract test, scoped startup and diagnostics |
| iOS | `codex/live-voice-ios` (`flowly-desktop/.codex-worktrees/live-voice-ios`) | snapshot, greeting, ringing, call screen, language, contract test, scoped startup and diagnostics |
| Relay | `main` at `094821d`; `codex/voice-language` (`flowly-repos/flowly-relay-language`) retained | language and memory hardening merged/pushed; latest deploy not verified |
| Web | `main` at `b0e293b`; `codex/voice-language` (`flowly-app-language`) retained | memory/language rules and access test fixture merged/pushed; latest deploy not verified |
| Android | — | no Live Voice |

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

Startup hardening (2026-10-04): Desktop `7bda3187`, iOS `5a0022c3`, Relay
`6eb0ebd`. Core runtime code did not change in this follow-up. Web test fixture
repair: `3c74986`. Web main `b0e293b` and Relay main `094821d` were subsequently
pushed with the owner's authorization; deployment remains unverified. The
Relay push also included 31 already-committed changes on local main that were
previously ahead of origin/main. iOS's owner's existing `project.pbxproj` and
`Info.plist` edits were left outside these commits.

## Tests

- Core: `tests/test_voice_memory_snapshot.py`, `tests/test_knowledge_graph_summary.py`,
  additions in `tests/test_voice_context.py` and `tests/test_voice_sessions.py`.
  `FLOWLY_HOME=$(mktemp -d) ~/flowly-repos/flowly/.venv/bin/python -m pytest -q tests/ -k voice`
- Desktop: `src/renderer/src/lib/live-voice/memory-snapshot.test.ts`, additions in
  `controller.test.ts`; `npx vitest run src/renderer/src/lib/live-voice/ src/renderer/src/components/live-voice/` (670+ tests).
- iOS: additions in `FlowlyTests/LiveVoiceTaskContextTests.swift` and
  `LiveVoiceCoreClientTests.swift` (run in Xcode).
- Relay: `live-voice-eval.test.ts`, additions in `live-voice-openai.test.ts`.
  `live-voice-audio.test.ts` used to hang `npm test` (and the deploy script):
  its teardown removed the journal before the relay stored final usage; fixed on
  Relay `main` in `c2b5683`. Relay and Web are merged (`4f83f5f`, `ed425e5`; Web pushed).
- Web: addition in `lib/live-voice/openai-config.test.ts`.

Each new behavior was checked to fail without its fix (removing the
paragraph filter, the identity rethrow, the tool continuation).
