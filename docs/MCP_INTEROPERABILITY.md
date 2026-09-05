# MCP interoperability acceptance record

The objective is a production-ready MCP surface for external agents, together
with a resilient client for third-party MCP integrations. Changes are committed
on `codex/mcp-enterprise`; merging and publishing are outside this task.

## Required outcomes

- [x] External clients can poll/wait for new conversation events with cursors,
  bounded retention, reconnect handling, cancellation, and explicit gaps.
- [x] External clients can identify message attachments, including media-only
  messages, without arbitrary filesystem access or losing archive history.
- [x] External clients can discover known channel targets with exact session
  addresses and configured enabled state (not a promise of live delivery).
- [x] The coding-agent tool bridge exposes vision/image, speech, and
  task-board capabilities where available, preserving authoritative schemas and
  execution permissions. Stateful tools reach the owning live runtime.
- [x] MCP server requests for user input/consent reach the owning surface;
  untrusted write tools cannot execute without approval.
- [x] OAuth picks up cross-process token changes, coordinates concurrent 401
  recovery, and distinguishes expired sessions from expired credentials.
- [x] Persistent tool manifests support lazy server startup and bounded idle/
  lifetime recycling, without stale schemas or duplicate subprocesses.
- [x] Explicit exclusive tool requests resolve consistently in English/Turkish,
  cannot broaden structured grants, and are enforced during execution.
- [ ] MCP log notifications and failure diagnostics remain bounded and redact
  credentials; existing protocol/content/transport support remains intact.
- [ ] Real transport tests exercise the public client/server paths, permission
  boundaries, concurrency and lifecycle failures. The full regression suite
  passes and each acceptance item links to its evidence.

## Explicit scope exclusions

Per the user's scope decision, browser automation through the external-agent
MCP bridge is excluded from this goal. Existing built-in browser functionality
is unchanged. Web search/retrieval and non-browser image analysis/generation,
speech and task-board interoperability remain in scope.

## Verification record

The remaining unchecked outcomes are pending. The historical baseline is commit `3c58b18`; its suite
reported 4897 passed, 1 skipped, 12 deselected. That result is not evidence for
the outcomes still unchecked above.

### Conversation history and events

`tests/mcp/test_conversation_events.py` exercises real archive writes,
compaction, media-only messages, exact session addressing, withdrawn/hidden
message exclusion, cross-process search freshness, deletion, concurrent journal
readers, cursor/filter validation, retention gaps, and cancellation. The public
stdio test launches a separate server, cancels a long wait, then restarts the
process and resumes from its persisted cursor. `tests/mcp/test_serve_http.py`
also exercises event waiting through the public authenticated HTTP server and
Flowly's real MCP client. Attachments intentionally return metadata, not bytes
or local paths. Events become visible at the next poll; they are not push
delivery guarantees. Search uses a public-only process-local index and does not
modify the agent's shared history index.

Verification command: `uv run pytest tests/mcp tests/test_session_archive_integrity.py
tests/test_session_concurrency.py tests/test_session_disk_roundtrip.py
tests/test_session_delete_integrity.py tests/test_session_indexer_incremental.py -q`.

### MCP consent and input-required continuation

`tests/mcp/test_interaction.py` launches a real stdio server in auto, explicit
modern and legacy modes. It verifies concurrent calls retain their own user
surface, remote `session_key` arguments cannot select the owner, typed form
answers are validated, denial and timeout disclose no data, cancellation
retires in-flight prompts (even during notification delivery), cron context
survives both loop hops, and untrusted writes do not run before approval.
Modern input-required continuation preserves opaque request state, uses the
SDK driver, and caps rounds and requests. The same continuation helper is used
by tools, prompt reads and resource reads.

`trust: untrusted` asks per-call consent for tools without `readOnlyHint: true`.
The default remains `full` for existing manually configured integrations;
annotations are server claims, not a process sandbox. Elicitation supports
bounded flat non-sensitive scalar forms. URL-mode, nested/reference schemas,
missing execution owners and sensitive credential fields decline explicitly.
Legacy calls serialize when elicitation is enabled because legacy callbacks
cannot reliably identify the parent call; modern calls preserve concurrency.
Programmatic registry calls supply the runtime-owned `session_key` keyword,
separate from the remote tool argument dictionary.

Verification: `uv run pytest -q` — **4930 passed, 1 skipped, 12 deselected**
in 85.95 seconds. This includes the 17 new consent/input tests and the public
conversation transport tests. Real-LLM tests remain excluded; this is not proof
of the still-unchecked acceptance items or live provider-specific behavior.
Targeted Ruff checks for the new interaction/context/request modules and tests,
and `git diff --check`, also pass.

### OAuth recovery and credential publication

`tests/mcp/test_oauth_recovery.py` has 38 tests using a real loopback HTTP
authorization service, independent providers, and separate Python processes.
It covers concurrent 401 recovery with rotating or unchanged access tokens,
cross-process credential reload, persisted expiry and AS discovery, omitted
refresh/scope fields, temporary/permanent failures, malformed token responses,
issuer/resource binding, scope consent, cancelled lock waiters, and process death.
Normal tool requests remain concurrent and successful SSE bodies stream without
holding a recovery lease. Token writes are owner-only before bytes are written,
atomic and revision-checked; a stale refresh cannot undo login/logout.

The public-client tests run Flowly's `discover_mcp_tools` and registry calls
against the official MCP server over modern and legacy Streamable HTTP plus
legacy SSE (including its separate POST endpoint). A terminated legacy HTTP
session reconnects without refreshing/deleting tokens or replaying the failed
operation. `tests/mcp/test_lifecycle.py` also distinguishes that exact protocol
error from ordinary method-not-found and authorization errors.

CLI and desktop sign-in now stage credentials until the connection probe
succeeds. Failed/cancelled sign-in requires no destructive rollback. Concurrent
login/logout wins over stale publication. Tests exercise the public interactive
probe across the MCP loop boundary, environment-expanded URLs, staging cleanup,
and late writes after cancellation. `tests/mcp/test_oauth.py` verifies the SDK's
typed authorization callback including the issuer parameter;
`tests/test_feature_rpc_mcp.py` checks failed desktop sign-in preserves the
working grant. Existing legacy token files remain readable and acquire an
explicit name/URL binding on their next write.

Targeted verification: `uv run pytest tests/mcp/test_oauth_recovery.py -q` —
**38 passed**. This is controlled local HTTP/SDK evidence, not a claim that every
vendor-specific SSO configuration or every operating system has been tested.
The remaining unchecked tool-bridge, manifest/lifecycle, policy, diagnostics and
final acceptance outcomes are still required.

Final regression for this change: `uv run pytest -q` — **4970 passed,
1 skipped, 12 deselected** in 89.70 seconds on the current macOS host.
Targeted Ruff checks for OAuth/storage/lifecycle code and tests and
`git diff --check` pass. The broader CLI/feature-RPC files retain their same
14 pre-existing Ruff findings; no unrelated lint rewrites were made.

### Live-runtime tool bridge (partial acceptance)

`flowly mcp tools` exposes the registered web, image/video analysis, speech/image
generation and Board tools through a separate generic SDK stdio front end.
Grants bind exact sessions, selected tools and the owning execution context.
Schema validation, live availability and registry hooks remain authoritative;
Board tools are context-bound copies sharing the live store/orchestrator.
No browser or shell tools are exposed. Local image/video/media reads use bounded,
no-follow descriptor walks rather than trusting a prior path check alone.

`tests/mcp/test_live_tool_bridge.py` exercises the real gateway HTTP routes and
public CLI subprocess with modern and legacy MCP clients. It verifies scoped
authorization, native image/audio and errors, real media tools with stubbed paid
provider boundaries, unchanged chosen model/voice, exact schemas, hook denial,
post-hook validation, session attribution, deletion/expiry/revocation,
cancellation, duplicate-write protection and bounded concurrency/argument
memory. It also exercises the real named-profile Board adapter and gateway
reverse-RPC correlation using an explicitly captured owner context; an unrelated
socket cannot answer the request. This is not a complete Desktop integration test.

`tests/test_image_analyze.py` covers the actual image tool's active provider
getters, allowed local files/data URLs, private URL rejection, invalid images,
oversize/decompression-bomb rejection, cancellation, and a directory-symlink
replacement during the secure open. Public CLI tests uncovered loss of `-m`
during sandbox re-execution; `tests/test_sandbox_cli.py` now pins preservation
of module/interpreter flags without changing sandbox permissions.

At this historical checkpoint, the tool-bridge acceptance checkbox remained open. Automatic per-turn grant
injection into managed coding sessions, propagation of the parent's structured/
exclusive tool restrictions, and end-to-end managed profile wiring remain
required. The public launcher only discovers the standalone gateway today;
internally supplied grants do not widen themselves through CLI options.
Local-file media reads currently require macOS/Linux descriptor primitives;
this is not cross-platform runtime evidence or proof of every paid provider.
The separate manifest/lifecycle, policy, diagnostics and final acceptance
items also remain open.

Verification: `uv run pytest tests/mcp/test_live_tool_bridge.py -q` — **47
passed**. Final full regression: `uv run pytest -q` — **5035 passed, 1 skipped,
12 deselected** in 97.99 seconds on macOS (65 tests added over the prior
committed baseline). This includes immediate runtime-stop revocation and
draining of cancelled calls after their grants have been removed. Targeted
Ruff checks and `git diff --check` pass. The broader loop/registry/CLI files
retain the same 32 pre-existing Ruff findings, verified against HEAD.

All gateway/provider tests use isolated temporary state; this work does not
deploy the branch to the user's running gateway. The final diagnostics audit
still needs missing/malformed local-media transport cases and structured
provider credential errors beyond the tested bearer/key patterns.

### Managed per-turn tool authority

Managed coding turns now receive a fresh live-runtime grant, constrained by
the registry's task-local owner, parent tool permissions and read-only sandbox
ceiling. The private listener needs no advertised gateway. Captured profile
context reaches the existing shared Board reverse-RPC adapter; another socket
cannot satisfy the request. Nested registry dispatch cannot widen the caller's
tool set. Concurrent sessions keep independent authority, and an overlapping
turn in the same coding session is rejected.

The managed client uses launch/thread overrides, disables other direct MCP
connections/plugins for that thread, and verifies the exact callback tool list
before starting a model turn. Registered third-party MCP tools are proxied
through Flowly's existing client, retaining consent, schema, native content and
transport policy. Read-only grants recheck remote annotations at dispatch.
The bridge is still a protocol boundary, not protection from arbitrary code
running with the user's full operating-system privileges.

`tests/mcp/test_managed_tools.py` exercises generic SDK stdio, real loopback
HTTP, the owning registry and named-profile Board adapter, third-party MCP
consent, native media/output schemas, cancellation/teardown ordering, empty
grants, catalog validation and packaged-executable selection. It also launches
the installed coding client against isolated configuration: a real MCP Board
call succeeds, stale callback paths/filters/profile variables are overridden,
other direct connections are disabled, and disk configuration is unchanged.

A second real-client test runs two turns in separate processes using a local
fixed-response HTTP provider (no paid model calls). It verifies history resumes,
grants rotate, the second turn can have zero granted tools, and the configured
model stays unchanged. Scanning the client's temporary state caught inherited
credentials in shell snapshots; managed launches now disable snapshotting and
clear the lease variable for ordinary shells. The test scans all client state
files to ensure neither transient credential was persisted.

The targeted managed suite contains **31 tests**; together with the session
suite, **54 passed**. It also exercises the actual agent-loop registration and
read-only configuration, disables account-backed app/plugin MCP sources in the
private child, and bounds the complete catalog verification with one deadline.
This is local macOS
transport/runtime evidence, not proof of every third-party client version,
paid provider, compiled Desktop binary or operating system. The separate
manifest/lifecycle, natural-language policy, diagnostics and final acceptance
requirements remain open. No merge, push or live-gateway deployment was done.

Final regression for this change: `uv run pytest -q` — **5066 passed,
1 skipped, 12 deselected** in 109.07 seconds, with 11 existing warnings.
The installed coding client used for the real-process tests is 0.149.0.
Targeted Ruff checks and `git diff --check` pass. The modified broad loop,
registry and existing session-test modules retain their same 36 pre-existing
Ruff findings, verified against the committed baseline.

### Exclusive policy and turn-long positive ceilings

`tests/test_exclusive_tool_policy.py` verifies equivalent English/Turkish
imperatives, short/qualified/full tool identifiers, source families, mixed
selectors, specific exclusions, conflicting exclusive clauses, unknown and
ambiguous aliases, Turkish suffixes/normalization collisions, quoted examples,
purpose/frequency clauses and bounded input. The original no-tools precedence
tests remain intact: structured deny wins, while existing broad structured
enablement does not disable tools merely from a prose global-deny phrase.
That broad enablement no longer erases positive exclusive constraints.

The real agent-loop tests deliberately emit out-of-scope calls from scripted
providers, intersect natural-language selectors with valid/empty/frozen/malformed
transport grants, and register a new tool during the provider await. The tool
cannot appear in later schemas or execute within that turn, but is usable on
the next independent turn. Concurrent turns expose and execute only their own
allowed tools. A positive ceiling is retained in task-local execution context,
not just a deny list expanded from an earlier registry snapshot.

The delegated transport test starts from the real parent message-processing
path with a Turkish exclusive request and a broader structured grant. It opens
the generic MCP SDK stdio callback, confirms only Board reading is granted,
successfully calls it, and rejects a Board write without changing the store.
All provider responses are local fixed fixtures; no paid model calls or live
gateway mutations are involved.

Targeted verification: `uv run pytest tests/test_exclusive_tool_policy.py
tests/test_no_tools_policy.py tests/test_profile_capability_policy.py -q`.
The grammar and boundaries are documented in the MCP feature guide. This is
not a promise to understand arbitrary ambiguous prose; clients can supply an
exact structured allowlist. Manifest/lifecycle, diagnostics and final overall
acceptance remain required.

Final verification: the targeted command above reports **131 passed**. Full
regression `uv run pytest -q` reports **5153 passed, 1 skipped, 12 deselected**
in 111.10 seconds, with 11 existing warnings (87 tests added). Targeted Ruff
checks and `git diff --check` pass; the broader agent-loop module retains the
same 20 pre-existing lint findings as the committed baseline. This change is
committed only; there was no merge, push or running-gateway deployment.

### Demand-driven runtime recycling (partial manifest/lifecycle acceptance)

Per-server idle/lifetime timers now retire transports without interrupting
admitted tool, resource or prompt requests. Lifetime expiry stops new admission
and drains the old generation; idle closure retains its tool catalog in memory
and waits for demand. Concurrent callers share one supervisor and one wake-up.
Connection/admission and teardown have separate finite deadlines, cancellation
is local to its caller, and shutdown is terminal. Keepalives do not count as
user activity. Timers default to disabled: restarting an arbitrary server may
lose that server's in-memory session state, which schema validation cannot restore.

Newly negotiated tools are compared against the calling handler's complete
contract, including the original input schema and permission metadata.
Changed/removed/ambiguous tools and withdrawn resource/prompt capabilities are
rejected before the remote operation. These local refusals do not trip the
remote failure circuit breaker. Metadata-only refreshes advance registry
generation, all bound registries receive fresh schemas under their own filters,
and repeated discovery does not forget owned tool names.

`tests/mcp/test_idle_lifecycle.py` exercises real public stdio in automatic,
explicit modern and legacy modes, plus real modern/legacy Streamable HTTP and
legacy SSE. It checks actual child PIDs are gone after idle teardown, concurrent
cold calls use one new process, cancellation preserves an unrelated live call,
old writes are never sent after a read-only annotation changes, unapproved
writes cannot wake an idle process, resource reads survive both deadlines, and
withdrawn resource capability refuses the call. Controlled lifecycle fixtures
also test hanging connection/close handshakes, cancelled drain waiters,
admission timeout without killing active work, weak consumer ownership, timer
bounds and terminal shutdown. No paid provider or running user gateway is involved.

At this historical checkpoint, disk-backed manifests, lazy startup after
restarting Flowly and concurrent initial discovery remained required. They are
addressed by the next section. The separate diagnostics and final acceptance
items remain open; this checkpoint does not prove those outcomes.

Verification: `uv run pytest tests/mcp/test_idle_lifecycle.py -q` — **48 passed**.
Full regression `uv run pytest -q` — **5201 passed, 1 skipped, 12 deselected**
in 126.42 seconds on macOS, with the same 11 existing warnings. Targeted Ruff
checks and `git diff --check` pass; `client.py` retains its single pre-existing
N818 finding, verified against the previous commit. No merge, push, provider
change or running-gateway deployment was performed.

### Persistent manifests and shared initial startup

Opt-in `lifecycle.lazyStart` now persists complete discovery hints with a
bounded `manifestTtl`. Valid hints register tools/capability utilities without
a transport at application boot; actual operations acquire a fresh connection
and run the contract checks above. Corrupt, expired, incomplete, oversized,
unsafe or identity-mismatched hints fall back to normal discovery. Probes bypass
cached readiness, and runtime health distinguishes manifest versus live catalogs.

`tests/mcp/test_manifest.py` exercises private descriptor-relative storage,
exact-name hashing, complete schema/annotation round trips, configuration,
environment/profile/SDK and OAuth-credential binding, cache entry and byte quotas,
atomic publication, rejection of symlinks/hardlinks/FIFOs/shared permissions,
parent-directory replacement, process death during a write, abandoned temporary
files and late publication by a separate process. No raw connection config or
credentials are persisted. Unsupported secure-filesystem primitives fall back
to live discovery; unknown packaged SDK identity prevents cross-process reuse.

`tests/mcp/test_lazy_discovery.py` uses real public stdio in automatic, modern
and legacy modes, including independent client processes and eight concurrent
consumers. It verifies first-call schema/read-only changes refuse the operation,
all consumers share initial startup and later idle wake-ups, cancelled startup
waiters are isolated, the final cancelled waiter joins subprocess cleanup,
changed configuration/profile cannot silently reuse a runtime, shutdown sees
unregistered startups, and an observation timeout retains ownership of ongoing
cleanup rather than allowing a replacement server to launch. The same combined
warm-cache/startup/idle lifecycle runs through real transports.

`tests/mcp/test_idle_lifecycle.py` additionally exercises lazy manifests over
modern/legacy Streamable HTTP and legacy SSE. `tests/mcp/test_oauth_recovery.py`
verifies cache reuse after real OAuth recovery and forced live discovery after
a persisted grant changes. A sanitized remote-name collision retains the same
first winner across refreshes; raw remote identity is part of the tool contract.

These tests use isolated local state and local SDK/auth servers. They are not
claims about every paid provider, compiled application or operating system.
The diagnostics audit and final combined acceptance remain open; no merge or
running-gateway deployment is part of this work.

Final verification: `uv run pytest tests/mcp/test_manifest.py
tests/mcp/test_lazy_discovery.py -q -W error::RuntimeWarning` — **69 passed**.
Full regression `uv run pytest -q` — **5275 passed, 1 skipped, 12 deselected**
in 150.85 seconds, with the same 11 existing warnings (74 tests added over
the preceding commit, including HTTP/SSE/OAuth and remote-name checks).
Targeted Ruff and `git diff --check` pass; the single pre-existing client N818
finding is unchanged. These changes are committed only: main and the user's
running gateway remain unchanged.

### Bounded failure diagnostics (partial logging acceptance)

Diagnostic text now handles credential-labelled JSON (including escaped keys),
nested/error-prefixed text, common token formats without leaking a long token's
suffix, HTTP authentication/cookies, URL userinfo and encoded query labels, and
complete/incomplete PEM private keys. Per-connection explicit environment/header
values, credential switches and URL credentials supplement pattern matching;
JSON/URL-escaped variants and authorization values without their scheme are
covered. The implementation does not scan unrelated account environments or
promise detection of arbitrary unknown, transformed or unlabeled secrets.

`safe_diagnostic` bounds individual input strings at 64 Ki characters and normal
output at 4096 characters. Oversized strings are omitted whole before redaction,
not sliced into a potentially exposed credential prefix. Structured values have
depth, traversal and collection limits; control characters are escaped so a
remote error cannot insert terminal controls or forge log lines. ExceptionGroup
inspection is iterative and bounded. Probe, discovery, dynamic-refresh and
tool-call errors use the owning connection's configured secret values; those
handlers no longer publish raw tracebacks. Sampling failure messages and selected
video-provider diagnostics use the safe renderer too.

Error tool results are handled before rich-content decoding: binary attachments,
structured payloads and vendor metadata on errors are not cached or relayed raw.
Successful content/metadata remain unchanged. `tests/mcp/test_diagnostic_transport.py`
verifies both the internal and native bridge envelopes over real automatic,
legacy and explicit-modern stdio, modern/legacy HTTP and legacy SSE SDK peers.
`tests/mcp/test_diagnostic_security.py` adds credential-format, complexity,
control-character, log-handler and scoped-configuration regression tests.

The live bridge now returns actionable failures for absent files, NUL paths and
symlink loops. It strictly checks every declared generated artifact instead of
using the channel helper's silent missing-file filtering. Malformed/empty media
envelopes fail; failure flags are evaluated before attachment extraction so a
successful-looking summary cannot turn a partially failed generation into a
success. `tests/mcp/test_live_tool_bridge.py` verifies these cases over real HTTP
and public stdio, plus the actual image/voice/video tool implementations with
only their paid-provider boundary replaced. Selected opaque provider keys are
redacted without building a new provider or changing the model.

Targeted verification: `uv run pytest tests/mcp/test_diagnostic_security.py
tests/mcp/test_diagnostic_transport.py tests/mcp/test_live_tool_bridge.py -q
--tb=short` — **121 passed**, three existing dependency warnings. This step adds
74 regressions. The log acceptance checkbox deliberately remains open: protocol
log notification handling and bounded/private subprocess stderr capture still
need implementation and real-transport evidence. The final combined acceptance
audit also remains pending. No merge or running-gateway deployment was performed.

Full regression `uv run pytest -q --tb=short` — **5349 passed, 1 skipped,
12 deselected** in 145.23 seconds, with the same 11 existing warnings. Targeted
Ruff checks and `git diff --check` pass. The sole client N818 finding is unchanged,
verified against the preceding commit. Main remains at `34932e9`.
