---
title: Bots & groups API
eyebrow: Reference
description: The RPC surface Desktop and iOS use to manage bots and groups — methods, capabilities, error codes, the health handshake, and every hard limit.
---

This is the contract behind [Bots](/docs/features/bots) and
[Bot groups](/docs/features/bot-groups). Desktop and the iOS app both speak it;
it is documented here because anything reaching a Flowly gateway has to know
the same bounds they do.

## Bot methods

Everything a client may ask one bot to do. A method not on this list is
refused rather than forwarded, and each carries its own timeout ceiling.

| Area | Methods |
|---|---|
| Providers & models | `provider.list`, `provider.active`, `model.list` |
| Conversations | `sessions.list`, `sessions.delete`, `sessions.model.get`, `sessions.model.set` |
| Chat | `chat.history`, `chat.inflight`, `chat.send`, `chat.abort` |
| Media | `media.read` |
| Shell approvals | `exec.approval.list`, `exec.approval.resolve`, `exec.policy.get`, `exec.policy.set` |
| Codex | `codex.policy.get`, `codex.policy.set` |
| Tools | `tools.access.get`, `tools.access.set` |
| Questions | `agent.clarify.list`, `agent.clarify.resolve` |
| Plan mode | `plan.get`, `plan.resolve`, `plan.resume`, `plan.mode.get`, `plan.mode.set` |
| Goals | `goal.get`, `goal.pause`, `goal.resume`, `goal.stop` |
| Scheduled jobs | `cron.list`, `cron.add`, `cron.update`, `cron.remove`, `cron.run`, `cron.output` |
| Commands | `commands.list` |

Most carry a 30-second ceiling; `chat.send`, `model.list`, `plan.resume`,
`goal.resume` and `cron.run` are allowed 60 seconds.

### Permission values

| Setting | Accepted values |
|---|---|
| Shell security | `deny`, `allowlist`, `full` |
| Shell asks first | `off`, `on-miss`, `always` |
| Codex approval | `on-request`, `never`, `auto-review`, `granular` |
| Codex sandbox | `read-only`, `workspace-write`, `full-access` |

### Internal sessions

Some sessions belong to host orchestration rather than to a reader, and are
not offered as conversations: those prefixed `desktop:profile-inbox:`,
`desktop:profile-room:` and `desktop:profile-task:`. Sessions reached from a
client carry a `desktop:`, `web:` or `ios:` prefix.

## Group methods

| Method | What it does |
|---|---|
| `profiles.rooms.list` | Every group, with or without messages |
| `profiles.rooms.get` | One group |
| `profiles.rooms.history` | A page of durable history, oldest-first within the page |
| `profiles.rooms.create` | Create a group |
| `profiles.rooms.update` | Rename, change members, change mode or reply policies |
| `profiles.rooms.delete` | Delete a group and its attachments |
| `profiles.rooms.prepare` | Warm the members before a first message |
| `profiles.rooms.send` | Post a message and start the turn it triggers |
| `profiles.rooms.stop` | Stop an in-flight turn |
| `profiles.rooms.import` | Adopt groups from another install |
| `profiles.rooms.approval.resolve` | Answer a member's approval request |
| `profiles.rooms.clarify.resolve` | Answer a member's question |
| `profiles.rooms.storage` | What groups are using on disk |

## Capabilities

A host advertises what it supports, so a client never has to guess or infer it
from a version number:

```json
{
  "modes": ["panel", "council"],
  "maxMembers": 6,
  "councilRounds": 3,
  "councilTurns": 10,
  "summaries": true,
  "historyPagination": {
    "defaultPageSize": 50,
    "maxPageSize": 100,
    "cursor": "opaque-v2"
  },
  "retention": {
    "model": "retain-beyond-window",
    "liveWindow": 1000,
    "durableHistory": true
  },
  "usage": {
    "tokens": true,
    "cost": "catalog-priced",
    "memberBreakdown": 24
  },
  "memberPolicies": {
    "values": ["always", "mentioned"],
    "default": "always",
    "everyoneOverrides": true,
    "silentWhenNoneAlways": true
  },
  "errorCodes": ["…"],
  "roomEvents": ["full-v1", "delta-v1"],
  "storage": "sqlite-wal",
  "legacyJsonMigration": "verified-copy-preserve-source"
}
```

`modes` advertises both shapes the host can run, and clients send `mode` on
create and update. A payload without it means `panel`, which is what every
group did before the field existed — so a client that does not know it still
creates groups that behave correctly, and an update that omits it leaves the
shape alone rather than resetting it.

Two of these deserve reading twice:

- **`retention.liveWindow` is not how many messages exist.** It is what the
  group carries in memory and in a snapshot payload. Durable history is not
  bounded by it. A client that treats the window as the total will report a
  group as shorter than it is the moment it outgrows the window.
- **`usage.cost` is `catalog-priced`.** Tokens are counted for every group;
  money appears only when the model catalogue can price the models involved.
  A client must be ready to render a group that reports tokens and no cost.

### Member reply policies

`memberPolicies` describes who answers a message that named nobody:

- `values` — `always` and `mentioned`
- `default` — `always`, which is what every group did before the field existed,
  so a client that does not know the field still describes its groups correctly
- `everyoneOverrides` — an explicit `@everyone` reaches members set to
  `mentioned`
- `silentWhenNoneAlways` — a group where nobody is `always` records an
  unaddressed message and starts no run. Clients should say so rather than
  leave somebody waiting for a reply that is not coming.

Policies are sent as a map of member name to policy. Absent means unchanged on
an update, not cleared — otherwise an older client renaming a group would
quietly make every member answer again.

## Error codes

A failure is named by a code, and the client translates the code. The sentence
that travels with it is for a log, not for a reader.

| Code | Means |
|---|---|
| `HOST_STOPPED` | The bot host is no longer running |
| `METHOD_NOT_ALLOWED` | The method is not available on this host |
| `PROFILE_NOT_FOUND` | One of the selected bots no longer exists |
| `ROOM_ATTACHMENT_TOO_LARGE` | An attachment exceeded 25 MB |
| `ROOM_BUSY` | The group is already answering |
| `ROOM_BUSY_DELETE` | Cannot delete while a turn is running |
| `ROOM_BUSY_MEMBERS` | Cannot change members while a turn is running |
| `ROOM_CURSOR_INVALID` | The history cursor does not belong to this group |
| `ROOM_HISTORY_UNAVAILABLE` | Durable history could not be read |
| `ROOM_INVALID` | The group id is not a valid one |
| `ROOM_LIMIT` | 200 groups already exist |
| `ROOM_LIMIT_IMPORT` | An import would exceed that limit |
| `ROOM_MEMBERS_INVALID` | The member list is not valid |
| `ROOM_MEMBERS_TOO_FEW` | Fewer than two members |
| `ROOM_MEMBER_FAILED` | A member could not answer |
| `ROOM_NOT_FOUND` | No such group |
| `ROOM_REQUEST_CLOSED` | The approval or question is no longer open |
| `ROOM_START_FAILED` | A member could not be started |
| `ROOM_STOPPED` | The turn was stopped |
| `ROOM_STORE_CONFLICT` | Another process wrote first |
| `ROOM_STORE_INVALID` | The group database is damaged or unreadable |
| `ROOM_STORE_LIMIT` | The group database reached its safe size |

`INVALID_PARAMS` is deliberately absent from that table. It means a client sent
something malformed, so the person reading it is the one debugging that client,
and English is the right language for them.

## Gateway identity

A running gateway states what it is on `/health`, which needs no token:

```json
{
  "status": "ok",
  "service_id": "ai.flowly.gateway",
  "version": "3.2.0",
  "runtime_owner": "desktop",
  "auth_required": false,
  "capabilities": ["tool_events", "queue_while_busy", "…"]
}
```

- **`service_id`** is always `ai.flowly.gateway`. A client that sees a different
  value should treat the response as coming from something that is not Flowly.
- **`version`** is omitted by a source checkout that cannot state a real
  version, rather than reported as `0.0.0-dev` — which would compare as older
  than every release.
- **`runtime_owner`** is `desktop`, `cli` or `manual`, taken from the
  environment the starter set, or from the executable's own path. It is omitted
  when neither says anything definite, so a reader falls back to its own
  classification rather than acting on a guess.

## Storage

Groups are kept in one SQLite database, write-ahead-logged so several processes
can use it safely.

| | |
|---|---|
| Schema version | 4 |
| Readable schema versions | 1, 2, 3, 4 |
| Maximum database size | 2 GB |
| Maximum group record | 256 KB |
| Maximum message record | 4 MB |

Migrations run forward one step at a time and stamp each version before the
next begins, so a crash between two steps cannot leave finished work behind an
unchanged version number.

A database written by a **newer** Flowly is left untouched by an older one,
which reports an unrecognised schema rather than editing it. Groups stop
working on the older build; the data is not damaged.

## Limits

| | |
|---|---|
| Groups per installation | 200 |
| Members per group | 2–6 |
| Live window | 1,000 messages |
| Live window size | ~2 MB, never fewer than 30 messages |
| Context per member per turn | 40 messages |
| History page | 50 default, 100 maximum |
| Attachments per message | 10 |
| Attachment size | 25 MB |
| Thumbnail size | 256 KB |
| Tool calls kept per message | 8 |
| Council | 3 rounds, 10 turns |
| Member response timeout | 10 minutes |
| Per-member usage rows | 24 |
| Group media notice | 500 MB |

## Sessions

Each member keeps a private session for each group, keyed
`desktop:profile-room:<groupId>` inside that bot's own directory. Group
attachments are stored with a `group-` prefix, which is what keeps the ordinary
media cleanup — which ages out generated pictures — from reaching a file a
message still points at.
