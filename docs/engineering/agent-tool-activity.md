# Agent tool activity on the roster

Every app draws a named agent as a character. While a command runs it looks
at a small terminal; while it searches, fetches or browses it scans. The
character in an open conversation can read that from the conversation's own
tool events. The roster (the strip, the agents list, the groups' rings)
cannot: a directory socket never receives a conversation's events, and
should not, since they carry the call's arguments and results.

## Event

`profile_host` publishes a `profile.event` of type **`tool.activity`** for
every tool start and end, and for a turn's end:

| phase   | fields                           | when                                     |
|---------|----------------------------------|------------------------------------------|
| `start` | `scope`, `callId`, `name`        | a tool call starts (`tool.start`)        |
| `end`   | `scope`, `callId`, `name`        | it returns (`tool.complete`)             |
| `idle`  | `scope`                          | the conversation's turn ends (`chat` final, aborted or error) |

`scope` is the conversation's session key, so an agent working in several
conversations at once ends only that conversation's calls. `name` is the
tool's name; clients map it to a cue (command → terminal; search, fetch,
browser → search) and ignore the rest. Arguments, previews and results never
appear. Malformed frames (empty or oversized ids and names) publish nothing.

Internal turns (an agent briefing another one, a headless task) publish
nothing; the owner did not start them and has no conversation to open.
Group-room turns do publish: the agent is working.

## Routing

`tool.activity` goes to directory subscribers only: on a direct gateway, a
client that called `profiles.list`, `profiles.statuses` or
`profiles.capabilities`; through the relay, any session that made a
`profiles.*` call. A conversation's readers already receive the full tool
events and get nothing extra. The relay forwards it unchanged.

The primary (default) agent is not a hosted runtime: its frames reach the
host only while a client holds a default-events lease, so the roster sees its
activity only then. The apps' strips show named agents.

## Clients

Clients smooth the signal exactly as Desktop's `BotToolActivity` does: a cue
stays at least 1.2 s after its start and 0.4 s after its end, a turn's `idle`
clears it at once, any connection change clears everything, and a call whose
end never arrives expires after 10 minutes. A start for a call already shown
(the same `callId` from the conversation's own events) is ignored.

Older hosts send nothing; their agents keep the plain working motion.
