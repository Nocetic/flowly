---
title: Discord
eyebrow: Channels
description: Connect your agent to a Discord bot. Inbound uses the raw Discord Gateway WebSocket; outbound uses the Discord REST API v10. Access control is enforced through `allowFrom` only.
---

## Requirements

- A bot token from the **Discord Developer Portal** (create an application, add a Bot, copy the token).
- The **Message Content** privileged intent enabled for your bot in the Developer Portal (it is part of the default `intents` value below).

## Configuration

Set under `channels.discord` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "...",
      "allowFrom": [],
      "gatewayUrl": "wss://gateway.discord.gg/?v=10&encoding=json",
      "intents": 37377
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the Discord adapter at gateway boot. |
| `token` | string | `""` | Bot token from the Discord Developer Portal. |
| `allowFrom` | string[] | `[]` | Allowed user IDs. Empty = allow all. |
| `gatewayUrl` | string | `wss://gateway.discord.gg/?v=10&encoding=json` | Discord Gateway WebSocket URL. |
| `intents` | int | `37377` | Gateway intents bitfield (GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT). |

## Connect method

The adapter opens the Discord Gateway WebSocket, sends IDENTIFY with your `token` and `intents`, and maintains a heartbeat. It handles HELLO, READY/MESSAGE_CREATE, RECONNECT, and INVALID_SESSION, and auto-reconnects with a 5-second backoff. Messages from bot authors are skipped; attachments up to 20 MB are downloaded to `~/.flowly/media`. Outbound replies are sent via `POST {API}/channels/{chat_id}/messages` with `Authorization: Bot <token>`, with retry handling for rate limits.

## Access control

> [!WARNING]
> Discord enforces only `allowFrom` (allowed user IDs); an empty list allows everyone. It does **not** use the pairing store, so `flowly pairing approve discord ...` has no runtime effect on this adapter — manage access via the `allowFrom` list.

## Setup steps

1. In the Discord Developer Portal, create an application and add a Bot. Copy the token.
2. Under the Bot settings, enable the **Message Content** privileged intent.
3. Invite the bot to your server with the appropriate permissions.
4. Set `channels.discord.enabled` to `true` and paste the `token`.
5. Optionally restrict access by adding user IDs to `allowFrom`.
6. Start the gateway: `flowly gateway`.

## Related

- [Channels overview](./overview.md)
- [Telegram](./telegram.md)
- [Slack](./slack.md)
- [WhatsApp](./whatsapp.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Cron](../features/cron.md)
- [Slash commands](../reference/slash-commands.md)
