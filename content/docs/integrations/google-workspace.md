---
title: Google Workspace
eyebrow: Integrations
description: Native tools for Google Calendar, Contacts, Drive, and Tasks (plus Gmail via the email tool) that call the Google REST APIs directly using an OAuth 2.0 access token.
---

This page covers the four Workspace data tools, how they are enabled, where their credentials live, and the honest state of the setup flow.

## Tools

| Tool | Purpose | Actions | Key params |
|---|---|---|---|
| `google_calendar` | List/manage calendar events | `list`, `get`, `create`, `update`, `delete`, `search` | `event_id`, `summary`, `description`, `start`, `end`, `location`, `attendees`, `query`, `max_results`, `calendar_id` |
| `google_contacts` | Read-only contact lookup | `search`, `list` | `query`, `max_results` |
| `google_drive` | Browse/read/create Drive files | `list`, `search`, `read`, `info`, `create` | `file_id`, `query`, `name`, `content`, `mime_type`, `max_results` |
| `google_tasks` | Manage task lists and tasks | `lists`, `tasks`, `create`, `complete`, `delete` | `tasklist_id`, `task_id`, `title`, `notes`, `due`, `max_results` |

Notes:

- `google_calendar` `start`/`end` are ISO 8601 datetimes (e.g.
  `2026-04-10T14:00:00+03:00`). `attendees` is a comma-separated list of email
  addresses. `calendar_id` defaults to `primary`.
- `google_drive` `query` uses Drive search syntax. For `create`, `mime_type`
  defaults to `text/plain`; use `application/vnd.google-apps.document` to create
  a Google Doc.
- `google_tasks` `tasklist_id` defaults to `@default`. `due` is an ISO 8601
  date.
- `google_contacts` is read-only (search/list only).

API base URLs used: Calendar API v3, People API (contacts), Drive API v3, Tasks
API v1.

## Enabling the tools

> [!IMPORTANT]
> The four Workspace tools (and the Gmail `email` tool) register together, gated on the **email channel** being enabled — not on the `integrations.googleWorkspace` card.

```json
{
  "channels": {
    "email": { "enabled": true }
  }
}
```

When `channels.email.enabled` is `true`, Flowly registers `email`,
`google_calendar`, `google_contacts`, `google_drive`, and `google_tasks` at agent
boot. Enabling the `integrations.googleWorkspace` card alone does **not** register
these native tools.

The `integrations.googleWorkspace` block is a separate, display/opt-in toggle:

```json
{
  "integrations": {
    "googleWorkspace": {
      "enabled": false,
      "email": ""
    }
  }
}
```

`email` here is display-only (the connected Google account address); `enabled`
flags the integration card. Neither field controls native-tool registration.

## Credentials

The native tools authenticate with an OAuth 2.0 access token obtained via a
refresh-token grant. The credentials file is read from:

```
~/.flowly/credentials/gmail.json
```

It contains `refresh_token`, `client_id`, and `client_secret`. Flowly exchanges
the refresh token at `https://oauth2.googleapis.com/token` for a short-lived
access token and sends `Authorization: Bearer <token>` on every API call. The
same token is shared across Gmail, Calendar, Contacts, Drive, and Tasks.

> [!WARNING]
> **Unverified:** The exact command/flow that *writes* `gmail.json` (including the requested OAuth scopes and the OAuth client configuration) is not present in this repository. The file appears to be produced by an external web/gateway OAuth flow (e.g. Flowly Cloud). If you do not have a `gmail.json`, obtain one through your Flowly Cloud account or the gateway's Google connect flow. The precise scope strings are not documented here because they are not defined in code we can cite — do not assume a specific scope set.

## `flowly setup google-workspace`

A separate setup wizard exists:

```bash
flowly setup google-workspace
```

This wizard installs and authenticates the Google Workspace CLI (`gws`):

1. Installs `gws` via `npm install -g @googleworkspace/cli` (Node.js required).
2. Installs the `gcloud` CLI (Homebrew on macOS, apt on Linux) if missing.
3. Runs `gws auth setup` then `gws auth login`.
4. On success, sets `integrations.googleWorkspace.enabled = true`, records the
   detected account email, enables the `exec` tool, and allowlists the `gws`
   binary so the agent can run `gws *` commands without per-command approval.

> [!IMPORTANT]
> This wizard wires up the `gws` *command-line* path (driven through the `exec` tool), which is distinct from the native `google_calendar`/`google_drive`/`google_contacts`/`google_tasks` tools. The native tools still require `channels.email.enabled` and a valid `~/.flowly/credentials/gmail.json`. Both paths can coexist.

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Channels overview](../channels/overview.md)
- [Linear](./linear.md), [Trello](./trello.md), [X](./x.md), [Home Assistant](./home-assistant.md)
