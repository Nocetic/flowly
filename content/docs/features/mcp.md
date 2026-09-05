---
title: MCP (Model Context Protocol)
eyebrow: Features
description: Connect Flowly to external MCP servers (GitHub, Linear, Notion, Playwright, your own) so the agent can call their tools — and expose Flowly itself as an MCP server to other clients.
---

Flowly speaks [MCP](https://modelcontextprotocol.io) **both ways**:

- **As a client** — connect Flowly to external MCP servers (Context7, GitHub, Linear, Playwright, your own) and the agent calls their tools like any built-in.
- **As a server** — run `flowly mcp serve` so other MCP clients (Claude Desktop, Cursor, Claude Code, another agent) can read your Flowly conversation history and, optionally, send messages and resolve approvals.

Everything is managed from the `flowly mcp` command group, the `/mcp` modal in the TUI, the desktop **MCP** tab, or by hand-editing the `mcpServers` block in `~/.flowly/config.json`. Changes take effect at the next agent boot — restart the gateway (`flowly restart`) or start a new session.

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

## Connection lifetime and idle recycling

For servers whose state survives a new connection/process, you can opt into
demand-driven connection recycling:

```json
"lifecycle": {
  "idleTimeout": 300,
  "maxLifetime": 3600,
  "closeTimeout": 10
}
```

Times are seconds. `idleTimeout` and `maxLifetime` default to `0` (disabled),
and accept values up to 604800 (seven days). `closeTimeout` defaults to 10 and
must be greater than zero and no more than 60. Recycling is deliberately
opt-in: some MCP servers keep non-reconstructable session state in memory.
Flowly cannot migrate that state by checking a tool schema.

- Idle time counts user requests, not keepalive traffic. An idle server closes
  its transport and stdio child, but its discovered tools remain available.
- At the lifetime deadline, the server enters `draining`: admitted tool,
  resource and prompt requests finish, including their queue/elicitation time.
  New requests wait for a fresh session rather than extending the old one.
- Closure finishes before the next transport opens. A cleanup deadline bounds
  an unresponsive close; shutdown is terminal and never wakes an idle server.
- Concurrent callers share one wake-up. Each caller waits at most
  `connectTimeout` for admission; one timeout/cancellation does not cancel the
  other callers or an already admitted operation. Failed operations are not replayed.
- Every new connection discovers the current catalog. An old tool handler is
  rejected if its schema, description, output contract or permission metadata
  changed, or if it vanished. The caller must fetch the new schema and retry
  deliberately; untrusted writes still need approval before a sleeping server
  can be started. All bound agent registries receive the refreshed catalog.

### Persistent discovery and lazy application startup

Set `lifecycle.lazyStart` to `true` to retain discovered tool manifests across
Flowly restarts. It defaults to `false`; the first discovery always connects.
After a successful complete discovery, later boots can load the manifest and
leave the transport idle until a tool/resource/prompt request needs it. This
works independently of the idle/lifetime timers above, and can be combined with them.

`lifecycle.manifestTtl` defaults to 86400 seconds (one day), with a positive
maximum of 604800 seconds (seven days). It controls whether a saved catalog is
usable at startup, not a guarantee that the remote catalog stayed unchanged.
Cached schemas are discovery hints: the first call still negotiates a connection
and revalidates the complete live contract before sending the operation.
Explicit connection probes always connect. Inspect `catalogSource` in runtime
health to distinguish `manifest` from `live` discovery; cached readiness does
not mean the server is currently reachable.

Manifests live in `$FLOWLY_HOME/cache/mcp-manifests/` with private directory/file
permissions. Published manifests are capped at 128 entries / 16 MiB, each at 1 MiB
and 10000 tools. Old entries and interrupted temporary writes are reclaimable
discovery metadata, not conversation or server state. Identity includes exact
server name, effective configuration/policy, resolved profile, SDK, interpreter,
working directory and allowed subprocess environment. OAuth login, refresh or
logout invalidates hints tied to the previous credential file. Connection
configuration and OAuth tokens contribute only fingerprints, not their raw
values. Remote tool schemas/metadata are retained, which is why the files are private.

Corrupt, expired, oversized, unsafe or mismatched cache entries fall back to
live discovery. Secure descriptor-relative caching is available on macOS/Linux;
other platforms use live discovery. Packaged builds without SDK distribution
metadata cannot reuse manifests across process restarts. These fallbacks do
not disable MCP transports.

Concurrent initial discoveries within one process share a single startup and
retain each consumer registry. Cancelling the last waiter joins startup cleanup;
cancelling just one waiter does not affect peers. A same-name server cannot be
silently reused under different configuration or profile authority: reload
configured servers after such a change. Separate Flowly processes share only
disk hints, never stdin/stdout connections or execution permission.

## OAuth for remote servers

HTTP servers with `auth: oauth` use OAuth 2.1 + PKCE. Tokens are stored per-server at `$FLOWLY_HOME/mcp-tokens/{server}.json` (mode 0600) and auto-refreshed.

```bash
flowly mcp login linear
```

`flowly mcp login` stages fresh browser authorization separately from the working credentials. CLI and desktop sign-in publish the new grant only after a successful connection; failed/cancelled sign-in leaves the previous grant intact. If another process changes or removes the credentials during sign-in, the stale login cannot overwrite that newer state. The callback is pinned to `http://127.0.0.1:8765/callback`, so only one interactive MCP OAuth flow can run at a time, and that redirect URI must match the authorization server's registration. At agent boot, stored tokens are used/refreshed non-interactively — if a server needs login and no browser is available, that server is skipped and boot is never blocked. `flowly mcp remove` also clears the server's tokens.

Running connections reload credential state before requests, including changes from another process. Token expiry and authorization-server discovery are persisted. Concurrent 401 responses share one refresh per credential file; ordinary tool calls and long-lived SSE streams do not hold that recovery lock. A cancelled or terminated process releases the lock automatically. Temporary authorization-server failures retain credentials and briefly back off, while a rejected refresh requires interactive sign-in. Permission/scope increases never authorize themselves in a non-interactive session.

An expired stateful MCP session is different from expired OAuth credentials: Flowly reconnects the MCP session without deleting tokens. It does not automatically replay the failed tool operation, because that operation may have side effects. New credential files are bound to the configured server name and URL; legacy files acquire that binding on their next write.

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
| `messages_read` | Paginated visible archive history, including compacted and media-only messages |
| `messages_search` | Full-text search across all conversations (FTS5) |
| `channels_list` | Configured channels + enabled state + exact known conversation targets |
| `attachments_fetch` | Attachment metadata for a stable message ID; no raw bytes or local paths |
| `events_poll` | New message/deletion events after an opaque, restart-safe cursor |
| `events_wait` | Cancellable wait for new events, with a bounded timeout |

Start event tracking without a cursor, then retain `next_cursor` for subsequent
polls/waits, including after restarting the MCP process. Keep the same session
filter. `CURSOR_EXPIRED` explicitly indicates a retention gap: refresh history
and resume from the supplied cursor. Events are discovered at poll time, not a
guaranteed push-delivery stream. Hidden, withdrawn and deleted messages are
excluded from public history, search and subsequent event reads.

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

## Live tools for external agents

`flowly mcp tools` is a separate stdio server for tools in a **running** Flowly
gateway. Unlike `mcp serve`, it uses the live registry, provider and task board;
it does not start a second agent or create a separate Board database.

```bash
# Use an exact, existing conversation key from conversations_list.
flowly mcp tools --session cli:my-conversation

# Explicitly permit selected writes, not every available capability.
flowly mcp tools --session cli:my-conversation --allow-writes \
  --tool board_add --tool board_list --ttl 1800
```

Configure any compatible stdio MCP client with the executable and arguments:

```json
{
  "mcpServers": {
    "flowly-live": {
      "command": "/absolute/path/to/flowly",
      "args": ["mcp", "tools", "--session", "cli:my-conversation"]
    }
  }
}
```

The default grant can expose web search/fetch/extract, video/image analysis,
skill lookup/listing and Board list/get **only where registered and enabled**.
`--allow-writes` additionally permits image/voice generation and Board add,
update and run. Repeat `--tool` to narrow either set. Flowly's built-in browser,
shell and arbitrary non-MCP registry tools are not exposed by this bridge.
Registered third-party MCP tools are also available through the owning Flowly
client: read-only grants include only tools declaring `readOnlyHint: true`,
plus enabled resource/prompt utilities. Other remote tools require a write
grant. These annotations are server claims, not an operating-system sandbox;
Flowly's existing trust, consent, tool filters and OAuth policy still apply.
Analysis and generation can incur charges on the user's configured providers.
Image analysis uses the selected live chat model without silently substituting
another; a model without image support returns an error. Speech keeps the
configured voice/model. Generated images and audio return native MCP content.

Authorization comes from a bounded, expiring grant, not tool arguments. The
local launcher uses the gateway's owner-only discovery credential to obtain a
grant for an existing conversation. Calls carry only that scoped grant, which
cannot issue other grants or change its session or tool set. Board write
attribution and hook ownership use that session; the Board itself is shared
installation data, not a per-conversation private store. Current channel tool
availability and pre-tool hooks are enforced again before dispatch.

This endpoint is loopback-only. It is a protocol permission boundary, **not an
OS sandbox against another process running as the same user**. The public
launcher targets the advertised standalone gateway; it does not discover profile
runtimes or choose a Desktop broker on its own.

Managed coding sessions (`tools.codexSession.exposeFlowlyTools: true`) instead
receive a fresh, private loopback callback for each turn. This works without
an advertised gateway, including TUI and named-profile execution. The callback
uses the parent's live registry and captured profile/Board reverse-RPC owner.
Its exact tool grant intersects the parent's per-turn permissions; read-only
sandbox settings also exclude write-capable tools. Empty grants stay empty.
No session key in a model-authored argument can change the owner.

The managed client gets temporary configuration overrides. Other direct MCP
connections, account-backed apps and plugins are disabled in the delegated
client; registered
Flowly MCP integrations remain reachable through the callback, with the parent
client's policies. Before a model turn starts, MCP discovery must contain the
callback and exactly its granted tool names, with no extra active server tools.
Unsupported client configuration/protocols fail explicitly rather than falling
back to an unrestricted or stateless callback.

The transient credential is inherited via an environment variable, not written
to configuration or argv. Managed launches disable shell snapshots and clear
that variable for ordinary shell commands. Existing configuration/model choices
are not rewritten by these per-turn overrides (the separate legacy enable/boot
migration still manages its existing configuration block). Successful completion,
cancellation and errors revoke authority before process teardown. The next turn
starts a new process with a new grant and explicitly resumes stored conversation
history. This adds startup overhead but prevents stale warm-process authority;
missing stored history is reported rather than silently replaced.

### Per-turn exclusive tool scopes

Explicit English and Turkish selectors narrow the tool surface for one turn:
`Only use Context7`, `Sadece Context7 kullan`, and `Context7 kullan, başka
araç kullanma` select the same registered server family. An exact tool name
selects that tool; source-qualified short names disambiguate identical names
on different servers. Unqualified ambiguous names and unknown exclusive
targets grant nothing. Server and tool selectors can be combined in one list.
Explicit named exclusions are subtracted; independently exclusive clauses
intersect instead of accumulating permissions.

Only imperative selector clauses contribute names: a forbidden tool, an
explanatory mention, quoted instruction, or fenced example cannot grant itself
authority. The bounded grammar is a convenience, not general natural-language
understanding. Clients that need an exact contract should send `allowedTools`
on gateway chat requests (internally `allowed_tools`). A supplied malformed
allowlist fails closed; an absent/null list preserves the usual runtime policy.
Broad `tools_allowed: true` does not erase an explicit exclusive selector.
Structured denies continue to win, and every derived grant intersects the
transport's positive ceiling and existing disabled-tool settings.

The resulting positive ceiling stays task-local through schema disclosure,
execution and delegated MCP calls. A newly discovered tool cannot join that
turn even if it belongs to the selected server. Later independent turns can
use the refreshed catalog. Concurrent turns keep separate grants. Policy
parsing is bounded to 65,536 characters and 256 clauses; over-limit requests
run without tools rather than ignoring an unchecked suffix.

Operational limits: one-hour grants by default (maximum eight hours), 128
active grants, four executing/eight pending calls per grant, 16 executing/64
pending globally, and 32 MiB of aggregate pending JSON arguments. Each call
allows 12 MiB of JSON arguments and a 16 MiB result. Calls time out after ten
minutes. A grant retains up to 1,024 used request IDs; concurrent duplicates
share execution, completed IDs are rejected rather than replayed. The launcher
revokes its grant on orderly shutdown; expiry, session deletion and gateway
shutdown also revoke calls. Cancellation cannot roll back an already-executed
external side effect, so clients must not blindly retry failed writes.

Local image/video inputs and generated media are bounded to eight MiB per file and
restricted to the live workspace/media directory. Local reads require secure
no-follow directory descriptors (macOS/Linux); unsupported platforms fail
closed for local files. Public image URLs and validated image data URLs do not
need those descriptors. Media replies support up to eight attachments within
the total response limit; oversize results return an explicit error, not partial
content. Source schemas are preserved without provider-specific flattening.

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
| `tools` | Expose live gateway tools over stdio (`--session`, `--allow-writes`, repeated `--tool`, `--ttl`) |
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

## Security

MCP servers run third-party code, so Flowly applies several guards:

- **OSV malware gate** — before an `npx`/`uvx` server spawns, Flowly queries the [OSV](https://osv.dev) database for known-malware advisories and blocks the spawn if any match. Fail-open (a network error allows the spawn); per-server opt-out via `osvCheck: false`.
- **Filtered subprocess env** — stdio servers get only a safe baseline (`PATH`, `HOME`, …) plus the `env` you explicitly list. Flowly's own provider keys are never inherited.
- **Credential redaction** — tokens and keys in error messages are replaced with `[REDACTED]` before the model or the logs see them.
- **Prompt-injection scan** — tool descriptions are scanned for override patterns and logged (not blocked) so a hostile server is detectable.
- **Sandbox** — under `FLOWLY_SANDBOX=1` the whole agent (and its MCP subprocesses) runs inside `sandbox-exec` (macOS) / `bwrap` (Linux). See [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md).
- **Circuit breaker** — a server that fails repeatedly is short-circuited for a cooldown (you'll see "unreachable, auto-retry in Ns") so the model stops hammering it; it recovers automatically.

Subprocess stderr is redirected to `$FLOWLY_HOME/logs/mcp-stderr.log` so a chatty server can't corrupt the TUI — check it first when debugging.

## MCP consent and user input

For an integration that should ask before writing, set `trust` to `untrusted`.
Every tool call without `readOnlyHint: true` then needs an explicit one-time
approval on the calling conversation's surface. Existing manually configured
integrations default to `full` for compatibility. Tool annotations are claims
made by the server: this gate is not a replacement for a process sandbox.

MCP elicitation routes non-sensitive flat forms through Flowly's approval and
question UI. Answers are type/constraint validated before being shared. A
missing caller, denial, timeout or cancellation never becomes silent consent.
URL-mode, nested/reference schemas and credential fields are explicitly
declined. Disable the feature per server with `elicitation.enabled: false`.
Legacy calls serialize while elicitation is enabled to avoid routing a
server-initiated question to the wrong concurrent user. Modern input-required
continuations retain per-call ownership and bounded parallelism.

## `mcpServers` config reference

Common server settings (camelCase on disk; Flowly converts to snake internally — server names and `env`/`headers` keys are preserved verbatim):

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
      "protocol": "auto",                // auto | stateless | legacy
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
      "trust": "untrusted",              // full (default) | untrusted
      "elicitation": { "enabled": true, "timeout": 300 },
      "sslVerify": true,                 // true | false | CA-bundle path
      "clientCert": "",                  // mTLS cert (path or [cert, key])
      "clientKey": "",
      "osvCheck": true,                  // OSV malware gate
      "reapOrphans": false,              // force-kill orphaned stdio children (Linux)
      "supportsParallelToolCalls": false,
      "maxParallelToolCalls": 8,
      "lifecycle": {                     // opt-in for restart-safe servers
        "idleTimeout": 0,                // 0 disables idle closure
        "maxLifetime": 0,                // 0 disables lifetime draining
        "closeTimeout": 10,              // bounded recycle teardown
        "lazyStart": false,              // opt-in persistent discovery hints
        "manifestTtl": 86400              // saved catalog acceptance window
      },
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

## Troubleshooting

| Symptom | Check |
|---|---|
| Server won't connect | `flowly mcp test <name>`; read `$FLOWLY_HOME/logs/mcp-stderr.log` |
| `npx`/`uvx` not found | Ensure Node / uv is on `PATH`, or set an absolute `command` + `env.PATH` |
| Tools missing after add | Start a new session — MCP loads at agent boot (`flowly restart`) |
| OAuth stuck | `flowly mcp login <name>` to re-authorize; for WorkOS-style servers use the `mcp-remote` bridge above |
| "unreachable, auto-retry in Ns" | Circuit breaker is open after repeated failures — fix the server; it recovers automatically |

## Related

- [Browser control](browser.md)
- [Computer use](computer-use.md)
- [Google Workspace](../integrations/google-workspace.md)
- [Tools reference](../reference/tools.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
