---
title: Telegram
eyebrow: Channels
description: Connect your agent to a Telegram bot over long polling — no webhook or public IP required. Telegram is the only channel that supports the pairing store for per-user access control.
---

## Requirements

- A bot token from **@BotFather** (create a bot with `/newbot`, copy the token).
- The `python-telegram-bot` library (installed with Flowly's Telegram extra).

## Configuration

Set under `channels.telegram` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:ABC-...",
      "allowFrom": [],
      "dmPolicy": "pairing"
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the Telegram adapter at gateway boot. |
| `token` | string | `""` | Bot token from @BotFather. |
| `allowFrom` | string[] | `[]` | Allowed user IDs or usernames. Empty = allow all (subject to `dmPolicy`). |
| `dmPolicy` | `"open"` \| `"pairing"` \| `"allowlist"` | `"pairing"` | DM access policy (see below). |

## DM access policy

- `open` — anyone who messages the bot is allowed.
- `pairing` — unknown senders receive an 8-character pairing code and instructions; you approve it from the CLI.
- `allowlist` — unknown senders are silently blocked; only `allowFrom` + approved pairing entries are allowed.

The Telegram adapter unions `allowFrom` with the approved pairing allow-store and matches a sender by ID, username, `@username`, or `telegram:{id}`.

## Connect method

Long polling via `python-telegram-bot`. The adapter registers native bot commands and handlers for text, photo, voice, audio, and documents, plus inline-button exec-approval callbacks. On startup it calls Telegram with `drop_pending_updates=true`, so messages received while the bot was offline are discarded.

## Pairing / allowlist

When a sender hits the bot in `pairing` mode, they get a code. Approve it:

```bash
flowly pairing list telegram
flowly pairing approve telegram <CODE> --notify
flowly pairing allowed telegram
flowly pairing revoke telegram <user_id>
```

> [!TIP]
> `--notify` sends an "approved" DM to the user (Telegram only). Codes expire after 1 hour; up to 3 pending requests are kept per channel.

## Setup steps

1. Create a bot in @BotFather and copy the token.
2. Set `channels.telegram.enabled` to `true` and paste the `token`.
3. Choose a `dmPolicy` (`pairing` is the default).
4. Start the gateway: `flowly gateway`.
5. Message your bot. In `pairing` mode, run `flowly pairing approve telegram <code>` with the code it gives you.

Exec-approval prompts are delivered as inline buttons in your Telegram chat — see [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md).

## Related

- [Channels overview](./overview.md)
- [Discord](./discord.md)
- [Slack](./slack.md)
- [WhatsApp](./whatsapp.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Cron](../features/cron.md)
- [Slash commands](../reference/slash-commands.md)
