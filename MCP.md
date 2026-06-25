# Flowly + Model Context Protocol (MCP)

Flowly speaks [MCP](https://modelcontextprotocol.io) **both ways**:

- **As a client** — connect Flowly to external MCP servers (Context7,
  GitHub, Linear, Playwright, your own) and the agent calls their tools
  like any built-in.
- **As a server** — run `flowly mcp serve` so other MCP clients (Claude
  Desktop, Cursor, Claude Code, another agent) can read your Flowly
  conversation history and, optionally, send messages + resolve approvals.

Everything is managed from the `flowly mcp` command group, the `/mcp`
modal in the TUI, or by hand-editing `mcpServers` in
`~/.flowly/config.json`.

---

## Quick start

```bash
flowly mcp catalog              # browse curated, ready-to-install servers
flowly mcp install context7     # install one (probes the connection)
flowly mcp list                 # see what's configured
flowly mcp test context7        # connect + list its tools
# start a new session — the agent now has mcp_context7_* tools
```

In the TUI, run `/mcp` for the same thing with arrow keys.

---

## The `flowly mcp` command group

| Command | What it does |
|---|---|
| `flowly mcp list` | Table of configured servers (transport, tool filter, status) |
| `flowly mcp catalog` | List the curated, version-pinned servers shipped with Flowly |
| `flowly mcp install <name>` | Install a catalog server — prompts for any secret, probes, enables |
| `flowly mcp picker` | Interactive catalog browser (install with arrow keys) |
| `flowly mcp add <name> …` | Add a custom server (stdio or HTTP) |
| `flowly mcp remove <name>` | Delete a server (+ its cached OAuth tokens) |
| `flowly mcp enable / disable <name>` | Flip a server on/off without removing it |
| `flowly mcp test <name>` | Connect, list tools, disconnect — a health check |
| `flowly mcp configure <name>` | Interactively pick which of a server's tools are enabled |
| `flowly mcp login <name>` | Run / re-run the OAuth flow for an OAuth server |
| `flowly mcp serve [--allow-writes]` | Run Flowly itself as an MCP server (stdio) |

Changes take effect at the next agent boot — restart the gateway
(`flowly restart`) or start a new `flowly agent` / TUI session.

---

## Adding servers

### From the catalog (recommended)

```bash
flowly mcp install context7     # no auth
flowly mcp install github       # prompts for a GitHub token → $FLOWLY_HOME/.env
flowly mcp install linear       # OAuth → run `flowly mcp login linear` after
```

Curated set: `context7`, `fetch`, `time`, `filesystem`, `github`,
`linear`, `playwright`, `notion`.

### Custom stdio server (local subprocess)

```bash
flowly mcp add myfs --command npx -a -y -a @modelcontextprotocol/server-filesystem -a /tmp
flowly mcp add mytool --command uvx -a my-mcp-package --env MY_TOKEN=${MY_TOKEN}
```

`--arg/-a` (repeatable) for command args; `--env/-e KEY=VAL` (repeatable)
for subprocess env. Requires `node`/`npx` or `uv`/`uvx` on PATH.

### Custom HTTP / SSE server (remote)

```bash
flowly mcp add api --url https://my-mcp.example.com/mcp \
  --header "Authorization: Bearer ${MY_API_KEY}"
flowly mcp add api --url https://my-mcp.example.com/mcp --auth oauth   # OAuth 2.1
```

For SSE servers set `transport: sse` in config (see below). HTTP defaults
to StreamableHTTP.

---

## Secrets — keep them out of `config.json`

`config.json` is the agent's shared, plugin-readable config — never put
raw credentials there. Instead:

1. Store secrets in **`$FLOWLY_HOME/.env`** (`KEY=value`, one per line,
   mode 0600). `flowly mcp install` writes there automatically.
2. Reference them in config with **`${VAR}`** — resolved at boot from
   `.env` (and the process environment):

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

`${VAR}` works in `env`, `args`, and `headers`.

---

## Tool names & collisions

Each remote tool registers as **`mcp_{server}_{tool}`** (non-alphanumeric
characters become `_`). So Context7's `resolve-library-id` becomes
`mcp_context7_resolve_library_id`.

If a generated name collides with an existing tool, the **existing tool
wins** and the MCP one is skipped (logged). Flowly's native `linear`,
`trello`, etc. are never overwritten by an MCP server.

Use `flowly mcp configure <name>` to enable only a subset of a server's
tools (writes `tools.include`).

---

## Flowly as an MCP server (`flowly mcp serve`)

Expose your Flowly conversation history to any MCP client over stdio:

```bash
flowly mcp serve                 # read-only (default)
flowly mcp serve --allow-writes  # also expose send + approvals (needs gateway)
```

**Read tools** (no gateway required — reads JSONL sessions + the FTS
index directly):

| Tool | Purpose |
|---|---|
| `conversations_list` | List conversations (filter by platform / search) |
| `conversation_get` | Metadata for one `channel:chat_id` |
| `messages_read` | Recent user/assistant messages of a conversation |
| `messages_search` | Full-text search across all conversations (FTS5) |
| `channels_list` | Configured channels + enabled state |

**Write tools** (`--allow-writes`, require a running `flowly gateway`):
`messages_send`, `approvals_list`, `approvals_resolve`. These reach the
gateway over an authed localhost control endpoint
(`$FLOWLY_HOME/gateway-api.json`); when the gateway is down they return a
clear "gateway not running" message instead of failing.

### Connecting clients

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "flowly": { "command": "/path/to/flowly", "args": ["mcp", "serve"] }
  }
}
```

**Cursor** — `~/.cursor/mcp.json` (same shape).

**Claude Code**:

```bash
claude mcp add flowly -- /path/to/flowly mcp serve
```

`serve` is **read-only by default** and opt-in (only runs when you launch
it), so it's safe to point at your real `~/.flowly`.

---

## Transports, OAuth & TLS

- **stdio** — local subprocess (`command` + `args`). Default for local servers.
- **HTTP (StreamableHTTP)** — `url`. The default for remote servers.
- **SSE** — `url` + `transport: sse` for older SSE-style servers.
- **OAuth 2.1 / PKCE** — `auth: oauth` on an HTTP server. Run
  `flowly mcp login <name>` to authorize in the browser; tokens are
  stored (and auto-refreshed) under `$FLOWLY_HOME/mcp-tokens/`.
- **mTLS / custom CA** — `client_cert` / `client_key` and `ssl_verify`
  (`true` | `false` | CA-bundle path) for HTTP/SSE servers.

---

## Security

MCP servers run third-party code, so Flowly applies several guards:

- **OSV malware gate** — before an `npx`/`uvx` server spawns, Flowly
  queries the OSV database for known-malware advisories and blocks the
  spawn if any. Fail-open (network errors allow); per-server opt-out via
  `osv_check: false`.
- **Filtered subprocess env** — stdio servers get only a safe baseline
  (`PATH`, `HOME`, …) plus the `env` you list. Flowly's own provider keys
  are never inherited.
- **Credential redaction** — tokens/keys in error messages are replaced
  with `[REDACTED]` before the model or logs see them.
- **Prompt-injection scan** — tool descriptions are scanned for override
  patterns and logged (not blocked) so a hostile server is detectable.
- **Sandbox** — under `FLOWLY_SANDBOX=1` the whole agent (and its MCP
  subprocesses) runs inside `sandbox-exec` (macOS) / `bwrap` (Linux).
- **Circuit breaker** — a server that fails repeatedly is short-circuited
  for a cooldown so the model stops hammering it.

Subprocess stderr is redirected to `$FLOWLY_HOME/logs/mcp-stderr.log` so
servers can't corrupt the TUI; check it when debugging a failing server.

---

## `mcpServers` config reference

```json
{
  "mcpServers": {
    "example": {
      "enabled": true,
      "command": "npx",                  // stdio: command + args + env
      "args": ["-y", "@scope/pkg"],
      "env": { "TOKEN": "${TOKEN}" },
      "url": "",                         // http/sse: url + headers instead
      "headers": {},
      "transport": "auto",               // auto | stdio | http | sse
      "timeout": 120,                    // per-tool-call seconds
      "connectTimeout": 60,              // initial connect seconds
      "tools": {                         // optional filtering / utilities
        "include": [],                   //   whitelist (empty = all)
        "exclude": [],                   //   blacklist (ignored if include set)
        "resources": false,              //   expose resources/* utility tools
        "prompts": false                 //   expose prompts/* utility tools
      },
      "auth": "",                        // "" | "oauth"
      "scope": "",                       // optional OAuth scope
      "sslVerify": true,                 // true | false | CA-bundle path
      "clientCert": "",                  // mTLS cert (path or [cert, key])
      "clientKey": "",
      "osvCheck": true,                  // OSV malware gate
      "reapOrphans": false,              // force-kill orphaned stdio children (Linux)
      "supportsParallelToolCalls": false,
      "sampling": {                      // server-initiated LLM (off by default)
        "enabled": false,
        "model": "",
        "maxRpm": 10,
        "maxTokensCap": 4096,
        "allowedModels": []
      }
    }
  }
}
```

Keys are camelCase on disk (Flowly converts to snake internally). Server
names and `env`/`headers` keys are preserved verbatim.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Server won't connect | `flowly mcp test <name>`; read `$FLOWLY_HOME/logs/mcp-stderr.log` |
| `npx`/`uvx` not found | Ensure Node / uv is on PATH, or set an absolute `command` + `env.PATH` |
| Tools missing after add | Start a new session — MCP loads at agent boot (`flowly restart`) |
| OAuth stuck | `flowly mcp login <name>` to re-authorize |
| Server "unreachable, auto-retry in Ns" | Circuit breaker is open after repeated failures — fix the server, it recovers automatically |
