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
- [ ] The coding-agent tool bridge exposes vision/image, speech, and
  task-board capabilities where available, preserving authoritative schemas and
  execution permissions. Stateful tools reach the owning live runtime.
- [x] MCP server requests for user input/consent reach the owning surface;
  untrusted write tools cannot execute without approval.
- [x] OAuth picks up cross-process token changes, coordinates concurrent 401
  recovery, and distinguishes expired sessions from expired credentials.
- [ ] Persistent tool manifests support lazy server startup and bounded idle/
  lifetime recycling, without stale schemas or duplicate subprocesses.
- [ ] Explicit exclusive tool requests resolve consistently in English/Turkish,
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
