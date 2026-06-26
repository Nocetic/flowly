---
title: Channels Overview
eyebrow: Channels
description: Flowly's gateway is a local daemon that runs the agent loop and attaches chat-platform adapters ("channels") so you can talk to your agent from Telegram, Discord, Slack, Teams, WhatsApp, or the web.
---

## The gateway

`flowly gateway` starts a single local daemon built on three pieces:

- **Gateway server** — an `aiohttp` HTTP + WebSocket app bound to `127.0.0.1:18790`. It serves `/health`, cron/provider HTTP routes, artifact routes, and a `/ws` WebSocket for the desktop/TUI clients. This is the direct-client surface; it is **not** the channel transport. Channels run independently.
- **Message bus** — two `asyncio` queues (`inbound`, `outbound`) that decouple channels from the agent. Channels publish inbound messages; the agent consumes them and publishes outbound responses; a dispatcher routes outbound replies back to the originating channel.
- **ChannelManager** — instantiates the enabled channels from config, starts them all (plus the outbound dispatcher), and routes each outbound message to the matching adapter by channel name.

Start it:

```bash
flowly gateway
flowly gateway --port 18790 --verbose
```

The bind host comes from `config.gateway.host`; the listen port comes from the `--port` flag (default `18790`). Setting `gateway.port` in config alone does **not** change the listening port via this command — pass `--port`.

> [!NOTE]
> The `/ws` endpoint is not token-gated; security relies on the localhost-only bind. To run the gateway as a background service, see [Service](../using-flowly/service.md).

## How channels attach at boot

At startup the ChannelManager reads `channels.<name>.enabled` and instantiates only the channels that are turned on. Each adapter import is guarded, so a missing optional dependency logs a warning instead of crashing the gateway. Enabled adapters and the outbound dispatcher are then started concurrently with the agent loop.

A channel is configured under `channels` in `~/.flowly/config.json` (camelCase keys on disk):

```json
{
  "channels": {
    "telegram": { "enabled": true, "token": "...", "allowFrom": [], "dmPolicy": "pairing" }
  }
}
```

Channels `cli`, `tui`, and `desktop` are intentionally adapter-less — their replies go out over the gateway WebSocket, not through a channel adapter.

## Active channels

| Channel | Connect method | Required credential |
| --- | --- | --- |
| [Telegram](./telegram.md) | Bot long-polling (`python-telegram-bot`) | Bot `token` from @BotFather |
| [Discord](./discord.md) | Discord Gateway WebSocket + REST v10 | Bot `token` from Discord Developer Portal |
| [Slack](./slack.md) | Socket Mode WebSocket (`slack_sdk`) | `botToken` (`xoxb-`) + `appToken` (`xapp-`) |
| [Teams](./teams.md) | Incoming Webhook (outbound only) | `webhookUrl` (HTTPS connector URL) |
| [WhatsApp](./whatsapp.md) | Node baileys bridge over WebSocket | QR-code scan (no token in config) |
| [iMessage](./imessage.md) | BlueBubbles Server (webhook in + REST out) | BlueBubbles `serverUrl` + `password` |
| [Web](./web.md) | Outbound WebSocket to Flowly Cloud relay | `relayUrl` / `serverId` / `authToken` / `jwtSecret` |

> [!NOTE]
> **Email note.** An Email/Gmail channel exists in the codebase (`channels.email` config is accepted), but it is **not wired into the ChannelManager** — the standard `flowly gateway` boot does not start it. Enabling `channels.email` and running the gateway produces no email polling from the manager path. It is documented here only for transparency; treat email as not currently available via the standard gateway.

## Pairing & allowlist security model

Each adapter checks whether a sender is allowed before publishing their message to the bus. The simplest control is the per-channel `allowFrom` list (an empty list means allow everyone). On top of that, Flowly has a **pairing store** for granting access without editing config by hand.

### How pairing works

- An unauthorized sender (in `pairing` mode) is issued an **8-character code** from an unambiguous alphabet (no `0/O/1/I`).
- Codes are stored as pending requests that **expire after 1 hour**, with a maximum of 3 pending requests per channel.
- You approve a code from the CLI, which moves the sender into the channel's approved allow-store.

Pairing/allow-store files live under `~/.flowly/credentials/`:

- `{channel}-pairing.json` — pending requests
- `{channel}-allowFrom.json` — approved senders

### Pairing commands

```bash
flowly pairing list <channel> [--json]
flowly pairing approve <channel> <code> [--notify]
flowly pairing revoke <channel> <user_id>
flowly pairing allowed <channel>
```

`<channel>` accepts `telegram`, `whatsapp`, `discord`, `slack`, or `imessage`. `--notify` (on `approve`) only sends a confirmation DM for Telegram.

> [!WARNING]
> **Pairing is enforced by Telegram and iMessage.** Those two adapters read the pairing/allow store. Discord, Slack, and WhatsApp enforce access through their own `allowFrom` / policy config keys instead, not the pairing store — so approving a Discord or Slack code writes a file those adapters never read. Use the channel's config `allowFrom`/policy keys for Discord/Slack/WhatsApp access control.

## Channel status & login (WhatsApp)

```bash
flowly channels status
flowly channels login
```

> [!NOTE]
> `flowly channels status` currently reports **only WhatsApp** (enabled + bridge URL). It does not enumerate Telegram, Discord, Slack, Teams, or Web. `flowly channels login` builds and runs the WhatsApp Node bridge and prints a QR code.

## Related

- [Telegram](./telegram.md)
- [Discord](./discord.md)
- [Slack](./slack.md)
- [Teams](./teams.md)
- [WhatsApp](./whatsapp.md)
- [Web](./web.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Service](../using-flowly/service.md)
- [Cron](../features/cron.md)
- [Slash commands](../reference/slash-commands.md)
