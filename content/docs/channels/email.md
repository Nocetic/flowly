---
title: Email
eyebrow: Channels
description: Give the agent Gmail — read, send, reply, and search — through the Gmail API, authenticated with OAuth 2.0 so the bot never sees your password. Gmail is wired up via the Flowly Desktop or web app, not the CLI wizard.
---

> [!IMPORTANT]
> What ships in the open-source gateway today is **Gmail access as a tool set** —
> the agent can read, send, reply to, and search your mail when asked. The
> inbound *email channel* (the gateway polling an inbox and auto-replying to
> every incoming message) exists in the codebase but is **not started by the
> open-source gateway** — `ChannelManager` doesn't attach it. So Flowly answers
> email when *you* drive a conversation through it, not unattended.

## Requirements

- A **Gmail / Google Workspace** account.
- A one-time **OAuth sign-in** that writes a token to
  `~/.flowly/credentials/gmail.json` (mode `0600`); your password is never
  handled by the bot.

> [!NOTE]
> Email uses the **Gmail API**, not IMAP/SMTP. Other providers (Outlook,
> Fastmail) aren't supported — use a messaging channel instead.

## How Gmail is connected

The OAuth flow is run by **Flowly Desktop or the web app**, which writes
`~/.flowly/credentials/gmail.json` for the CLI to use — there is **no
`flowly setup → Email`** path in the wizard (the gateway logs *"Connect Gmail via
web app or desktop app"* on boot). Once `gmail.json` is present, the Gmail and
Google Workspace tools register at the next gateway boot.

On a headless/VPS host, complete the sign-in once on a machine with a browser
(via Desktop/web), then copy `~/.flowly/credentials/gmail.json` to the server.

## Configuration

Set under `channels.email` in `~/.flowly/config.json`. This flag is what gates
the Gmail + Google Workspace tools (`email`, `google_calendar`, `google_contacts`,
`google_drive`, `google_tasks`):

```json
{
  "channels": {
    "email": {
      "enabled": true,
      "pollIntervalSeconds": 30,
      "allowFrom": ["you@example.com"]
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Register the Gmail / Google Workspace tools. (Also the flag the inbound channel would read, when it runs.) |
| `pollIntervalSeconds` | int | `30` | Inbox poll interval for the inbound channel (only relevant when that channel runs). |
| `allowFrom` | string[] | `[]` | Sender addresses allowed to drive the agent by mail (used by the inbound channel). |

The OAuth token is **not** in `config.json` — it lives in
`~/.flowly/credentials/gmail.json` and is refreshed automatically.

## What the agent can do with Gmail

With `channels.email.enabled: true` and `gmail.json` present, the agent gets the
Gmail and Google Workspace tools:

- **`email`** — read, send, reply (in-thread), and search Gmail.
- **`google_calendar` / `google_contacts` / `google_drive` / `google_tasks`** — the
  rest of Google Workspace. See [Google Workspace](../integrations/google-workspace.md).

So you can ask, in any channel or the TUI, *"check my inbox for anything from
Finance and draft a reply"* and the agent uses these tools to do it.

## Access control

`allowFrom` restricts which senders may drive the agent **when the inbound email
channel is running**. With an empty list any sender in the inbox could issue
commands — only ever do that on a dedicated, private address.

## Pitfalls

- **No CLI setup.** `flowly setup` has no Email entry — connect Gmail from
  Flowly Desktop or the web app, which writes `gmail.json`.
- **Token expiry.** Tokens refresh automatically; if Google revokes access
  (password change, security review), re-connect from the Desktop/web app.
- **Inbound is not unattended yet.** The OSS gateway won't watch an inbox and
  reply on its own — use the Gmail tools through a conversation instead.

## Related

- [Google Workspace](../integrations/google-workspace.md)
- [Channels overview](./overview.md)
- [Tools](../reference/tools.md)
