---
title: MCP (Model Context Protocol)
eyebrow: Features
description: Connect the Flowly agent you own to external tools, manage sign-in and permissions from Flowly apps, or give another agent limited access to Flowly.
---

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) is a standard way for agents and tools to work together. Flowly supports both directions:

- **Flowly as an MCP client:** connect services such as Linear, Notion, Canva, GitHub, Higgsfield, or your own MCP server. The personal AI agent you own can then use the tools you permit.
- **Flowly as an MCP server:** let another compatible agent use a deliberately limited part of Flowly with an expiring access key, or run one of the local MCP bridges from the CLI.

You do not need to understand MCP configuration files to connect a service. The Flowly apps provide the recommended setup flow. The CLI and `mcpServers` configuration remain available for advanced and headless installations.

## Choose the right path

| What you want | Recommended path |
|---|---|
| Connect a supported service | Open **MCP connections** in Flowly Desktop, iOS, or Android |
| Let Flowly request a connection while you are chatting | Ask it to connect the service, then review the connection card yourself |
| Add a custom local or remote MCP server | Use **Add connection** in an app, or `flowly mcp add` |
| Let another agent use selected Flowly tools | In Flowly Desktop, use **External agent access** |
| Expose conversation archives over a local MCP process | Run `flowly mcp serve` |
| Expose selected tools from a running local gateway | Run `flowly mcp tools` |

## Connect a service from a Flowly app

The owner-managed connection flow is available in Flowly Desktop, iOS, and Android. It works with the exact Flowly runtime you selected: local, direct self-hosted, relay-managed, or a selected profile runtime, as long as that runtime advertises MCP connection support.

1. Open the selected Flowly's **MCP connections** screen.
2. Choose a catalog service or add a custom connection.
3. Enter any required URL, local command, header, environment value, or account sign-in details in the secure setup screen—not in chat.
4. Flowly tests the connection and discovers its tools.
5. Choose **all tools**, **selected tools**, or **no tools**. You can separately enable resource and prompt access when the server provides them.
6. Confirm the review. Only then does Flowly publish the new configuration and apply it to the selected runtime.

Supported app actions include:

- Connect a new catalog or custom server.
- Sign in with OAuth and reauthorize an existing connection.
- Select exact tools and optional resource/prompt access.
- Enable, disable, retry, or remove a connection.
- See whether a connection is actually healthy, not merely present in configuration.

The setup draft expires after 10 minutes while waiting for connection verification and your decision. Closing the setup sheet does **not** cancel it; use the setup's cancellation action when you want to end it. Failure or cancellation before publication preserves the previous working configuration and credentials.

After you confirm, Flowly saves the configuration and then applies it to the live runtime. If that final step fails or is interrupted, the new settings remain saved. The screen reports the failure with `saved: true`; retry the connection and verify its live status. A failed apply does not automatically restore the previous settings.

> [!NOTE]
> App-managed changes use targeted live reconfiguration. Existing chat and mail connections do not restart just because you connected, disabled, or removed one MCP server. CLI, TUI, and manual configuration changes still need a new agent session or runtime restart before the registered tool list changes.

### Understanding connection status

**Saved** and **connected** are different facts. A server can be saved but disabled, waiting for sign-in, temporarily unreachable, or reconnecting.

| Status | Meaning |
|---|---|
| Disabled | Saved in configuration but unavailable to the agent |
| Sign-in needed | Enabled, but OAuth authorization is missing or no longer valid |
| Connecting | Flowly is opening the transport and discovering capabilities |
| Connected | The live connection is healthy |
| Degraded / reconnecting | A connection failed and Flowly is recovering it automatically |
| Parked | Fast retries were exhausted; Flowly now probes slowly until the server returns |
| Failed | Setup or the latest connection attempt could not complete; inspect the shown error |

Flowly does not automatically replay an operation that failed during a disconnect. The operation may have already changed external data, so an automatic retry could duplicate the effect.

## Request a connection from a conversation

You can say, for example, “Connect Linear” or “I want to use the Higgsfield MCP.” Flowly can create a connection request for that conversation, but it cannot approve its own access.

The request appears as a connection card in the supported app. You review the endpoint or catalog entry, complete sign-in, choose permissions, and confirm. If you leave and reopen the conversation, a still-pending request appears again. Cancelling it is explicit.

From chat, the agent can request a new connection, reauthorization, or permission review. Disabling or removing an existing connection remains an owner action in the MCP connections screen.

This conversation flow is intentionally limited:

- The agent cannot put credentials into the request.
- A request does not edit configuration or grant tools by itself.
- The agent cannot silently expand an existing connection's permissions.
- Cancelled and failed setup cannot be bypassed from chat.
- Scheduled runs cannot start an interactive connection request.
- The request is bound to the originating conversation and selected runtime; a model-written session identifier cannot redirect it elsewhere.

## OAuth sign-in

Remote HTTP servers with `auth: "oauth"` use native OAuth 2.1 discovery and PKCE.

### App-managed OAuth flow

1. The selected Flowly runtime discovers the provider's authorization endpoints and creates a short-lived request with PKCE and a random state value.
2. The authorization URL is delivered to the owner app over its authenticated feature connection.
3. The system browser opens on the **owner's device**, even when the selected runtime is on another machine.
4. The provider returns to an exact callback:
   - Desktop uses a random loopback URL on `127.0.0.1`.
   - iOS uses `https://useflowlyapp.com/api/auth/mcp/ios/callback`.
   - Android uses `https://useflowlyapp.com/api/auth/mcp/android/callback`.
5. The app validates that the callback belongs to the setup it started and sends the authorization result to the original selected runtime.
6. That runtime exchanges the code, stores the provider tokens, connects to the MCP server, and discovers its tools.
7. You review permissions. The new connection and credential slot are published together only after confirmation.

Provider tokens remain on the selected Flowly runtime under `$FLOWLY_HOME/mcp-tokens/`; the mobile app does not keep them. The website callback is a secure return path into the mobile app, not the place where provider tokens are stored or exchanged.

Failed or cancelled sign-in does not replace a working credential. If two processes race to update the same connection, stale setup cannot overwrite the newer credential state. Running connections detect credential changes, and concurrent `401` responses share a single refresh attempt.

> [!IMPORTANT]
> General OAuth support does not guarantee that every MCP provider accepts every redirect URI or dynamic client registration policy. A provider may require its own callback registration or release-specific validation. Flowly reports that provider error instead of saving a connection as healthy.

### CLI OAuth flow

For a manually configured OAuth server:

```bash
flowly mcp login linear
```

The CLI callback is fixed at `http://127.0.0.1:8765/callback`. The provider must accept that redirect, and only one interactive CLI MCP sign-in can use that port at a time. Stored tokens are reused and refreshed without opening a browser during normal startup. If interactive sign-in is needed but unavailable, Flowly skips that server instead of blocking startup.

Older configurations that use a recognized external OAuth helper are not silently rewritten. The app can offer an explicit migration; accepting it creates and tests a native connection before replacing the old setup.

## Catalog connections

The built-in catalog gives common services a known URL or local command and explains any required setup. The catalog can grow over time, so `flowly mcp catalog` is the source of truth for the installed version.

| Name | What it provides | Connection |
|---|---|---|
| `canva` | Work with designs and presentations | Remote HTTP + OAuth |
| `higgsfield` | Generate images, video, and audio; provider credits may be used | Remote HTTP + OAuth |
| `linear` | Find and manage issues, projects, and comments | Remote HTTP + OAuth |
| `notion-cloud` | Search and update a Notion workspace through account sign-in | Remote HTTP + OAuth |
| `github` | Work with repositories, issues, and pull requests | Local stdio + access token |
| `notion` | Work with pages, databases, and blocks | Local stdio + API key |
| `context7` | Retrieve current, version-specific library documentation | Local stdio |
| `fetch` | Fetch a URL as readable Markdown | Local stdio |
| `filesystem` | Read, write, and search only the directory you choose | Local stdio + allowed root path |
| `playwright` | Navigate, click, fill forms, and capture browser screenshots | Local stdio |
| `time` | Read time and convert time zones | Local stdio |

From the CLI:

```bash
flowly mcp catalog
flowly mcp install github
flowly mcp picker
```

`install` resolves the catalog manifest, asks for declared values, writes the connection, and probes it when possible. OAuth entries require sign-in before they can be connected. Local catalog servers may require their package runner, such as `npx` or `uvx`, to exist on the selected runtime host.

## Add a server with the CLI

Use `flowly mcp add` for a local stdio process or remote HTTP endpoint:

```bash
# Local subprocess
flowly mcp add docs --command npx --arg -y --arg @upstash/context7-mcp

# Remote Streamable HTTP with an explicit header
flowly mcp add acme \
  --url https://mcp.example.com/mcp \
  --header 'X-Api-Key: ${ACME_MCP_KEY}'

# Remote HTTP with OAuth
flowly mcp add linear \
  --url https://mcp.linear.app/mcp \
  --auth oauth
```

`--command` and `--url` are mutually exclusive. `--auth oauth` is valid only with an HTTP URL. Repeat `--arg`, `--env KEY=VALUE`, and `--header "Name: value"` as needed. Connection timeout defaults to 60 seconds and tool-call timeout to 120 seconds. The command probes by default; if a probe fails, Flowly asks whether to save the entry disabled.

After a CLI, TUI, or manual configuration change, start a new agent session or restart the affected runtime so its tool registry is rebuilt.

The TUI `/mcp` modal provides the same catalog and basic configuration operations without leaving the terminal.

## Tools, resources, and prompts

Each remote tool is registered as `mcp_{server}_{tool}`. Punctuation becomes `_`; for example, `resolve-library-id` on a server named `context7` becomes `mcp_context7_resolve_library_id`. If a name collides, the tool already registered in Flowly wins. An MCP server never overwrites another tool silently.

App-managed connections save an explicit permission mode:

| Mode | Result |
|---|---|
| `all` | All tools, including tools discovered later, except names in `tools.exclude` |
| `selected` | Only names in `tools.include`, minus `tools.exclude`; an empty list means zero regular tools |
| `none` | No tools, resources, or prompts; app confirmation also saves the connection disabled |
| `legacy` | Compatibility behavior for older hand-written configurations |

In `legacy` mode, a non-empty `include` list is a whitelist. Otherwise a non-empty `exclude` list is a blacklist. When both are empty, all tools are available. New app-managed setup does not rely on this ambiguous empty-list behavior.

For fixed access, choose `selected`. It is possible to permit resources or prompts without regular tools using `selected` and an empty include list. If no tools, resources, or prompts are selected, app confirmation converts the decision to `none`. Review permissions before enabling that connection again. External-agent keys always use exact tool names and never automatically inherit newly discovered tools, even when the underlying connection uses `all`.

When enabled, MCP resources and prompts appear as bounded utility tools:

- `mcp_{server}_list_resources`
- `mcp_{server}_read_resource`
- `mcp_{server}_list_prompts`
- `mcp_{server}_get_prompt`

They appear only when the server advertises the capability and you permitted it. Tool, resource, and prompt discovery follows pagination with cycle detection and configured page/item limits.

`flowly mcp configure <name>` connects to a configured server and opens an interactive tool picker.

## Connection reliability

Flowly supervises every MCP transport instead of treating the first disconnect as permanent.

- Fast failures use exponential backoff with jitter.
- After the fast retry budget, the connection becomes **parked** and is probed at a slower interval. Recovery has no terminal retry limit.
- Keepalive checks detect dead transports even when no tool call is active.
- A stable connection resets the consecutive-failure counter.
- Dynamic `tools/list_changed` notifications refresh every bound live registry. Removed or changed contracts invalidate stale handlers rather than calling them with an old schema.
- Existing calls are allowed to drain during targeted reload or optional lifetime recycling. New calls wait for the replacement connection.
- A failed operation is never replayed automatically.

Optional idle and maximum-lifetime recycling are disabled by default because some servers keep state that cannot be reconstructed. `lifecycle.lazyStart` can reuse a private discovery manifest after restart while leaving the actual transport closed until the first request.

Discovery manifests are hints, not proof that a server is reachable. The first operation reconnects and validates the full live contract. Manifests are private, bounded, expire by default after one day, and contain credential fingerprints rather than raw secrets. Invalid, stale, unsafe, or unsupported cache entries fall back to live discovery.

The manifest cache holds at most 128 entries and 16 MiB in total; one entry is limited to 1 MiB and 10000 tools. Its identity includes the exact server name, effective configuration and policy, profile, SDK/interpreter, working directory, and permitted subprocess environment. Separate Flowly processes may share these disk hints, but never share a live stdin/stdout connection or execution authority.

## Protocol and result compatibility

For external MCP servers, Flowly supports:

- Local stdio, Streamable HTTP, and legacy SSE transports.
- Modern stateless discovery and the older initialize handshake.
- `protocol: "auto"`, which tries modern discovery and falls back to the legacy handshake; `stateless` and `legacy` force one mode.
- Native MCP text, image, audio, resource-link, and embedded-resource content.
- Structured content, output schemas, annotations, and `_meta` fields.
- Bounded binary-media caching, so large base64 payloads are not expanded directly into the agent context.
- Server-initiated non-sensitive elicitation forms and optional, tightly bounded sampling.

Interoperability is tested against real stdio, HTTP, and SSE SDK peers and independent compatible clients. That is not a claim that every provider, client version, extension, or mobile association policy has been certified. Use `flowly mcp test <name>` for the exact server and version you plan to deploy.

## Let another agent use Flowly

There are three public bridges and one managed internal bridge. They serve different purposes.

### 1. Scoped external access key — recommended

Flowly Desktop can issue an independent, limited, expiring key for another compatible agent.

1. Open the selected Flowly's **MCP connections** screen in Desktop.
2. Under **External agent access**, choose **Create access key**.
3. Give the key a recognizable name.
4. Choose an existing owning conversation. It provides context for live actions.
5. Select the exact tools to expose. The initial selection is empty, and future tools are never added automatically.
6. Choose a lifetime of 1, 7, 30, or 90 days.
7. Create the key and save it immediately. Flowly shows the plaintext key only once.

The Desktop app currently creates and revokes external-agent keys. iOS and Android can manage MCP connections and OAuth, but do not currently create these keys.

The copied Streamable HTTP configuration has this shape:

```json
{
  "mcpServers": {
    "flowly": {
      "url": "https://your-flowly.example/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SCOPED_KEY"
      }
    }
  }
}
```

For a client that only accepts stdio MCP configuration, install the Flowly CLI on that client machine and use the compatibility adapter:

```json
{
  "mcpServers": {
    "flowly": {
      "command": "flowly",
      "args": ["mcp", "connect"],
      "env": {
        "FLOWLY_MCP_ENDPOINT": "https://your-flowly.example/mcp",
        "FLOWLY_MCP_ACCESS_KEY": "YOUR_SCOPED_KEY"
      }
    }
  }
}
```

The key is bound to one existing conversation and at most 64 exact tool names. A key cannot mint another key, change its owning conversation, or expand its tool set. The gateway stores only a SHA-256 digest, not the plaintext key. Listing keys never returns their secrets.

> [!IMPORTANT]
> Conversation-list, read, and search tools cover the **entire selected profile** when you grant them. The owning conversation supplies context for live actions; it does not narrow those archive tools to one conversation.

Revoking a key blocks new calls and cancels running calls where possible. It cannot undo an external action that was already sent. Deleting the owning conversation revokes its keys, and recreating a conversation with the same visible name does not restore them.

The `/mcp` endpoint uses native stateless Streamable HTTP. It rejects browser-origin requests, credentials in the query string, the gateway administration token, and plaintext non-loopback transport. For remote use, expose only the MCP route through a reachable HTTPS endpoint. A relay connection manages Flowly remotely but does **not** publish a public MCP URL for you.

If a reverse proxy terminates TLS and forwards plain HTTP locally, the upstream connection must use a loopback peer address and a loopback Host such as `127.0.0.1` or `localhost`. Forwarded-protocol headers alone do not make an insecure upstream request acceptable. Preserve the scoped Authorization header, restrict the published route to `/mcp`, and keep gateway control routes private.

### 2. Conversation archive server: `flowly mcp serve`

`serve` exposes Flowly conversation data over stdio or Streamable HTTP. It is read-only by default.

```bash
# Local stdio
flowly mcp serve

# Include send and approval tools; a gateway must be running
flowly mcp serve --allow-writes

# Local Streamable HTTP
flowly mcp serve --transport http --host 127.0.0.1 --port 8765 --path /mcp
```

Read tools, available without a running gateway:

| Tool | Purpose |
|---|---|
| `conversations_list` | List conversations with optional platform/search filters |
| `conversation_get` | Read metadata for one exact conversation key |
| `messages_read` | Read paginated visible history, including compacted and media-only messages |
| `messages_search` | Full-text search across visible conversations |
| `channels_list` | List configured channels, enabled state, and known targets |
| `attachments_fetch` | Return attachment metadata for a stable message ID, without raw bytes or local paths |
| `events_poll` | Read message/deletion events after an opaque cursor |
| `events_wait` | Wait cancellably for new events with a bounded timeout |

Retain the returned event cursor between calls and process restarts. `CURSOR_EXPIRED` means retention no longer covers that position; refresh the relevant history and continue from the supplied cursor. Hidden, withdrawn, and deleted content is excluded from public history and search.

With `--allow-writes`, a running gateway adds `messages_send`, `approvals_list`, and `approvals_resolve`. `messages_send` accepts an optional idempotency key. These calls use the authenticated local gateway control API and return a clear error when the gateway is unavailable.

For non-loopback HTTP, Flowly requires both TLS (`--tls-cert` and `--tls-key`) and a bearer token of at least 32 characters. Put that token in the environment variable named by `--auth-token-env` (default: `FLOWLY_MCP_TOKEN`). Use `--stateless` if each HTTP request must have no server-held session state.

### 3. Ephemeral live tools: `flowly mcp tools`

This local stdio bridge exposes selected tools from a **running** gateway for an exact existing conversation:

```bash
flowly mcp tools \
  --session cli:my-conversation \
  --allow-writes \
  --tool board_list \
  --tool board_add \
  --ttl 1800
```

The default lifetime is one hour; the maximum is eight hours. Repeat `--tool` to narrow the grant. Without `--allow-writes`, built-in availability is limited to registered read tools such as web read/search, media analysis, skill lookup, and Board read operations. Write mode can additionally expose registered image/voice generation and Board mutation tools.

The eligible built-in set is explicit and still depends on what the owning runtime has enabled:

| Read grant | Write grant also permits |
|---|---|
| `web_search`, `web_fetch`, `web_extract` | `image_generate`, `voice_generate` |
| `video_analyze`, `image_analyze` | `board_add`, `board_update`, `board_run` |
| `skill_view`, `skills_list` | |
| `board_list`, `board_get` | |

Configured third-party MCP tools can also pass through the owning Flowly runtime. Read-only grants accept only tools whose server declares `readOnlyHint: true`; other remote tools require write permission. Server annotations are claims, not an operating-system sandbox.

This bridge never exposes Flowly's shell, browser controller, or arbitrary internal registry tools. It is loopback-only and is a protocol permission boundary—not protection from another same-user process on the host.

Live-tool grants are bounded to 128 active grants. Each grant allows four executing and eight pending calls; global limits are 16 executing and 64 pending calls with at most 32 MiB of queued JSON arguments. One call accepts 12 MiB of JSON arguments, a 16 MiB result, and up to ten minutes of execution. Up to 1024 used request IDs are retained per grant: concurrent duplicates share the same execution, while a completed ID is rejected instead of replayed.

Local media input is limited to 8 MiB per file and must resolve inside the live workspace or media directory through secure no-follow access on supported systems. A reply can contain up to eight media attachments within the total result limit. Unsupported secure local-file access fails closed; validated public image URLs and image data URLs do not depend on that local path mechanism.

### 4. Managed per-turn access

When a supported managed coding session is configured to expose Flowly tools, Flowly creates a fresh private callback and a new exact grant for each turn. The credential is passed through the process environment rather than saved in configuration or command-line arguments. It is revoked on completion, cancellation, or error, and a later turn starts with a new process and grant.

The grant intersects the parent's current permissions and any exact per-turn allowlist. Unknown, ambiguous, malformed, or over-limit exclusive tool selection fails closed. A newly discovered tool cannot join an already-running turn. This prevents a delegated process or model-authored session value from widening its own authority.

Simple English and Turkish exclusive requests can narrow one turn, such as `Only use Context7`, `Sadece Context7 kullan`, or an exact tool name. Independently exclusive clauses intersect, and explicit named exclusions are subtracted. Only imperative selector clauses grant names: quoted examples, explanations, and mentions of forbidden tools cannot authorize themselves. This parser is bounded convenience, not unrestricted natural-language policy. Integrators that need an exact contract should supply the structured per-turn allowlist supported by the gateway; malformed structured input fails closed.

The managed process receives temporary configuration that contains only the callback and its granted tools. Direct external MCP connections and unrelated account-backed extensions are disabled for that managed turn; permitted configured MCP tools remain reachable through Flowly's parent-controlled callback. The temporary secret is removed from ordinary shell environments, and successful completion, cancellation, or failure revokes it before process teardown.

## Security boundaries

MCP connects Flowly to third-party code and external accounts. The important controls are:

- **Explicit owner confirmation:** app setup publishes nothing until connection, discovery, and permission review succeed.
- **Least-privilege tool filters:** choose all, selected, or none; new external-access keys start empty.
- **Untrusted-server consent:** `trust: "untrusted"` requires one-time approval for calls not declaring `readOnlyHint: true`.
- **Filtered subprocess environment:** stdio servers receive a safe baseline plus values explicitly configured for that server. Flowly provider credentials are not inherited.
- **OSV package check:** before spawning recognized `npx`, `uvx`, or `pipx` packages, Flowly checks OSV for known malware advisories. Network, parse, or unrecognized-package failures are fail-open, so this is not a complete supply-chain guarantee.
- **Private OAuth storage:** token files use private storage and are bound to the configured server identity.
- **Bounded diagnostics:** protocol logs and subprocess stderr are sanitized, rate-limited, size-limited, and written privately.
- **Transport policy:** owner-scoped remote access requires HTTPS and a distinct scoped key; gateway administration credentials are not accepted as MCP keys.
- **Bounded execution:** grants, request sizes, result sizes, concurrency, timeouts, pagination, and binary content all have limits.
- **No blind write replay:** Flowly does not retry a possibly side-effecting operation automatically.
- **Optional process sandbox:** when Flowly itself runs inside its supported sandbox, its MCP subprocesses inherit that boundary. See [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md).

The tool-description prompt-injection scan is diagnostic only; it logs suspicious patterns but does not prove that a server is safe. `readOnlyHint` and other annotations also come from the server. Use an operating-system sandbox and restricted provider credentials when running code you do not trust.

### Consent, elicitation, and sampling

MCP elicitation can route a server's non-sensitive, flat input form to the correct calling surface. Flowly validates primitive types and constraints before returning an answer. Missing ownership, denial, timeout, or cancellation declines the request. Credential fields, nested/reference schemas, and URL-mode elicitation are rejected.

Server-initiated model sampling is disabled by default. If enabled, it remains subject to per-minute, model, and token limits and may consume your configured model-provider quota. Only text sampling is supported.

## Diagnostics and operational limits

MCP protocol diagnostics and sanitized subprocess stderr are written to:

```text
$FLOWLY_HOME/logs/mcp/diagnostics.jsonl
```

The directory uses mode `0700` and files use `0600` on supported POSIX systems. One current file and two rotations are capped at 1 MiB each. Records and queues are bounded; under overload, diagnostics may be dropped rather than delaying tool calls. Runtime health includes received, written, filtered, dropped, pending, and disk-failure counters.

Each retained server has a 100-record-per-10-second budget and a 64-record writer queue. Serialized records are limited to 20 KiB, message text to 4096 characters, and framed stderr lines/records to 64 KiB. Oversized multiline stderr disables the remainder of that connection's stderr capture because Flowly cannot safely guess whether a secret continues in the discarded bytes. Unsafe links, file types, permissions, and oversized existing files are rejected instead of adopted.

Redaction covers configured credentials and common key/token formats, including multiline fragments. It is defense in depth, not a guarantee that every transformed secret can be recognized. Successful tool results are not modified as though they were log lines.

Owner-scoped external access is also bounded: a key has at most 64 tools and at most a 90-day lifetime; the store supports at most 256 records and 1 MiB of state. Calls permit 12 MiB of JSON arguments, a 16 MiB result, and roughly ten minutes of execution. The gateway allows up to four running calls per key and 32 across keys. These limits protect availability, but they do not roll back a side effect already accepted by an external service.

## `flowly mcp` command reference

| Command | What it does |
|---|---|
| `list` | List configured servers and their current configuration status |
| `add <name>` | Add a local command or remote URL; repeat args, environment values, and headers as needed |
| `remove <name>` | Remove a server and its associated OAuth credentials |
| `enable <name>` / `disable <name>` | Change whether a configured server is available |
| `configure <name>` | Discover tools and choose an allowlist interactively |
| `tools` | Start an expiring local bridge into a running gateway |
| `connect` | Adapt a scoped remote Streamable HTTP endpoint to stdio using `FLOWLY_MCP_ENDPOINT` and `FLOWLY_MCP_ACCESS_KEY` |
| `serve` | Expose Flowly archives and optional gateway writes over stdio or Streamable HTTP |
| `catalog` | Show the catalog included in this installation |
| `install <name>` | Install one catalog entry |
| `picker` | Browse and install catalog entries interactively |
| `test <name>` | Connect and discover tools as a health check |
| `login <name>` | Run or repeat OAuth sign-in for a manually configured HTTP server |

Useful `serve` options:

```text
--allow-writes
--verbose
--transport stdio|http
--host HOST
--port PORT
--path /mcp
--stateless
--auth-token-env FLOWLY_MCP_TOKEN
--tls-cert PATH
--tls-key PATH
```

Run `flowly mcp <command> --help` for the exact options supported by your installed version.

## `mcpServers` configuration reference

Servers live under the top-level `mcpServers` object in `$FLOWLY_HOME/config.json` (normally `~/.flowly/config.json`). Configuration keys are camelCase. Server names and environment/header names are preserved exactly.

`${VAR}` interpolation works in `env`, `args`, and `headers`. Values resolve from `$FLOWLY_HOME/.env` and the process environment, with the process environment winning. Keep secret files private and prefer references over inline credentials.

The annotated example below shows the defaults. Remove its `//` comments before copying it into `config.json`, which requires valid JSON. Replace the placeholder package with the server you intend to run.

```jsonc
{
  "mcpServers": {
    "example": {
      "enabled": true,

      // Choose command/args/env for stdio OR url/headers for HTTP/SSE.
      "command": "npx",
      "args": ["-y", "@scope/package"],
      "env": { "TOKEN": "${EXAMPLE_TOKEN}" },
      "url": "",
      "headers": {},

      "transport": "auto",       // auto | stdio | http | sse
      "protocol": "auto",        // auto | stateless | legacy
      "timeout": 120,
      "connectTimeout": 60,

      "auth": "",                // "" | oauth; OAuth is HTTP only
      "scope": "",
      "trust": "full",           // full | untrusted

      "tools": {
        "mode": "legacy",        // legacy | all | selected | none
        "include": [],
        "exclude": [],
        "resources": false,
        "prompts": false
      },

      "sslVerify": true,          // true | false | CA-bundle path
      "clientCert": "",          // combined PEM or supported cert tuple
      "clientKey": "",

      "supportsParallelToolCalls": false,
      "maxParallelToolCalls": 8,
      "reapOrphans": false,
      "osvCheck": true,

      "elicitation": {
        "enabled": true,
        "timeout": 300
      },

      "sampling": {
        "enabled": false,
        "model": "",
        "maxRpm": 10,
        "maxTokensCap": 4096,
        "allowedModels": []
      },

      "lifecycle": {
        "reconnectEnabled": true,
        "reconnectBaseDelay": 1,
        "reconnectMaxDelay": 30,
        "reconnectJitter": 0.2,
        "parkAfterAttempts": 8,
        "parkedProbeInterval": 300,
        "keepaliveInterval": 180,
        "keepaliveTimeout": 30,
        "stableConnectionSeconds": 30,
        "idleTimeout": 0,
        "maxLifetime": 0,
        "closeTimeout": 10,
        "lazyStart": false,
        "manifestTtl": 86400
      },

      "pagination": {
        "maxPages": 100,
        "maxItems": 10000
      },

      "content": {
        "maxBinaryBytes": 26214400
      },

      "logging": {
        "enabled": true,
        "level": "info"
      }
    }
  }
}
```

`oauthCredentialId` is an internal, app-managed credential-slot identifier. Do not copy it between servers or edit it to try to reuse an OAuth grant.

Important bounds:

- `elicitation.timeout`: greater than 0 and at most 600 seconds.
- `maxParallelToolCalls`: 1–256.
- `idleTimeout` and `maxLifetime`: 0 disables; otherwise at most 604800 seconds.
- `closeTimeout`: greater than 0 and at most 60 seconds.
- `manifestTtl`: greater than 0 and at most 604800 seconds.
- `pagination.maxPages`: 1–10000; `maxItems`: 1–1000000.
- `content.maxBinaryBytes`: 1 KiB–1 GiB.
- Logging levels: `debug`, `info`, `notice`, `warning`, `error`, `critical`, `alert`, `emergency`.

For mTLS, provide `clientCert` and `clientKey` in the supported form. `sslVerify: false` disables server-certificate verification and should be limited to controlled development environments.

## Troubleshooting

| Symptom | What to check |
|---|---|
| **Saved but not connected** | Open the connection detail. If the apply step failed after confirmation, the new settings are already saved; retry and verify live status. Also check whether it is disabled or needs sign-in. |
| Sign-in closes before the provider appears | Confirm the selected runtime supports the callback mode, the provider accepts the exact redirect URI, and the app link/associated domain opens the installed app. Restarting setup creates a new state value; do not reuse an old callback. |
| OAuth finishes but connection fails | Read the provider error shown by Flowly. The token exchange may have succeeded while the MCP endpoint, scope, or provider account still rejects discovery. Retry does not overwrite a working credential until the replacement is healthy. |
| Tools do not appear after app setup | Confirm permissions were saved as `all` or `selected`, then inspect live status. App setup reloads the one server without restarting chat. |
| Tools do not appear after CLI/TUI/manual setup | Start a new agent session or restart the affected runtime; those paths do not perform owner-managed live reload. |
| A server repeatedly reconnects | Let the row show the last error, run `flowly mcp test <name>`, and inspect `$FLOWLY_HOME/logs/mcp/diagnostics.jsonl`. Parked servers keep probing slowly. |
| `npx`, `uvx`, or `pipx` is missing | Install the required runner on the machine hosting the selected runtime, or use an absolute command and explicit `env.PATH`. |
| A local server works on one Flowly but not another | Local commands and files run on the selected runtime host, not necessarily the phone or Desktop app displaying the controls. |
| External agent cannot connect | Use the exact endpoint and one-time key from Desktop. Remote endpoints must be reachable over HTTPS; relay management alone is not an MCP URL. Check that the key is active, unexpired, tied to an existing conversation, and includes the requested tool. |
| A granted tool is still denied | Tool grants are an upper bound. Existing connection permissions, trust/approval policy, disabled-tool settings, and live tool availability still apply. |
| Event cursor expired | Refresh the relevant conversation history, then continue with the replacement cursor returned in `CURSOR_EXPIRED`. |

## Related

- [Configuration](../using-flowly/configuration.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Tools reference](../reference/tools.md)
- [Profiles](../using-flowly/profiles.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
