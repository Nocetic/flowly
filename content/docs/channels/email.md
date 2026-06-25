---
title: Email
eyebrow: Channels
description: Talk to your agent over email. Flowly polls a Gmail inbox through the Gmail API and replies in-thread, authenticated with OAuth 2.0 — the bot never sees your password.
---

## Requirements

- A **Gmail / Google Workspace** account.
- A one-time **OAuth sign-in** in the browser. Flowly stores the resulting token
  at `~/.flowly/credentials/gmail.json` (mode `0600`); your password is never
  handled by the bot.

> [!NOTE]
> Email uses the **Gmail API**, not IMAP/SMTP. Other providers (Outlook, Fastmail)
> aren't supported by this channel today — use a messaging channel instead.

## Configuration

Set under `channels.email` in `~/.flowly/config.json`:

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
| `enabled` | bool | `false` | Start the email adapter at gateway boot. |
| `pollIntervalSeconds` | int | `30` | How often to poll the inbox for new mail. |
| `allowFrom` | string[] | `[]` | Sender addresses allowed to reach the agent. Empty = allow all (not recommended for a public inbox). |

The OAuth token itself is **not** in `config.json` — it lives in
`~/.flowly/credentials/gmail.json` and is refreshed automatically.

## How it works

- **Inbound:** Flowly polls the Gmail API every `pollIntervalSeconds`, picks up
  unread messages from allowed senders, and feeds the body to the agent as a
  turn. The original `Subject` and thread are preserved.
- **Outbound:** the agent's reply is sent through the Gmail API **in the same
  thread**, so the conversation stays threaded in the client.
- **Auth:** OAuth 2.0. The token is obtained via the browser sign-in flow and
  stored locally; only the scopes needed to read and send mail are requested.

## Setup

Run the wizard and pick Email under channels:

```bash
flowly setup            # → Channels → Email → sign in with Google
```

The browser opens the Google consent screen once; after you approve, the token
is saved and the channel is ready. Restart the gateway (`flowly restart`) if it
was already running.

## Access control

Use `allowFrom` to restrict who can drive the agent by email. With an empty
list, **any** sender that lands in the inbox can issue commands — only do that on
a dedicated, private address.

## Pitfalls

- **First run needs a browser.** Headless/VPS setups should complete the OAuth
  sign-in once on a machine with a browser, then copy
  `~/.flowly/credentials/gmail.json` to the server.
- **Polling latency.** Replies arrive within one poll interval; lower
  `pollIntervalSeconds` for snappier turnaround at the cost of more API calls.
- **Token expiry.** Tokens refresh automatically; if Google revokes access
  (password change, security review), re-run `flowly setup` → Email to re-auth.
