---
title: Slack
eyebrow: Channels
description: Connect your agent to a Slack app over Socket Mode — no public HTTP endpoint required. Slack has separate access policies for DMs and channel/group messages, enforced through Slack's own policy keys rather than the pairing store.
---

## Requirements

- A Slack app with **Socket Mode** enabled.
- A bot token (`xoxb-…`) and an app-level token (`xapp-…`).
- The `slack_sdk` library (installed with Flowly's Slack extra).

## Configuration

Set under `channels.slack` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "mode": "socket",
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "groupPolicy": "mention",
      "groupAllowFrom": [],
      "dm": {
        "enabled": true,
        "policy": "open",
        "allowFrom": []
      }
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the Slack adapter at gateway boot. |
| `mode` | string | `"socket"` | Connection mode; `socket` (Socket Mode) is supported. |
| `botToken` | string | `""` | Bot token (`xoxb-…`). |
| `appToken` | string | `""` | App-level token (`xapp-…`) for Socket Mode. |
| `groupPolicy` | `"mention"` \| `"open"` \| `"allowlist"` | `"mention"` | Access policy for channel/group messages. |
| `groupAllowFrom` | string[] | `[]` | Allowed channel IDs (used when `groupPolicy` is `allowlist`). |
| `dm.enabled` | bool | `true` | Allow direct messages to the bot. |
| `dm.policy` | `"open"` \| `"allowlist"` | `"open"` | DM access policy. |
| `dm.allowFrom` | string[] | `[]` | Allowed Slack user IDs (used when `dm.policy` is `allowlist`). |

## Access policies

- **DMs** are gated by `dm.enabled`, then `dm.policy` (`open` or `allowlist` against `dm.allowFrom`).
- **Channel/group messages** are gated by `groupPolicy`:
  - `mention` — only messages that `@mention` the bot (or `app_mention` events) trigger it.
  - `open` — any message in the channel triggers it.
  - `allowlist` — only channels listed in `groupAllowFrom`.

> [!NOTE]
> Slack does **not** use the pairing store; manage access via the keys above.

## Connect method

The adapter connects over Socket Mode, acks incoming Events API requests immediately, then processes `message` and `app_mention` events (ignoring subtypes and the bot's own messages, and de-duplicating channel mentions). It adds an `:eyes:` reaction to the triggering message. Outbound replies use `chat_postMessage`; non-DM replies are posted in a thread.

## Setup steps

1. Create a Slack app and enable **Socket Mode**.
2. Add a bot user and the required event subscriptions (`message`, `app_mention`); install the app to your workspace.
3. Copy the bot token (`xoxb-…`) and generate an app-level token (`xapp-…`).
4. Set `channels.slack.enabled` to `true`, paste `botToken` and `appToken`.
5. Choose `groupPolicy` and `dm` settings.
6. Start the gateway: `flowly gateway`.

## Related

- [Channels overview](./overview.md)
- [Telegram](./telegram.md)
- [Discord](./discord.md)
- [Teams](./teams.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Cron](../features/cron.md)
- [Slash commands](../reference/slash-commands.md)
