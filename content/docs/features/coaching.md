---
title: Coaching — live meeting coach
eyebrow: Features
description: A real-time meeting assistant that watches a rolling transcript and surfaces short, timely tips to a notch UI. Active whenever the gateway runs.
---

> [!NOTE]
> Coaching is surfaced through the **desktop / client app**, not a CLI command. It is driven over the gateway's WebSocket RPC interface, not through `flowly` subcommands.

## What it does

1. The client sends **already-transcribed text** segments (`coaching.segment`). Transcription happens **client-side** — the gateway receives text, not audio, and has no STT dependency in this path.
2. Each segment is appended to a rolling buffer (up to 50 segments).
3. Every K new segments (or every K seconds), if not rate-limited, the buffer is passed through the gate pipeline.
4. Tips that pass the gate are dispatched to per-session callbacks and rendered in the notch UI.
5. On stop, the coach best-effort summarizes the session → extracts knowledge-graph entities → appends to `MEMORY.md` → saves a transcript artifact.

An STT-noise filter discards tags like `[music]` and `[silence]`. Silence resets the buffer after 120s. Hard caps protect the session: 40 tips per session, 5 concurrent sessions, 4 hours per session.

## The gate pipeline

Each candidate moment runs through a 2-stage pipeline before a tip is shown:

1. **Relevance** — is this worth interrupting for?
2. **Generate** — produce a candidate tip.

An optional third stage, **Critic** — does the tip survive a quality check? — is **off by default** and can be enabled per session. Only Relevance and Generate are mandatory; tips that clear them reach the UI unless the Critic stage is turned on.

## Frequency profiles and gate modes

A frequency profile tunes how often the gate runs (segments, seconds, rate limit) and the score threshold tips must clear:

| Profile | Score threshold |
| --- | --- |
| `selective` | 0.80 |
| `moderate` | 0.60 |
| `proactive` | 0.40 |

A higher threshold (`selective`) means fewer, higher-confidence tips; `proactive` surfaces more, lower-bar tips.

The gate also has two modes: `assistant` (default) and `guardian`. Profile and mode are passed in over the WebSocket session.

## Driving it (WebSocket RPC)

The client drives a session with these RPC methods:

```text
coaching.start
coaching.segment
coaching.askNow
coaching.stop
coaching.state
coaching.snapshot
coaching.update
```

`start` opens a session, `segment` feeds transcribed text, `askNow` requests an immediate tip, `update` adjusts the profile/mode, `state`/`snapshot` inspect the session, and `stop` triggers finalization (summary, KG extraction, MEMORY.md append, transcript artifact).

## Related

- [Voice](voice.md)
- [Cron](cron.md)
- [Channels overview](../channels/overview.md)
- [Feature overview](overview.md)
- [Slash commands reference](../reference/slash-commands.md)
