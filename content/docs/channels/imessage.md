---
title: iMessage
eyebrow: Channels
description: Reach your agent from the blue bubbles. Flowly speaks iMessage in two ways — through a BlueBubbles server (recommended, zero macOS permission prompts) or directly against Messages.app on a Mac. macOS only, off by default.
---

## Requirements

- A **Mac** signed into iMessage (this channel is macOS-only).
- **Either** a running [BlueBubbles](https://bluebubbles.app) server **or** Full
  Disk Access granted to the process that runs the gateway (direct mode).

> [!WARNING]
> iMessage is power-user territory. Apple gives no official bot API, so both
> paths involve real setup. A **dedicated Apple ID** for the bot is strongly
> recommended so agent traffic never mixes with your personal messages.

## Two modes

| Mode | When | Inbound | Outbound | macOS permissions |
| --- | --- | --- | --- | --- |
| **BlueBubbles** *(recommended)* | `bluebubblesUrl` is set | New-message **webhooks** from the BlueBubbles server | BlueBubbles **REST** API | None on the gateway — BlueBubbles holds Automation + Full Disk Access |
| **Direct** *(default)* | no `bluebubblesUrl` | Tails `~/Library/Messages/chat.db` (SQLite WAL) | Drives Messages.app via a signed helper / `osascript` | **Full Disk Access** required on the gateway |

BlueBubbles is recommended because a separate, signed macOS app holds the
sensitive grants — the gateway needs no special permissions and isn't fighting
the TCC prompts that block a background process scripting Messages directly.

## Configuration

Set under `channels.imessage` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "imessage": {
      "enabled": true,
      "bluebubblesUrl": "http://localhost:1234",
      "bluebubblesPassword": "your-bluebubbles-password",
      "bluebubblesWebhookHost": "127.0.0.1",
      "bluebubblesWebhookPort": 8642,
      "dmPolicy": "pairing",
      "groupPolicy": "mention",
      "allowFrom": [],
      "groupAllowFrom": [],
      "mentionPatterns": ["flowly"]
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the iMessage adapter at gateway boot. |
| `pollIntervalSeconds` | float | `2.0` | Direct mode: how often to tail `chat.db`. |
| `dbPath` | string | `""` | Override the `chat.db` path (direct mode). Empty = default. |
| `bluebubblesUrl` | string | `""` | BlueBubbles server URL. **Set this to use BlueBubbles mode.** |
| `bluebubblesPassword` | string | `""` | BlueBubbles server password. |
| `bluebubblesWebhookHost` | string | `127.0.0.1` | Local host the webhook listener binds to. |
| `bluebubblesWebhookPort` | int | `8642` | Local port BlueBubbles posts inbound webhooks to. |
| `dmPolicy` | string | `pairing` | How DMs are admitted — `pairing` requires a one-time DM-pairing handshake. |
| `groupPolicy` | string | `mention` | Group chats respond only when the bot is mentioned. |
| `allowFrom` | string[] | `[]` | Allowed DM senders (phone/email handles). |
| `groupAllowFrom` | string[] | `[]` | Allowed group participants. |
| `mentionPatterns` | string[] | `[]` | Regex patterns that count as a mention in groups. When empty, the runtime falls back to `@?flowly\b` (matches "flowly" / "@flowly"). |

## Setup

**BlueBubbles (recommended):**

1. Install and run the [BlueBubbles Server](https://bluebubbles.app) on the Mac,
   grant it Automation + Full Disk Access, and set a server password.
2. Point Flowly at it: set `bluebubblesUrl` + `bluebubblesPassword` above.
3. **No manual webhook needed** — on start, Flowly auto-registers its listener
   (`http://<webhookHost>:<webhookPort>/bluebubbles-webhook`) with the
   BlueBubbles server. If auto-registration fails (older BlueBubbles, locked-down
   network), add that exact URL as a webhook in BlueBubbles by hand.
4. `flowly restart`.

**Direct:**

1. Grant **Full Disk Access** to the process that runs the gateway (Terminal, or
   the service binary) in System Settings → Privacy & Security.
2. Leave `bluebubblesUrl` empty; `flowly restart`.

## Access control

- **DMs** default to `pairing` — a sender must complete a one-time pairing
  handshake before the agent will talk to them. Combine with `allowFrom` to hard-
  restrict handles.
- **Groups** default to mention-only (`groupPolicy: mention`) so the bot stays
  quiet until addressed; tune what counts as a mention with `mentionPatterns`.

## Pitfalls

- **Direct mode and TCC.** A background/launchd gateway often can't get the
  Automation prompt it needs to script Messages.app — this is exactly why
  BlueBubbles exists. If outbound silently fails in direct mode, switch to
  BlueBubbles.
- **Outbound can be slow.** iMessage send latency varies; long sends are given a
  generous timeout.
- **Dedicated Apple ID.** Running the bot on your personal Apple ID mixes agent
  replies into your own threads — use a separate account.
