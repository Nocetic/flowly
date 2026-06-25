---
title: Trello
eyebrow: Integrations
description: The trello tool lets the agent work with Trello boards, lists, and cards, authenticated with a Trello API key + token pair supplied via config or environment variables.
---

## Tool

| Tool | Actions |
|---|---|
| `trello` | `list_boards`, `list_lists`, `list_cards`, `get_card`, `create_card`, `update_card`, `add_comment`, `archive_card`, `search` |

Key params:

| Param | Used by | Notes |
|---|---|---|
| `board_id` | `list_lists`, `list_cards` | Board identifier |
| `list_id` | `list_cards`, `create_card` | List identifier |
| `card_id` | `get_card`, `update_card`, `add_comment`, `archive_card` | Card identifier |
| `name` | `create_card`, `update_card` | Card name |
| `description` | `create_card`, `update_card` | Card description |
| `comment` | `add_comment` | Comment text |
| `query` | `search` | Search query |
| `due_date` | `create_card`, `update_card` | Due date (ISO format) |
| `labels` | `create_card`, `update_card` | Comma-separated label IDs |

The key and token are sent as `key` and `token` query parameters on each Trello
REST call.

## Configuration

The tool registers when **both** the API key and token are present. Config keys:

```json
{
  "integrations": {
    "trello": {
      "apiKey": "...",
      "token": "..."
    }
  }
}
```

### Environment fallback

If the config values are empty, the tool falls back to environment variables:

| Env var | Maps to |
|---|---|
| `TRELLO_API_KEY` | `integrations.trello.apiKey` |
| `TRELLO_TOKEN` | `integrations.trello.token` |

## Getting key + token

Generate both from the Trello developer page at
`https://trello.com/app-key` — the API key is shown there, and the manual token
is generated from the same page.

## Setup

Run the interactive integrations setup and pick Trello:

```bash
flowly setup tools
```

The Trello card prompts for the key and token, validates them with a probe, and
writes `integrations.trello.apiKey` / `integrations.trello.token`.

> [!NOTE]
> The integration needs a gateway restart to take effect (handled by setup, or run `flowly service restart`).

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Google Workspace](./google-workspace.md), [Linear](./linear.md), [X](./x.md), [Home Assistant](./home-assistant.md)
- [Channels overview](../channels/overview.md)
