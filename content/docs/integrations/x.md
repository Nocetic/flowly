---
title: X (Twitter)
eyebrow: Integrations
description: Two X-related tools — x for posting and reading via the X API, and x_search for Grok-backed X research via xAI. The x tool uses dual authentication.
---

The `x` tool uses **dual authentication** — an App Bearer Token for reads and OAuth 1.0a for writes.

## Tools

| Tool | Purpose | Actions |
|---|---|---|
| `x` | Post/read X content via the X API | `post_tweet`, `delete_tweet`, `search_tweets`, `get_timeline`, `get_user` |
| `x_search` | Grok-backed X research (xAI hosted tool) | single query tool (see below) |

### `x` params

| Param | Used by | Notes |
|---|---|---|
| `text` | `post_tweet` | Tweet text, max 280 chars |
| `tweet_id` | `delete_tweet` | Target tweet ID |
| `query` | `search_tweets` | Search query |
| `username` | `get_timeline`, `get_user` | Handle without `@` |
| `max_results` | reads | 5–100, default 10 |

### `x_search` params

`query`, `allowed_x_handles`, `excluded_x_handles`, `from_date`, `to_date`,
`enable_image_understanding`, `enable_video_understanding`.

## Authentication (`x` tool)

The `x` tool uses two credential paths depending on the operation:

- **Read** (`search_tweets`, `get_timeline`, `get_user`): App-only **Bearer
  Token**.
- **Write** (`post_tweet`, `delete_tweet`): **OAuth 1.0a** — consumer key/secret
  plus access token/secret, with an HMAC-SHA1 signed `Authorization` header.

## Configuration (`x` tool)

The tool registers when `bearerToken` **or** `apiKey` is present. Full key set:

```json
{
  "integrations": {
    "x": {
      "bearerToken": "",
      "apiKey": "",
      "apiSecret": "",
      "accessToken": "",
      "accessTokenSecret": ""
    }
  }
}
```

| Key | Role |
|---|---|
| `bearerToken` | App-only Bearer Token (read operations) |
| `apiKey` | OAuth 1.0a consumer key (write operations) |
| `apiSecret` | OAuth 1.0a consumer secret |
| `accessToken` | OAuth 1.0a access token |
| `accessTokenSecret` | OAuth 1.0a access token secret |

Provide only `bearerToken` for read-only use; supply the four OAuth 1.0a values
to enable posting/deleting.

> [!NOTE]
> There is no environment-variable fallback for the `x` tool — config only. Missing-credential errors name the exact key required.

## `x_search` (xAI)

`x_search` is a separate tool backed by xAI's hosted `x_search` capability. Its
credentials are resolved in priority order:

1. xAI OAuth subscription credentials (signed in via `flowly xai login`).
2. Fallback to `providers.xai.apiKey`, or the `XAI_API_KEY` environment
   variable.

## Setup

Run the interactive integrations setup and pick X:

```bash
flowly setup tools
```

The X card prompts for the relevant credentials, validates with a probe, and
writes the `integrations.x.*` keys.

> [!NOTE]
> The integration needs a gateway restart to take effect (handled by setup, or run `flowly service restart`).

## Related

- [MCP](../features/mcp.md)
- [Tools reference](../reference/tools.md)
- [Configuration](../using-flowly/configuration.md)
- [Google Workspace](./google-workspace.md), [Linear](./linear.md), [Trello](./trello.md), [Home Assistant](./home-assistant.md)
- [Channels overview](../channels/overview.md)
