---
title: Linear
eyebrow: Integrations
description: The linear tool lets the agent read and manage Linear issues, projects, teams, and comments through Linear's GraphQL API, authenticated with a personal API key.
---

## Tool

| Tool | Actions |
|---|---|
| `linear` | `list_issues`, `get_issue`, `create_issue`, `update_issue`, `add_comment`, `search`, `list_projects`, `list_teams` |

Key params:

| Param | Used by | Notes |
|---|---|---|
| `issue_id` | `get_issue`, `update_issue`, `add_comment` | UUID or key like `ENG-123` |
| `title` | `create_issue` | Issue title |
| `description` | `create_issue`, `update_issue` | Markdown |
| `team_id` | `create_issue`, `list_issues` | Filter / target team |
| `project_id` | `create_issue`, `list_issues` | Filter / target project |
| `assignee_id` | `create_issue`, `update_issue` | User ID to assign |
| `state_name` | `update_issue` | Workflow state name, e.g. `In Progress`, `Done`, `Todo` |
| `priority` | `create_issue`, `update_issue` | `0`=none, `1`=urgent, `2`=high, `3`=medium, `4`=low |
| `label_names` | `create_issue`, `update_issue` | Comma-separated label names |
| `comment_body` | `add_comment` | Comment text (markdown) |
| `status_filter` | `list_issues` | Filter by state name, e.g. `Todo`, `In Progress` |
| `query` | `search` | Search query |
| `max_results` | list/search | Default 10 |

## Configuration

The tool registers only when `integrations.linear.apiKey` is set:

```json
{
  "integrations": {
    "linear": {
      "apiKey": "lin_api_..."
    }
  }
}
```

The key is sent as the `Authorization` header (raw token, no `Bearer` prefix) to
Linear's GraphQL API.

## Getting a Linear personal API key

In Linear, go to **Settings → API** and create a personal API key. It is
prefixed `lin_api_`. Paste it into `integrations.linear.apiKey` (or run setup
below).

## Setup

Run the interactive integrations setup and pick Linear:

```bash
flowly setup tools
```

This presents the Linear card, prompts for the API key, validates it with a
probe, and writes `integrations.linear.apiKey`.

> [!NOTE]
> The integration requires a gateway restart to take effect (handled by setup, or run `flowly service restart`).

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Google Workspace](./google-workspace.md), [Trello](./trello.md), [X](./x.md), [Home Assistant](./home-assistant.md)
- [Channels overview](../channels/overview.md)
