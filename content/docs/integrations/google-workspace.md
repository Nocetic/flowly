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

## Connect and choose access

In Desktop, open Gmail for the selected agent, or review a Google connection
request in chat. Choose Gmail management and any optional Calendar, Drive,
Contacts or Tasks access before continuing to Google. Google presents its own
consent screen. The app shows which permissions were actually granted; declined
services remain unavailable. “Extend Google access” adds services later while
keeping the old connection until the new authorization succeeds for the same
Google account.

From a terminal:

```sh
flowly gmail connect --services gmail,gmail_manage,calendar,drive,contacts,tasks
flowly gmail connect --extend --services gmail,gmail_manage,tasks
```

The plain `flowly gmail connect` command keeps the original Gmail read/send
selection. Native tools become available without restarting the agent after
consent. The separate `integrations.googleWorkspace.enabled` card still refers
to the optional CLI setup below, not native tool permission grants.

## Credentials and permissions

Managed connections store a profile-local grant secret and short-lived access
token under `credentials/gmail.json`; the broker retains encrypted Google refresh
tokens. OAuth client secrets and refresh tokens are not sent to the agent or chat.
Historical local OAuth credential files remain supported.

| Access | OAuth scopes |
|---|---|
| Gmail read/send | `gmail.readonly`, `gmail.send` |
| Gmail management | `gmail.modify` (includes read/send; no immediate permanent deletion) |
| Calendar | `calendar.events` |
| Drive | `drive.readonly`, `drive.file` (browse/read existing files, create new files) |
| Contacts | `contacts.readonly` |
| Tasks | `tasks` |

Google API scopes above use the `https://www.googleapis.com/auth/` prefix.
Enabled tools are bounded by both requested services and actual granted scopes.
Sending, Gmail management and Workspace writes require approval. There are no
native full-document editing tools for Sheets or Docs in this integration.

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
> This wizard wires up the `gws` *command-line* path (driven through the `exec` tool), which is distinct from the native `google_calendar`/`google_drive`/`google_contacts`/`google_tasks` tools. Native tools use the profile-local Google connection and granted service permissions. Both paths can coexist.

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Channels overview](../channels/overview.md)
- [Linear](./linear.md), [Trello](./trello.md), [X](./x.md), [Home Assistant](./home-assistant.md)
