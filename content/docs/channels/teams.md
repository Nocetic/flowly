---
title: Microsoft Teams
eyebrow: Channels
description: Send agent output to a Microsoft Teams channel via an Incoming Webhook connector. This channel is outbound only — the agent can post to Teams, but Teams messages do not flow back into the agent.
---

## Outbound-only

> [!WARNING]
> The current Teams integration (Faz 1) is one-way: the bot posts to a Teams channel through an Incoming Webhook URL. There is no inbound path — you cannot message the agent from Teams. It is suited to notifications, cron output, alerts, and daily summaries. Bidirectional support (Bot Framework + Graph API) is deferred to a future phase.

## Requirements

- An **Incoming Webhook** connector created in the target Teams channel; the resulting HTTPS URL is the credential.

## Configuration

Set under `channels.teams` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "teams": {
      "enabled": true,
      "webhookUrl": "https://outlook.office.com/webhook/...",
      "defaultChatLabel": "",
      "allowFrom": []
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the Teams adapter at gateway boot. |
| `webhookUrl` | string | `""` | Teams Incoming Webhook URL. Must start with `https://`. The URL itself is the secret. |
| `defaultChatLabel` | string | `""` | Human-friendly label for the target channel. |
| `allowFrom` | string[] | `[]` | Reserved for future inbound support; unused now. |

## Connect method

The adapter posts `{"text": "..."}` to `webhookUrl`. Plain text / Teams-rendered markdown is supported; HTTP(S) media URLs are appended under an **Attachments** list. It retries once on 5xx/network errors and skips empty bodies (a Teams webhook rejects empty payloads). `webhookUrl` **must** start with `https://`, or the channel disables itself with a warning. One webhook targets exactly one channel.

## Setup steps

1. In the target Teams channel, add an **Incoming Webhook** connector and copy the generated HTTPS URL.
2. Set `channels.teams.enabled` to `true` and paste the URL into `webhookUrl`.
3. Optionally set `defaultChatLabel`.
4. Start the gateway: `flowly gateway`.

Pair this with [Cron](../features/cron.md) to push scheduled summaries to Teams.

## Related

- [Channels overview](./overview.md)
- [Slack](./slack.md)
- [Web](./web.md)
- [Cron](../features/cron.md)
- [Service](../using-flowly/service.md)
- [Slash commands](../reference/slash-commands.md)
