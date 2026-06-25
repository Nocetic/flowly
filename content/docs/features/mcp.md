---
title: MCP (Model Context Protocol)
eyebrow: Features
description: Connect Flowly to external MCP servers (GitHub, Linear, Notion, Playwright, your own) so the agent can call their tools — and expose Flowly itself as an MCP server to other clients.
---

MCP servers are configured through `flowly mcp` subcommands, the `/mcp` modal in the TUI, or by hand-editing the `mcpServers` block in `~/.flowly/config.json`.

> [!TIP]
> For the full deep reference (security guards, OSV gate, circuit breaker, every config key), see [the root MCP guide](../../../MCP.md).

## Adding a server

Use `flowly mcp add` for stdio (local subprocess) or HTTP servers:

```bash
# stdio: local subprocess
flowly mcp add context7 --command npx --arg -y --arg @upstash/context7-mcp

# HTTP (StreamableHTTP): remote URL
flowly mcp add acme --url https://mcp.example.com/mcp --header "X-Api-Key: ..."

# HTTP + OAuth
flowly mcp add linear --url https://mcp.linear.app/mcp --auth oauth
```

`--command` and `--url` are mutually exclusive; `--auth oauth` requires `--url`. Other flags: `--env KEY=VALUE`, `--timeout` (120s default), `--connect-timeout` (60s), `--probe`/`--no-probe`, `--force`.

> [!NOTE]
> A new session must start for newly-registered tools to appear.

### Transports

| Transport | Config | Notes |
|---|---|---|
| stdio | `command` + `args` (+ `env`) | Local subprocess. Default for local servers. stderr → `$FLOWLY_HOME/logs/mcp-stderr.log` |
| HTTP (StreamableHTTP) | `url` (+ `headers`) | First-class. Default for remote servers |
| SSE | `url` + `transport: sse` | For older SSE-style servers |

## The `mcpServers` config block

Servers live under the top-level `mcpServers` key in `~/.flowly/config.json`. Keys are camelCase. A real stdio example with an injected secret:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}" }
    }
  }
}
```

`${VAR}` interpolation works in `env`, `args`, and `headers`. Variables resolve at boot from `$FLOWLY_HOME/.env` (and the process environment, which wins on conflict).

> [!TIP]
> Store secrets in `.env` (mode 0600) and reference them by `${VAR}` rather than inlining them.

An HTTP server with OAuth:

```json
{
  "mcpServers": {
    "linear": {
      "url": "https://mcp.linear.app/mcp",
      "auth": "oauth"
    }
  }
}
```

For mTLS / a custom CA on HTTP/SSE servers, set `clientCert`, `clientKey`, and `sslVerify` (`true` | `false` | path to a CA bundle).

For stdio servers installed via `npx`/`uvx`/`pipx`, `osvCheck` (default `true`) queries the OSV API for known supply-chain malware advisories on the package before the server spawns. Set it to `false` to skip the check for a trusted or local server.

## Per-server tool filtering

Each remote tool registers as `mcp_{server}_{tool}` (non-alphanumeric characters become `_`). For example, Context7's `resolve-library-id` becomes `mcp_context7_resolve_library_id`. On a name collision the **existing tool wins** and the MCP one is skipped — Flowly's native tools are never overwritten.

Limit which of a server's tools the agent sees via the `tools` block:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "tools": {
        "include": ["search_repositories", "get_issue"],
        "exclude": [],
        "resources": false,
        "prompts": false
      }
    }
  }
}
```

- `tools.include` is a whitelist — if set, it wins and everything else is hidden.
- `tools.exclude` is a blacklist — used only when `include` is empty.
- Empty `include` + empty `exclude` exposes all tools.
- `resources` / `prompts` expose the server's resource/prompt utility tools when it advertises those capabilities.

`flowly mcp configure <name>` connects to the server, lists its tools, and gives you an interactive checkbox picker that writes `tools.include` for you.

## OAuth for remote servers

HTTP servers with `auth: oauth` use OAuth 2.1 + PKCE. Tokens are stored per-server at `$FLOWLY_HOME/mcp-tokens/{server}.json` (mode 0600) and auto-refreshed.

```bash
flowly mcp login linear
```

`flowly mcp login` clears any cached token, then runs the interactive browser authorization flow. The callback is pinned to `http://127.0.0.1:8765/callback`, so only one interactive MCP OAuth flow can run at a time, and that redirect URI must match the authorization server's registration. At agent boot, stored tokens are used/refreshed non-interactively — if a server needs login and no browser is available, that server is skipped and boot is never blocked. `flowly mcp remove` also clears the server's tokens.

> [!TIP]
> Some providers' OAuth (notably WorkOS-backed servers) complete the browser step but fail the **token exchange** with the raw MCP SDK — `flowly mcp login` returns a `401`/token-exchange error even though you authorized successfully. Wire those servers through [`mcp-remote`](https://www.npmjs.com/package/mcp-remote), the ecosystem-standard OAuth bridge, instead:
>
> ```bash
> flowly mcp add yargi --command npx \
>   --arg=-y --arg=mcp-remote@latest --arg=https://server.example.com/mcp \
>   --connect-timeout 300
> ```
>
> The first connect opens the browser, `mcp-remote` completes the OAuth and caches the token under `~/.mcp-auth`, and the bot reuses it on every boot. To Flowly this is a plain stdio server (no `auth: oauth`), so it needs **Node.js on the bot host**. The desktop app's **Requires OAuth sign-in** checkbox does all of this for you (see [Managing servers from the desktop app](#managing-servers-from-the-desktop-app)).

## The curated catalog

Flowly ships a curated catalog of ready-to-install servers. Browse and install them:

```bash
flowly mcp catalog            # table: Name / Auth / Transport / Description
flowly mcp install github     # resolve manifest, prompt for secrets, write config, probe
flowly mcp picker             # interactive catalog browser (TTY only)
```

The catalog has exactly 8 entries:

| Name | Transport | Auth |
|---|---|---|
| `context7` | stdio (npx) | none |
| `fetch` | stdio (uvx) | none |
| `time` | stdio (uvx) | none |
| `filesystem` | stdio (npx) | api_key (`MCP_FILESYSTEM_ROOT`) |
| `github` | stdio (npx) | api_key (`GITHUB_PERSONAL_ACCESS_TOKEN`) |
| `notion` | stdio (npx) | api_key (`NOTION_API_KEY`) |
| `playwright` | stdio (npx) | none |
| `linear` | HTTP | oauth |

`flowly mcp install <name>` resolves the manifest, prompts for any declared environment variables, saves them to `$FLOWLY_HOME/.env`, writes the `mcpServers` entry, and probes the server (probing is skipped for OAuth servers). It then prints the manifest's `post_install` note — for OAuth entries like `linear`, that note tells you to run `flowly mcp login linear` next. To see the current catalog at any time, run `flowly mcp catalog`.

## Running Flowly as an MCP server

Expose your Flowly conversation history to any MCP client (Claude Desktop, Cursor, Claude Code) over stdio:

```bash
flowly mcp serve                 # read-only (default)
flowly mcp serve --allow-writes  # also expose send + approvals (needs gateway)
flowly mcp serve --verbose
```

**Read tools** (always available, no gateway required — they read JSONL sessions and the FTS index directly):

| Tool | Purpose |
|---|---|
| `conversations_list` | List conversations (filter by platform / search) |
| `conversation_get` | Metadata for one `channel:chat_id` |
| `messages_read` | Recent user/assistant messages of a conversation |
| `messages_search` | Full-text search across all conversations (FTS5) |
| `channels_list` | Configured channels + enabled state |

**Write tools** (only with `--allow-writes`, and they require a running `flowly gateway`): `messages_send`, `approvals_list`, `approvals_resolve` (decision = allow-once / allow-always / deny). These reach the gateway over an authed localhost control endpoint (`$FLOWLY_HOME/gateway-api.json`); when the gateway is down they return a clear "gateway not running" message instead of failing.

> [!TIP]
> `serve` is read-only by default, so it is safe to point at your real `~/.flowly`.

Point a client at it the same way you would any stdio server:

```json
{
  "mcpServers": {
    "flowly": { "command": "/path/to/flowly", "args": ["mcp", "serve"] }
  }
}
```

## `flowly mcp` subcommands

| Command | What it does |
|---|---|
| `list` | Table of configured servers: Name / Transport / Tools filter / Status |
| `add <name>` | Add a server (`--command`/`--url`, `--arg`, `--env`, `--header`, `--auth oauth`, `--timeout`, `--connect-timeout`, `--probe`, `--force`) |
| `remove <name>` | Remove a server (`--yes`); also clears its OAuth tokens |
| `enable <name>` | Flip the server's `enabled` flag on |
| `disable <name>` | Flip the server's `enabled` flag off |
| `configure <name>` | Interactively pick enabled tools → writes `tools.include` |
| `serve` | Run Flowly as an MCP server (`--allow-writes`, `--verbose`) |
| `catalog` | List the curated catalog |
| `install <name>` | Install a catalog entry (`--force`, `--probe`) |
| `picker` | Interactive catalog browser (TTY only) |
| `test <name>` | Connect + list tools — a health check |
| `login <name>` | (Re)run the OAuth browser flow |

## The `/mcp` slash command

In the TUI, `/mcp` opens a modal to manage MCP servers and install entries from the curated catalog — the same operations as the CLI, without leaving the chat.

## Managing servers from the desktop app

Flowly Desktop has an **MCP** tab (Dashboard → MCP) for managing a bot's servers from a GUI — the same operations as the CLI and TUI, served over the bot's feature RPC. It works identically whether the selected bot is **local**, a **relay** bot, or a **direct self-hosted gateway**: there's one source of truth (the bot's `mcpServers` config), never a per-transport path.

The tab shows two groups:

- **Configured** — your servers, each with a status badge, an enable/disable toggle, **Test** (connect + list tools), and **Remove**.
- **Available** — installable curated-catalog entries. **Install** writes the entry, prompting first for any required secrets (which are saved to the bot's `.env`).

**Add server** opens a dialog with two transports:

- **Local (stdio)** — command + space-separated arguments + environment variables.
- **Remote (HTTP)** — URL + headers, with an optional **Requires OAuth sign-in**.

A change restarts the bot's gateway so newly-registered tools load at the next boot; the panel refreshes automatically when the bot reconnects.

### OAuth from the desktop

Checking **Requires OAuth sign-in** on a Remote (HTTP) server turns the dialog's button into **Sign in & add**, and Flowly wires the server through [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) rather than the native HTTP+OAuth path:

1. Click **Sign in & add** — a browser window opens for the provider's authorization (e.g. WorkOS).
2. Approve; `mcp-remote` caches the token.
3. The server is saved + enabled, and the bot reconnects using the cached token — no further sign-in.

This avoids the token-exchange failures some providers' OAuth has with the raw MCP SDK (a direct `auth: oauth` HTTP server may `401` on token exchange where `mcp-remote` succeeds). It requires **Node.js on the bot host** (for `npx`).

For OAuth servers the status badge reflects real authorization state — **sign-in needed** (enabled but no token yet) vs **signed in** — and a configured OAuth server you haven't signed into yet exposes a prominent **Sign in** button on its row. A plain **enabled** badge means the server is on in config; it is *not* a connectivity guarantee, so use **Test** to confirm a server actually connects.

> [!NOTE]
> The browser opens on the **bot host**. For a local/desktop bot that is your own machine, so sign-in is one click. For a remote/VPS bot, run the one-time `npx -y mcp-remote@latest <url>` on the host (over SSH) to cache the token there, then add the server as a **Local (stdio)** `mcp-remote` command from the tab.

## Related

- [Browser control](browser.md)
- [Computer use](computer-use.md)
- [Google Workspace](../integrations/google-workspace.md)
- [Tools reference](../reference/tools.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Root MCP reference](../../../MCP.md)
