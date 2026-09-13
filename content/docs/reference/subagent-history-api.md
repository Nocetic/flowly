# Background task history

The shared feature RPC exposes the same task history over the authenticated
local gateway and cloud relay. This contract supplies data for mobile task
screens; it does not change the mobile layouts or select a different model.

## Reading tasks

- `subagents.list {}` returns all retained tasks, newest first, plus
  `schemaVersion: 2`, `counts` and `nextCursor`. Existing task fields remain.
- Optional `status`: `all`, `running`, `completed`, `failed` or `stopped`.
- Optional `limit` (1–200) and opaque `cursor` provide stable pagination.
  Equal creation times are ordered by run ID. Without `limit`, no page cap is
  imposed. This API does not limit chat history.
- `subagents.get {runId}` returns `{schemaVersion: 2, task}` with tool history.
- `subagents.result {runId, offset?, limit?}` reads a saved result. The default
  limit is 32,000 characters; the maximum is 200,000. Offsets count Unicode
  characters. Replies contain `content`, `offset`, `totalChars`, `nextOffset`.
  A null `nextOffset` means the result has ended.
- `subagents.cancel {runId}` reaches the owning worker, including external
  delegates, and waits for cancellation cleanup. Completed tasks return their
  actual terminal status. Legacy unambiguous short IDs remain accepted for
  cancellation; ambiguous prefixes are rejected.

Errors distinguish `INVALID`, `NOT_FOUND`, `UNAVAILABLE` and `CONFLICT`.
A missing or corrupt history is not reported as an empty task list. Legacy runs
without saved output return `resultAvailable: false`; no summary is invented.

## Task fields

`runId` is the canonical ID in snapshots and explicitly negotiated v2 events.
Legacy lifecycle events retain their eight-character `runId`. `shortId` is
display only for v2 clients. `label` is the task-derived name; `kind` distinguishes `subagent` and
`delegate`, with `agentId` identifying an external delegate. `parentSessionKey`
links the task to its originating conversation.

`status` is `running`, `ok`, `error`, `timeout`, `cancelled`, `interrupted` or
`unknown` for legacy terminal records. `createdAt`, `startedAt`, `endedAt` and
`updatedAt` are Unix seconds. `duration` is elapsed seconds, not an ETA.
`revision` increases on persisted changes; legacy rows begin at zero.

`activity.phase` can be `queued`, `thinking`, `tool`, `retrying`, `summarizing`,
`working` (an external CLI with no structured intermediate telemetry) or
`finished`. Optional fields include `iteration`, `tool`, `attempt`, `errorCode`.
These are observed phases, not a claimed percentage of completion.

`toolCount`, `completedToolCount` and `toolsUsed` summarize executed tools.
`toolTrace` on detail/completion includes names, status, byte counts and timing.
Arguments, commands, tool output bodies and model reasoning are not copied into
this trace. Tool discovery protocol bookkeeping is not an executed workspace
operation. Interrupting an active tool preserves its entry with a terminal
status.

`resultPreview` is the first 1,200 characters of the actual final output, not a
second model-generated summary. `resultChars` and `resultAvailable` describe the
separately stored full body; it is captured before parent-context truncation.
`artifactIds` links artifacts produced by the task. Failed tasks have a stable
`errorCode` and readable `error`; provider errors never count as success.

`deliveryState` is `pending`, `queued` or `not_required`. `queued` means handed
to the parent message bus/announce queue, not that the user has read a reply.
A parent delivery failure does not erase the task's saved result.

## Live updates and reconnect

Event delivery defaults to the installed apps' legacy protocol. To receive v2,
send `eventVersion: 2` with `subagents.list` or `subagents.get`. The parameter is
optional; older agents ignore it. Values other than integers 1 and 2 return
`INVALID`. Only a successful read negotiates the connection's event version;
failed requests cannot subscribe or change versions. An ordinary refresh
without this parameter preserves an existing selection. Explicit 1 downgrades.

Without an opt-in, `subagent.started` and `subagent.completed` retain the old
short IDs and field shapes, including delegate labels. Coalesced progress is
projected as an idempotent legacy start (or completion for terminal data), never
as a new full-ID row. This lets installed desktop clients match their initial
short-ID snapshot to completion without an app update. No new event names are
sent to legacy listeners. RPC snapshots retain their existing full IDs and
fields; new fields are additive, independent of event negotiation.

With v2 selected,
`subagent.started`, `subagent.progress` and `subagent.completed` carry the same
projection as snapshots, with `outcome` and a global `running` count. Completion
also includes `toolTrace`. Upsert by full `runId`; ignore
an equal or older `revision`. A client may first see a completion without a
start when a task finishes quickly.

A successful relay `list` or `get` establishes a 120-second observer lease for
that authenticated browser session. Refresh the snapshot at least once a minute
while the screen is visible. Up to 64 relay sessions can hold leases. A browser
disconnect removes its lease. Relay reconnect clears all leases: refresh before
resuming events. Direct gateway clients receive broadcasts on their existing
connection.

Gateway version selection belongs to the actual socket, not its reusable
`clientId`. Relay selection belongs to the individual browser session and its
lease, not the bot's shared relay socket. Expiry, disconnect and reconnect
discard relay selections. Reconnecting clients must explicitly select v2 again.
Selecting v2 on the primary runtime does not upgrade nested profile events;
each profile reader negotiates separately via `profiles.rpc` containing
`subagents.list` or `subagents.get`. The named-profile allowlist also permits
`subagents.result`; this extension is read-only, with no new spawn, cancel or
model-setting authority. Only successful history reads subscribe to profile
task events. Directory access alone and reads of other profiles do not.

The internal host-to-runtime bridge requests v2 even for a legacy external
reader; the authenticated transport projects the appropriate format for each
reader. Older child runtimes can still return legacy events. Relay profile
subscriptions have a 120-second lease, a 64-subscription bound and no offline
event replay. Disconnect invalidates pending subscription requests, so a slow
read cannot reopen a lease after the browser has left. The default profile's
internal event bridge is retained only for its subscribed consumers.

Delivery is best effort: each connection has a one-second send deadline, and
the task event publisher has a two-second deadline and a 128-run pending bound.
Newer snapshots replace pending updates for the same run; overflow evicts the
oldest pending run. No model or tool execution waits for network delivery.
Refresh on screen entry, foreground, reconnect, and periodically; snapshots
recover state after a dropped event.

## Storage and recovery

The active profile stores its index at `subagents/runs.json` and result bodies
in `subagents/runs-results/`. Result filenames are hashes of task IDs, never
client-supplied paths. Files are atomically replaced with owner-only permissions
and flushed to disk. A shared file lock protects concurrent read-modify-write
operations. Corrupt individual legacy rows are skipped; a corrupt index fails
closed without replacing its contents.

Announced terminal tasks, synchronous/silent tasks, and cancellations remain
for 24 hours after completion. Running tasks and terminal results still awaiting
parent delivery are retained. Warm and restarted processes apply the same
retention rule; the next write removes expired result bodies. Artifacts follow
the artifact store's own lifecycle.

A host restart marks unfinished tasks as failed with `process_restarted`, keeping
already-recorded tool activity. It does not rerun a task that might already have
performed side effects. Explicit cancellation and parent/shutdown interruption
are distinct terminal outcomes. POSIX external delegates run in their own process
session so cancellation stops descendants and reaps the direct child.
