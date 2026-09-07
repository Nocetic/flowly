# MCP connections implementation and acceptance

Scope: agent core and Desktop. iOS is explicitly deferred by the owner. Preserve
existing mail integrations, existing MCP configurations and unrelated worktrees.
No merge, deployment, account connection or external messages without approval.

## Worktrees

- Core: `codex/mcp-connections`, based on `codex/mcp-enterprise` at `886f073`.
- Desktop: `codex/mcp-connections`, based on Desktop `main` at `772ac65`.
- Core path: `/Users/hakanoren/flowly-repos/flowly/.codex-worktrees/mcp-connections`.
- Desktop path: `/Users/hakanoren/flowly-repos/flowly/.codex-worktrees/mcp-connections-desktop`.
- No commits, merge, push or release was performed. The core worktree includes
  the earlier MCP branch history; it is not based directly on core `main`.

## Required outcomes (completion requires evidence, not only implementation)

- [x] Accurate local/direct/relay connection state; stale responses cannot cross
  selected environments or resurrect a disconnected client.
- [x] One reusable MCP management/setup workflow for Dashboard and local profiles.
- [x] Native OAuth with the browser and callback on the user's Desktop, including
  remote gateways; bounded, cancellable operations with state/PKCE validation.
- [x] Failed/cancelled authorization preserves working configuration and tokens;
  existing helper-based connections migrate only through an explicit operation.
- [x] Explicit all/selected/none tool permissions enforced by the runtime; no
  accidental permission expansion when removing the final selected tool.
- [x] Live MCP status, actionable errors, retry/re-authorize and removal controls.
- [x] Safe targeted MCP reconfiguration without restarting mail/chat services;
  active calls, late discovery and concurrent config edits cannot resurrect tools.
- [x] Owner-managed external-agent credentials with scoped capabilities, expiry,
  revocation, secret-safe listing and backend enforcement on every operation.
- [x] Setup initiated from chat reaches a user-controlled setup surface, preserves
  the owning conversation/profile and resumes or cancels predictably.
- [x] Compatibility/capability negotiation for older embedded runtimes, across
  direct, local, relay and profile IPC surfaces.
- [x] Regression and acceptance tests cover cancellation, races, revocation,
  denied calls, reconnect and unchanged mail integration behavior.
- [x] Final handoff includes exact worktree paths, actual test results and a short
  manual test checklist. Do not claim untested live provider/device behavior.

## Sequence

1. Fix connection readiness and scope identity; add regression tests first.
2. Implement runtime-owned setup transactions and native OAuth handoff.
3. Implement explicit permission policy and targeted lifecycle application.
4. Reuse the setup/management UI across Desktop surfaces, including chat.
5. Implement external-agent access management and enforcement.
6. Audit the complete scope, run regression/acceptance checks and document gaps.

## Evidence log

- Initial read-only review: 100 existing Desktop unit tests passed in five suites.
  This is a baseline, not evidence that the new workflow works.
- Worktrees created without changing either main checkout or the original MCP
  enterprise worktree. Implementation and new test evidence follows below.

### Core and Desktop integration checkpoint

- Core: explicit permission modes and runtime enforcement; private staged OAuth
  credential slots; bounded Desktop handoff; setup review/confirm/cancel;
  catalog credentials remain draft-only before consent; explicit helper migration.
- Core: authenticated owner RPC (`mcp.capabilities`, `mcp.connections.*`,
  `mcp.setup.*`) bound to the selected profile runtime. Short setup/status RPCs
  keep long human waits out of the gateway receive loop. Accepted changes drain
  on service shutdown and never request a gateway restart.
- Core: targeted MCP replacement withdraws stale tools, drains sent calls,
  rebinds consumer registries and rejects superseded discovery. Explicit retry
  verifies the real transport, even with a cached lazy manifest.
- Desktop: one shared connection panel now replaces the separate Dashboard and
  profile MCP implementations. Supports permission review, native sign-in,
  catalog/manual connections, live status, retry, disable, removal and explicit
  helper migration. English/Turkish/Spanish copy included.
- Desktop: private loopback callback receivers, owner-window/subframe IPC gates,
  state/redirect/PKCE URL validation, bounded deadlines and cleanup on navigation,
  cancellation and renderer loss. The one-time callback crosses only the captured
  authenticated target transport; access tokens and PKCE verifiers stay on the
  agent host. No host-browser fallback is invoked by the new setup UI.
- Verification: 152 OAuth/credential/manifest/legacy RPC tests passed in the
  earlier checkpoint; 120 lifecycle/registration/lazy/real stdio+HTTP/SSE tests
  passed after fixing a closed-loop double-shutdown regression; 24 current
  setup/owner RPC/targeted reload tests passed after live transport verification.
  These counts overlap and must not be summed.
- Desktop verification: 128 focused readiness, transport, attention, profile RPC,
  controller and shared UI tests passed; 14 actual loopback OAuth receiver tests
  passed separately. Main/preload/renderer TypeScript checks passed.
- Test fixtures use temporary state and local peers, not paid model APIs, live
  mail, real account authorization, or the user's running gateway.

### External-access and visual acceptance checkpoint

- Added owner-only `mcp.access.catalog/list/create/revoke` RPC and matching
  Desktop/profile method allowlists. The capability becomes true only when the
  selected live gateway has the external service. No gateway restart requested.
- Desktop now creates a distinct cryptographically random key per client, sends
  only its SHA-256 digest to owner RPC, retains the one-time secret only in the
  mounted panel, and retries an uncertain creation with the same ID and digest.
  Tool selection starts empty; all grants use exact names and a 1/7/30/90-day
  lifetime. Listing exposes no token or hash. Revoke requires confirmation.
- Core stores private atomic profile-local digests and metadata, with bounded
  records and strict validation. Expired/revoked keys cannot be used or revived
  by retrying creation. Authority is rechecked at admission, after discovery,
  before live tool dispatch, while calls run and before returning results.
- The gateway's new `/mcp` route speaks SDK-owned native Streamable HTTP. It
  accepts scoped keys, never the gateway administrator token. It rejects
  browser Origin requests, query credentials and non-loopback plaintext. The
  stateless bounded adapter does not share protocol sessions between clients.
- External live actions run through the existing tool registry, policy hooks
  and session context. Read, event and control clients are explicitly bound to
  the owning profile instead of consulting a later global environment selection.
- Fixed an in-flight revocation bug: cancellation of the child invocation must
  return a native MCP error, not silently abandon the HTTP response.
- Added `flowly mcp connect` for stdio-only clients. It uses the exported scoped
  endpoint/key from environment variables and never falls back to a local admin
  credential. Both auto-negotiated and legacy stdio clients passed real CLI
  subprocess round trips against the temporary HTTP peer.
- Native HTTP is the default exported Desktop configuration; stdio compatibility
  is explicit and requires Flowly CLI on the client machine. Remote use requires
  a reachable HTTPS gateway/proxy. Relay management alone does not publish an MCP
  URL. No account, public endpoint, proxy or live key was configured in this task.
- Visual QA used the actual React panel with synthetic RPC fixtures in Turkish,
  in dark and light themes. Connection cards, permission review, external key
  form and revocation controls were inspected. Fixed the fixture-only transparent
  window background and localized reconnecting states. Preview tab and temporary
  Vite server have been closed.
- Latest broad core verification: **801 passed, 4 skipped** in 126.72 seconds.
  The skips need an explicitly supplied external-client launch adapter. This run
  includes external HTTP, profile isolation, denial and cancellation tests but
  predates the additional stdio adapter tests. Those subsequently passed in the
  **13-test** external HTTP/stdio suite. Counts overlap; do not add them.
- Latest Desktop checks: **35 tests** in seven setup/readiness/profile/UI suites,
  then **19 tests** in the external credential-helper and UI suites after adding
  stdio export. All main/preload/renderer TypeScript checks passed; renderer was
  checked again after the compatibility control. Renderer production build passed
  (5,786 modules, 2.84 seconds), with the existing deprecation/chunk-size warnings.
- Earlier isolated Desktop-to-core OAuth acceptance passed (one cross-runtime
  test): real Desktop listener/controller, SDK discovery/DCR/PKCE exchange and
  temporary MCP HTTP service. No tool was available before consent; only the
  selected tool executed, empty permissions withdrew it, and mail config stayed
  unchanged. This was a fake OAuth provider, not a real account sign-in.
- Lost setup-begin acknowledgments now recover through the same request ID;
  cancellation-by-request-ID prevents delayed frames from starting a cancelled
  setup. Permission review removes retired tools from the selection and preserves
  an explicitly empty selection. Focused regression tests cover both cases.

### Final implementation audit

- Chat now has `mcp_connection`: an inert proposal bound to the runtime's trusted
  conversation identity. It cannot supply environment secrets or grant tools.
  It is an interactive toolset member, unavailable to scheduled runs. Desktop
  shows a review card in primary and side chats, and the shared management panel
  also lists pending requests. Human verification precedes tool-permission
  confirmation. The waiting tool returns the actual saved/connected/cancelled
  outcome and sanitized failure information to its original conversation.
- The chat dialog uses the same setup controller and permission component as
  Dashboard/profiles, but hides unrelated management and external-key controls.
  Late lists and cleanup stay bound to the captured target. Polling errors clear
  after recovery without hiding a failed user action. Older runtimes do not
  receive unsupported setup mutations. Actual Turkish dark/light dialog and
  permission screens were inspected with synthetic data; no browser errors.
- Core configuration writers now share a bounded SQLite write lease with
  Desktop's general settings writer and repair helper. The existing core-only
  file lock remains for compatibility. This uses OS-released transactions, not
  TTL-based deletion of supposedly stale locks. Verified SQLite availability in
  the actual Electron runtime (Node 22.21.1), not only the system Node runtime.
- A stale Pydantic settings object preserves newer MCP state on unrelated saves;
  an intentional conflicting MCP edit fails and requests a fresh read. Raw core
  editors use compare-before-write snapshots. Desktop general settings preserve
  the latest MCP maps under the shared lease and cannot restore an old grant or
  removed connection. They write private temporary files and atomically replace
  the configuration. Repair/recovery cannot race a current MCP publication.
- Native HTTP disconnect testing found and fixed a capacity leak caused by SDK
  cancellation interrupting async cleanup. Capacity now follows completion of
  the actual invocation. Disconnect, shutdown, revocation, expiry, denied tools,
  empty Origin headers and forged forwarding headers are covered by tests.
- Malformed configured rows no longer crash connection listing; owner listings
  use the service's own configuration snapshot. Secret-bearing launch arguments
  and headers remain absent from listings. Provider tool identifiers remain
  exact for auditability; surrounding permission explanations are localized.
- The remote isolated-profile host contract was reviewed and remains unchanged:
  its shared model-facing delegation allowlist must not gain installation or
  credential administration. Local profiles use the authenticated owner IPC;
  direct/relay gateways use their authenticated feature RPC. Remote profile-host
  administration is not silently treated as equivalent to local profile IPC.

### Requirement-to-evidence map

| Area | Authoritative implementation and verification |
| --- | --- |
| Readiness and target isolation | Desktop `gateway-readiness`, `gateway-feature-client`, `local-feature-readiness`, setup-controller and profile-runtime tests |
| Shared management UI | `McpConnectionsPanel`, `McpConnectionsTargets`, Dashboard `MCPTab`, `ProfileCapabilitiesDialog`; shared panel tests and browser fixture |
| Native authorization and preservation | Core `test_oauth_handoff`, `test_oauth_slots`, `test_setup`; Desktop OAuth listener/handler/controller tests; real cross-runtime test with a synthetic authorization service |
| Permissions and live changes | `test_explicit_permissions`, `test_targeted_reload`, `test_setup`; existing real stdio/HTTP/SSE lifecycle tests |
| No mail/chat restart | `gateway_cmd.on_tool_access_reload` only refreshes the model configuration and tool availability; owner RPC tests assert no restart; setup and cross-runtime tests preserve mail configuration |
| Concurrent settings writers | Core `test_config_transactions`; Desktop `config-write.test`; cross-runtime test locks in both directions and attempts a stale Desktop save after permission withdrawal |
| External clients | `test_external_access`, `test_external_runtime`, `test_external_mcp_http`; actual HTTP SDK and public stdio CLI subprocess calls, revocation and disconnect tests |
| Chat ownership and cancellation | `test_chat_setup`, `McpChatSetupPrompt.test`, `McpConnectionsPanel.test`; cross-runtime test proposes from the model tool, completes Desktop OAuth, resumes and cancels a later request |
| Compatibility | Owner capabilities, direct/relay adapter methods, local/profile IPC allowlists, old-runtime and late-response tests |

### Operational boundaries and release checks

- This is protocol authorization, not an OS sandbox against a process that can
  edit the owner's files. Manual file replacement or an older application that
  does not participate in the shared write protocol is outside that guarantee.
  Ship/update core and Desktop together; do not mix this feature with an older
  running Desktop against the same profile.
- A key has at most 64 exact tools and a maximum 90-day lifetime. The store keeps
  up to 256 records and is bounded to 1 MiB. Revoked/expired records are retained
  to prevent a retried creation from reviving old authority; capacity exhaustion
  fails closed. Automatic tombstone pruning/long-term archival is not included.
- A lost creation reply is retried with the same key ID/digest. If the user leaves
  before saving the one-time secret, it is intentionally not recoverable from
  storage. The access list remains available to revoke that unsaved key. The UI
  explains this; it never generates a replacement key on an uncertain retry.
- Remote external clients need an explicit reachable HTTPS `/mcp` endpoint. A
  TLS reverse proxy may forward **only `/mcp`** to the loopback gateway and set
  the upstream Host to its loopback address. Keep the gateway port private,
  terminate TLS at the trusted proxy, preserve Authorization/Accept/content-type
  headers and disable proxy request retries for writes. Do not publish `/control`
  as part of MCP setup. Forwarded scheme headers alone never authorize plaintext
  requests. Relay management does not automatically publish this endpoint.
- No public proxy, live provider account, real external desktop agent, paid model
  or live mail workflow was configured by this implementation task. Real provider
  redirect-registration constraints, packaged macOS/Windows/Linux behavior and
  public HTTPS deployment remain owner-run release checks, not claimed test
  results. iOS remains explicitly deferred; no website/iOS change was made.
- Node emits an experimental-API warning for its bundled SQLite API; its use was
  verified in the installed Electron runtime. Recheck that capability when
  upgrading the Electron/Node baseline. Renderer build also retains the existing
  Vite/Tailwind/chunk-size warnings.

### Final verification results

- Desktop: **232 passed in 18 suites** after the shared write-lease changes.
- Cross-runtime acceptance: **1 passed** after adding bidirectional core/Desktop
  lock contention and stale Desktop save checks to chat → OAuth/PKCE → MCP.
- Main, preload and renderer TypeScript checks passed. Renderer production build
  passed with **5,788 modules** (2.65 seconds). Changed core modules passed Ruff;
  both worktrees passed `git diff --check`. Temporary browser tabs and this task's
  localhost preview server were closed; unrelated listeners were left alone.
- Final combined core run: **941 passed, 4 skipped** in 136.97 seconds. It covers
  all `tests/mcp` plus owner MCP RPC, configuration loader/integration writes,
  provider reload, tool routing and registry suites. Four optional external-client
  acceptance cases require an explicitly supplied isolated launch adapter and
  were not run; native SDK HTTP and actual public stdio subprocess cases did run.
  Earlier checkpoint counts overlap and must not be added together.
- Implementation and local acceptance are complete for the agreed core/Desktop
  scope. This is not a claim that untested real providers, packaged devices or
  every third-party client have passed release qualification.

### Manual checks before release

1. On the chosen test agent/profile, add a trusted service; finish Desktop OAuth,
   choose one read tool and verify only it is available. Cancel another setup and
   confirm the previous working connection remains intact.
2. Remove the last tool permission; confirm it is unavailable without restarting
   the gateway. Check mail/chat still receive normally using the owner's own test.
3. Create a short-lived external key, connect an external MCP client using the
   copied HTTP or stdio config, and try an unselected tool (must fail).
4. Revoke that key during an in-flight test call; confirm it returns an error and
   later calls are denied. Other client keys and profiles must remain unaffected.
5. Change selected agent/profile while setup or a status request is pending;
   no result or authorization may be applied to the new target.
6. Ask for a connection from chat. Review and allow only one tool, then check the
   same conversation resumes. Cancel a second request; it must end without saving.
7. While MCP permissions change, save an unrelated Desktop preference; the new
   permissions and mail settings must remain unchanged. Repeat on a named profile.

### 2026-09-07: OAuth registration interoperability follow-up

- A real Linear setup reached token exchange but failed with HTTP 400 and the
  standard error `invalid_request`. That code alone does not establish which
  parameter was rejected. The original provider description was not retained;
  the exact internal rejection reason cannot be reconstructed from that code.
- New registrations now explicitly request `token_endpoint_auth_method=none`
  for the local PKCE client. Omitting this field permits a confidential-client
  default under RFC 7591. The SDK already sends `application_type=native`.
- The server's explicit negotiated `client_secret_basic`/`client_secret_post`
  response still controls login and refresh. An explicit `none` is not silently
  changed merely because a server also returns a secret. Existing credentials,
  permissions, mail configuration and Desktop code are unchanged by this patch.
- Error diagnostics retain only standard error codes, fixed parameter names
  mentioned in the provider description, a bounded auth-method label, and a
  boolean indicating whether a secret was issued. Raw descriptions, client IDs,
  secrets, authorization codes and tokens are never rendered. Mentioned fields
  are diagnostic context, not a claim about the root cause.
- A strict local HTTP authority exercises public registration, omitted response
  method, explicit public registration with a superfluous secret, and negotiated
  Basic/Post authentication. Each case verifies PKCE, exact redirect, resource,
  scope, token exchange and refresh after provider restart. Owner RPC/chat tests
  verify that safe errors arrive without saving a failed setup or changing mail.
- Focused regression verification: **179 passed**, with three existing dependency
  deprecation warnings. Changed Python modules/tests pass Ruff and the worktree
  passes `git diff --check`.
- Live verification on 2026-09-07 (Europe/Istanbul): the owner restarted this
  worktree's gateway at 15:26 and completed a fresh Desktop setup at 15:27.
  The persisted chat tool result reports `complete`, `saved=true`,
  `connected=true`, and no error. Its referenced credential slot records auth
  method `none`, no client secret, and both access/refresh tokens; only presence
  flags were inspected, never credential values in diagnostic output.
- An independent non-interactive process using the same production Flowly MCP
  discovery/tool wrapper loaded the saved owner policy, registered 65 selected
  tools, and successfully called the permitted read-only `list_teams` with
  `limit=1`. The response was non-error and contained one team; team contents
  were not emitted. The verification client was closed and the owner's gateway
  remained running. No Linear records or user permissions were changed by this
  diagnostic call.
- This verifies the repaired real-provider setup and reuse for a permitted read,
  not every selected write tool, real-provider expiry/refresh, or packaged-device
  release qualification. The live provider granted `read write`; the selected
  tool policy includes write/delete tools, while resource/prompt access is off.

### 2026-09-07: Remaining acceptance tests, isolated follow-up

- Added tests only: owner-key installed-client acceptance, bounded HTTP
  saturation/soak, and real email-channel coexistence with a local Gmail API
  fixture. No production code or Desktop source changed in this follow-up.
- Final selected core regression: **984 passed, zero skipped** in 147.30 seconds.
  The previously optional four client cases now ran using an explicitly selected
  installed Messages client. Eight new owner-key cases also passed for that
  client; a separate installed GenerateContent client passed all **12 cases**
  in 34.61 seconds. Local fixed-response model APIs avoid paid/account use.
- Cases include actual HTTP/stdio read/write, permission denial, in-flight
  revocation/expiry, and isolation of an unaffected key. Existing installed
  coding app-server round trips passed as part of the core selection.
- Desktop: **117 passed in 14 suites**, including the real Python/TypeScript
  OAuth loopback test. Main, preload and renderer TypeScript checks passed.
  These are not fresh packaged native-window or cross-platform release tests.
- Separate 60-second local HTTP soak: **8,508 successful calls**, concurrency
  four, p95 0.008 seconds, seven asyncio tasks before and after. A separate
  saturation/recovery case passed 400 calls after rejecting an over-limit call
  and withdrawing a blocked key. These are synthetic transport measurements,
  not production throughput or long-term endurance claims.
- The real email channel and message bus received/replied through a local Gmail
  API fixture before and after key revocation without restarting their tasks.
  No real mail, provider records or paid model calls were sent. The owner's
  removed connection stayed removed; personal config hash was unchanged and
  the same gateway process remained listening.
- New tests pass Ruff and both worktrees pass `git diff --check`. Three known
  Python dependency deprecation warnings and the Node SQLite warning remain.
  Historical counts overlap; this core selection differs from the earlier
  selection and must not be summed with it.
- Full evidence, reproduction and owner-dependent live-account/device checks:
  [MCP acceptance follow-up](MCP_ACCEPTANCE_2026-09-07.md). Real OAuth refresh and
  provider-side revocation, live mail, packaged devices, reachable remote HTTPS
  and longer endurance remain unclaimed. No merge, release or restart occurred.

### 2026-09-07: Six live-audit findings corrected

- Conversation deletion now atomically revokes all of that owner's durable MCP
  credentials before removing its history. It uses the SessionManager's captured
  profile path, including gateway fallback deletion paths. A corrupt access file
  or failed revocation write aborts deletion rather than leaving reusable keys.
  Recreating the same session key or restarting the service cannot restore those
  revoked credentials. Keys belonging to other conversations remain usable.
- Key creation rechecks the owner's creation timestamp under the same session
  write lease after asynchronous discovery. A delete/recreate during discovery
  rejects the old pending request. Normal message saves do not invalidate keys.
  Cancelled background writers are drained even if durable deletion fails.
- Legacy active records whose owner is absent are displayed as unavailable,
  never active, and remain explicitly revocable. This is an effective read-only
  status, not an automatic migration of historical records. Manually editing or
  restoring profile files outside the app is outside this protocol boundary.
- Desktop asks users to choose a conversation before showing its tools. The
  profile-wide conversation read/search warning remains explicit. Loading,
  empty-conversation, empty-search and discovery-error states are explained.
- Catalogs refresh every four seconds, when reopening creation, and on an
  explicit refresh action. Removed tools lose draft selections; future tools
  never acquire permissions automatically. Missing owners clear the draft after
  unscoped discovery. Late target/owner responses are ignored, and an uncertain
  creation acknowledgement keeps its original retry request immutable.
- Tool lists show their matching total and offer additional 200-item pages.
  A visible 0–64 selection count and limit explanation prevent a 65th selection;
  the server's existing maximum is unchanged. Key/config clipboard feedback is
  distinguished, and outdated completions or clipboard failures cannot report
  success for the wrong content. All new messages include English, Turkish and
  Spanish translations.

Verification after these changes:

- **1,004 core tests passed, zero skipped**, in 152.24 seconds: the full MCP
  directory, owner MCP/provider RPC, tool routing/registry, web messages/media,
  and session-deletion integrity. Installed Messages client cases were enabled.
- A second installed GenerateContent client passed **14 tests** in 40.70 seconds.
  Both installed clients exercised HTTP and stdio owner deletion during an
  in-flight call, immediate recreation, continued denial, and other-key isolation.
  Fixed-response local model peers were used; no paid model/account calls.
- **131 Desktop tests passed in 14 suites**, including Python/TypeScript OAuth
  roundtrip and 19 external-access UI cases. Main, preload and renderer TypeScript
  checks passed. Changed external-access Python modules/tests pass Ruff; both
  worktrees pass whitespace checks. Existing dependency deprecations remain.
- The running native Desktop displayed the new choose-conversation explanation,
  refresh action and 0/64 counter. The empty creation draft was cancelled without
  issuing a key. Dynamic selected-tool, pagination and clipboard behavior was
  verified in isolated renderer tests, not by creating personal credentials.
- Personal configuration SHA-256 remained unchanged and the same gateway process
  stayed running. Existing connections and real mail were untouched; the core
  regression includes the isolated mail-channel coexistence test.
- **Activation still requires restarting the core gateway**: its running process
  predates these changes. Desktop development hot reload showed the UI update.
  No commit, merge, deployment or personal gateway restart was performed. These
  findings are fixed and regression-tested, not a blanket production certification
  for real-provider refresh, remote HTTPS or packaged operating systems.

### 2026-09-07: Post-restart Desktop credential lifecycle, live verification

- The owner restarted the CLI gateway at 17:04 local time from the corrected
  core worktree. Health, owner RPC, native OAuth capabilities and external access
  were available; both existing service connections reported connected.
- Prepared one uniquely named empty test conversation through the gateway's
  session RPC. The owner selected it and a one-day lifetime in native Desktop;
  computer control selected only `board_list` and, after explicit owner approval,
  created the key using the actual Desktop form. Configuration-copy feedback
  appeared on the correct button. Native select controls and the system/browser
  clipboard boundary required owner assistance; this was not a fully unattended
  computer-control run.
- The owner pasted the copied configuration into a temporary loopback-only test
  page. A real MCP SDK client initialized against the running gateway, listed
  exactly `board_list`, successfully executed it, and confirmed an unselected
  `messages_read` call was rejected as not permitted. Lifetime was 86,400 seconds.
  Neither raw credentials nor personal Board response bodies were logged/saved.
- After explicit approval, the normal gateway session-delete RPC removed only
  that empty test session. The existing key became `revoked` automatically and
  the same in-memory bearer received HTTP 401. Recreating the identical empty
  session key did not restore authority: another real request still returned 401.
  Desktop's polled credential row changed to Revoked without reopening the panel.
- Removed the recreated empty fixture, dismissed the one-time key view, closed
  the test browser tab and stopped the temporary loopback process. No active
  external credentials remain; the revoked test record is retained as a durable
  tombstone. Existing connection configuration hash was unchanged and both
  service connections remained connected. No real mail or Board writes were sent.
- This closes the live Desktop-to-gateway credential creation/read/permission/
  deletion/recreation acceptance gap. No merge or release was performed; broader
  remote HTTPS, real-provider refresh and packaged-platform qualification remain
  separate checks, not inferred from this local result.
