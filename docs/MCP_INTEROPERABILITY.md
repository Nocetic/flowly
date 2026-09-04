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
- [ ] The coding-agent tool bridge exposes browser, vision/image, speech, and
  task-board capabilities where available, preserving authoritative schemas and
  execution permissions. Stateful tools reach the owning live runtime.
- [ ] MCP server requests for user input/consent reach the owning surface;
  untrusted write tools cannot execute without approval.
- [ ] OAuth picks up cross-process token changes, coordinates concurrent 401
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

## Verification record

The remaining unchecked outcomes are pending. The previous baseline is commit `3c58b18`; its suite
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
