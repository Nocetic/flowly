---
name: teams
description: "Send messages to a Microsoft Teams channel via an Incoming Webhook. Outbound-only (Faz 1): bot can post into Teams, users can't reply through the same channel yet. Use for notifications, cron output, daily summaries, alerts."
metadata: {"flowly":{"emoji":"💬","tags":["teams","microsoft","webhook","notifications","outbound"]}}
---

# Microsoft Teams

Flowly can post messages into any Microsoft Teams channel through an
**Incoming Webhook** — a per-channel HTTPS URL generated inside Teams.
No Azure AD app, no Bot Framework registration, no tenant admin
approval required for Faz 1. Best for **bot → channel** flows:
cron output, alerts, daily summaries, reports.

> **Faz 1 limitation:** outbound only. Users in Teams cannot reply to
> the bot through the webhook. Bidirectional chat (Bot Framework +
> Graph API) lands in Faz 2.

## When to Use This Skill

The user is asking for any of:

- Send a notification / alert to a Teams channel
- Wire a cron job's output into Teams
- Post a daily summary into a project channel
- Mirror an event from another platform (Slack, Telegram) into Teams

## Quick Setup (~60 seconds)

1. Open the target channel in Microsoft Teams.
2. Click **⋯ (More options)** next to the channel name → **Connectors**.
   *(If Connectors is missing, your tenant admin disabled it — the
   user must enable it for the channel before continuing.)*
3. Find **Incoming Webhook** → **Configure**.
4. Name the webhook (e.g. *Flowly*), optionally upload an icon, then
   **Create**.
5. **Copy the generated URL** — it looks like
   `https://outlook.office.com/webhook/<long-token>` or
   `https://<tenant>.webhook.office.com/webhookb2/<token>`.
6. Save it once shown — Teams won't reveal it again.

## Configure Flowly

Add the URL to `~/.flowly/config.yaml`:

```yaml
channels:
  teams:
    enabled: true
    webhook_url: "https://outlook.office.com/webhook/..."
    default_chat_label: "engineering"   # optional, human-friendly tag
```

Restart the bot. On startup it logs:

```
[Teams] Channel ready (outbound webhook → engineering)
```

## What Gets Sent

The bot posts whatever it would otherwise reply with — plain
Markdown-rendered text. Teams renders **bold**, *italic*, lists,
and inline code natively.

If the message has media attachments (image / video / pdf cdnUrl),
they appear under an **Attachments** section as clickable links:

```
Daily report ready.

**Attachments**
- https://cdn.example.com/users/u1/uploads/report.pdf
- https://cdn.example.com/users/u1/uploads/chart.png
```

## Cron Integration

Wire any cron job to Teams via the standard outbound dispatcher.
Example: post a memory-watchdog alert into Teams every 15 minutes
(only when there's something to say):

```bash
flowly cron create "*/15 * * * *" \
  --no-agent \
  --script ~/.flowly/scripts/disk-alert.sh \
  --deliver teams \
  --name "disk-watchdog-teams"
```

The `no_agent` mode emits the script's stdout into the Teams channel
verbatim. Empty stdout = silent (no alert, no Teams noise).

## Common Pitfalls

- **404 on POST:** the webhook URL was revoked. Generate a new one
  in the Teams channel's Connectors panel and update `config.yaml`.
- **400 "Bad payload":** Teams rejects unusually long messages
  (~28 KB Markdown limit). Trim the content or split into multiple
  posts before sending.
- **No notifications:** make sure the channel's notification rules
  in Teams include webhook posts; some users mute connector activity
  by default.
- **Tenant admin disabled Connectors:** ask the admin to re-enable
  the Incoming Webhook connector for the channel — there is no
  workaround on the bot side.

## Faz 2 (Future)

When bidirectional chat is enabled (Bot Framework + Azure AD app
registration + Microsoft Graph), the same `channels.teams` config
gains `tenant_id`, `client_id`, and `client_secret` fields and the
bot can receive messages from Teams users in addition to posting.
The webhook URL stays a valid fallback for outbound-only setups.
