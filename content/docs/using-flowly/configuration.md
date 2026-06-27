---
title: Configuration
eyebrow: Using Flowly
description: The canonical configuration reference for Flowly. All settings live in a single JSON file with camelCase keys; most users never edit it by hand, but every key is documented here.
---

## File path

```
<FLOWLY_HOME>/config.json
```

`FLOWLY_HOME` defaults to `~/.flowly`. The file is stored with owner-only (`0600`) permissions because it holds API keys.

Sibling files in the same directory:

- `config.json.bak` — a self-heal backup, seeded from the last good config.
- `config.json.broken-<unix-ts>` — a corrupted config moved aside during recovery.

## Profiles

A profile is an isolated `FLOWLY_HOME` with its own config, sessions, workspace, and credentials. The active profile is resolved in this order:

1. `-p` / `--profile <name>` CLI flag
2. `FLOWLY_PROFILE` environment variable
3. `~/.flowly/active_profile` sticky pointer file
4. `"default"`

The `default` profile is `~/.flowly`. A named profile `coder` lives at `~/.flowly/profiles/coder`. Profile names must match `^[a-z0-9][a-z0-9_-]{0,63}$`; the names `flowly`, `default`, `test`, `tmp`, `root`, and `sudo` are reserved. See [Environment variables](../reference/environment-variables.md).

## Self-healing loader

The loader is resilient by design:

1. It parses `config.json`. If valid, it's used (and a `.bak` is seeded if missing).
2. If parsing or validation fails, it tries `config.json.bak`. If that's good, the broken file is renamed `config.json.broken-<ts>` and the backup is restored.
3. If both fail, Flowly falls back to in-code defaults so the agent still boots.

> [!WARNING]
> If both `config.json` and `config.json.bak` fail to load, **secrets in the broken file are lost from the running process** (but the broken file is preserved on disk).

> [!NOTE]
> Saving is read-modify-write with a deep merge, so **unknown or manually-added keys are preserved**. A `None` value in the model never overwrites an existing non-`None` value on disk — to truly clear a value, write `""` or delete the key. Unknown top-level keys are ignored (`extra = "ignore"`), not errors. A handful of behaviors are overridable with the documented [`FLOWLY_*` environment variables](../reference/environment-variables.md); note that a generic nested override like `FLOWLY_GATEWAY__PORT` only takes effect on the fallback path (when `config.json` is missing or unparseable) — when a valid `config.json` exists, it is **not** consulted for those.

## Top-level keys

| Key | Purpose |
|---|---|
| `agents` | Agent defaults, compaction, heartbeat, memory search, multi-agent |
| `channels` | Messaging channels (Telegram, Discord, Slack, …) |
| `providers` | LLM providers and API keys |
| `gateway` | Gateway host / port |
| `tools` | Built-in tool toggles and limits |
| `integrations` | Third-party integrations (Trello, voice, X, …) |
| `audit` | Local audit-log retention |
| `plugins` | Enable / disable plugins |
| `mcpServers` | MCP server definitions |
| `backgroundMode` | `bool` (default `false`) |

### agents

`agents.defaults` — applied to the main agent:

| Key | Default |
|---|---|
| `workspace` | `"~/.flowly/workspace"` |
| `cwd` | `""` (empty → workspace; overridden by `FLOWLY_CWD`) |
| `model` | `"moonshotai/kimi-k2.5"` |
| `maxTokens` | `8192` |
| `temperature` | `0.7` |
| `actionTemperature` | `0.1` |
| `actionToolRetries` | `2` |
| `maxToolIterations` | `100` |
| `softWarnAtIteration` | `30` |
| `contextMessages` | `100` |
| `persona` | `"default"` |
| `saveTrajectories` | `false` |
| `memoryNudgeInterval` | `10` |
| `skillNudgeInterval` | `15` |

`agents.defaults.compaction` — see [Sessions](./sessions.md):

| Key | Default |
|---|---|
| `mode` | `"safeguard"` (`"default"` \| `"safeguard"`) |
| `reserveTokensFloor` | `20000` |
| `maxHistoryShare` | `0.5` |
| `contextWindow` | `128000` |
| `memoryFlush.enabled` | `true` |
| `memoryFlush.softThresholdTokens` | `4000` |

`agents.defaults.heartbeat`:

| Key | Default |
|---|---|
| `enabled` | `true` |
| `everyMinutes` | `30` |
| `activeHours` | `null` (or `{ start: "09:00", end: "23:00", timezone: "" }`) |
| `deliver` | `"none"` (`"none"` \| `"message_tool"`) |

`agents.defaults.memorySearch`:

| Key | Default |
|---|---|
| `enabled` | `true` |
| `provider` | `"auto"` (`"auto"` \| `"openai"` \| `"gemini"` \| `"none"`) |
| `model` | `""` |
| `apiKey` | `""` |
| `apiBase` | `""` |
| `chunkTokens` | `400` |
| `overlapTokens` | `80` |
| `maxResults` | `6` |
| `minScore` | `0.35` |
| `vectorWeight` | `0.7` |
| `textWeight` | `0.3` |

`agents.defaults.memoryDreaming` — cross-session "dreaming" + autonomous consolidation (see [Memory](../features/memory.md)):

| Key | Default |
|---|---|
| `enabled` | `true` (the whole governance/dreaming layer) |
| `commitMode` | `"selective"` (`"selective"` \| `"manual"` \| `"aggressive"`) |
| `idleMinutes` | `30` (run after this much agent inactivity; background heartbeats don't count) |
| `dailyEnabled` | `true` |
| `dailyTime` | `"03:30"` (HH:MM local) |
| `turnInterval` | `10` (also run every N user turns; `0` disables the coarse pass) |
| `autoFloor` | `0.80` (≥ → auto-active when unconflicted and not sensitive) |
| `reviewFloor` | `0.55` (< → dropped instead of queued) |
| `maxMessagesPerRun` | `500` (bound per pass so a backlog can't blow up one run) |
| `autoConsolidate` | `true` (background cleanup: merge duplicates, retire stale) |
| `consolidateTurnInterval` | `50` (consolidate every N user turns; `0` off) |
| `consolidateEveryMinutes` | `30` (background consolidation timer; `0` off) |
| `freezeInjectedMemory` | `false` (advanced: freeze the injected memory block per session for prefix-cache stability) |

`agents.agents` (per-agent map) — `name`, `provider` (`"anthropic"` default \| `"openai"` \| `"flowly"`), `model=""`, `workingDirectory=""`, `persona=""`.
`agents.teams` (per-team map) — `name`, `agents=[]`, `leaderAgent=""`.

### channels

Channels are off by default. See [Channels overview](../channels/overview.md).

| Channel | Key defaults |
|---|---|
| `whatsapp` | `enabled=false`, `bridgeUrl="ws://localhost:3001"`, `allowFrom=[]` |
| `telegram` | `enabled=false`, `token=""`, `allowFrom=[]`, `dmPolicy="pairing"` (`"open"`\|`"pairing"`\|`"allowlist"`) |
| `discord` | `enabled=false`, `token=""`, `allowFrom=[]`, `gatewayUrl="wss://gateway.discord.gg/?v=10&encoding=json"`, `intents=37377` |
| `slack` | `enabled=false`, `mode="socket"`, `botToken=""`, `appToken=""`, `groupPolicy="mention"`, `groupAllowFrom=[]`, `dm.enabled=true`, `dm.policy="open"`, `dm.allowFrom=[]` |
| `web` | `enabled=false`, `relayUrl=""`, `serverId=""`, `authToken=""`, `jwtSecret=""` |
| `email` | `enabled=false`, `pollIntervalSeconds=30`, `allowFrom=[]` |
| `teams` | `enabled=false`, `webhookUrl=""`, `defaultChatLabel=""`, `allowFrom=[]` |

### providers

| Key | Default / notes |
|---|---|
| `active` | `""` — explicit default provider slug; `""` falls back to the API-key cascade |
| `flowly` | `enabled=true`, `apiBase="https://useflowlyapp.com/api/v1"` (Flowly Cloud; uses account token when signed in) |
| `xaiOAuth` | `enabled=true`, `clientId=""`, `apiBase="https://api.x.ai/v1"` (tokens stored in OS keychain, not config) |

BYOK provider slots — each with `apiKey=""`, `apiBase=null`, `fallbackKeys=[]`: `anthropic`, `openai`, `openrouter`, `zhipu`, `vllm`, `gemini`, `groq`, `xai`.

When `active=""`, the API-key cascade picks the first usable provider in priority order: OpenRouter → Anthropic → OpenAI → xAI → xAI OAuth → Gemini → Groq → Zhipu → Sakana → vLLM. See [Providers and models](./providers-and-models.md).

### gateway

| Key | Default |
|---|---|
| `host` | `"127.0.0.1"` |
| `port` | `18790` (1–65535) |

### tools

See [Sandbox and approvals](./sandbox-and-approvals.md) for execution policy.

| Tool | Key defaults |
|---|---|
| `web.search` | `apiKey=""` (Brave), `maxResults=5`, `proxyUrl=""` |
| `exec` | `enabled=true`, `timeoutSeconds=300`, `maxOutputChars=200000`, `approvalTimeoutSeconds=120`, `cronMode="deny"` (`"deny"`\|`"approve"`) |
| `artifact` | `enabled=true`, `maxContentLength=500000` |
| `browserTab` | `enabled=false` |
| `computer` | `enabled=false`, `actionDelayMs=100`, `failsafe=true` |
| `codexSession` | `enabled=false`, `codexBin="codex"`, `codexHome=""`, `cwd=""`, `turnTimeoutS=600`, `postToolQuietTimeoutS=90`, `approvalPolicy="on-request"`, `sandbox="workspace-write"`, `exposeFlowlyTools=true` |

> [!IMPORTANT]
> The exec allowlist / per-command approval policy is **not** in `config.json` — only `enabled` and the runtime knobs above are. Approval policy lives in the approvals store at `~/.flowly/credentials/exec-approvals.json`. See [Sandbox and approvals](./sandbox-and-approvals.md).

### integrations

| Integration | Key defaults |
|---|---|
| `trello` | `apiKey=""`, `token=""` |
| `voice` | `enabled=false`, `bridgeUrl="http://localhost:8765"`, plus Twilio / STT / TTS credentials (`sttProvider="groq"`, `ttsProvider="elevenlabs"`, `ttsVoice="21m00Tcm4TlvDq8ikWAM"`, `language="en-US"`) |
| `x` | `bearerToken`, `apiKey`, `apiSecret`, `accessToken`, `accessTokenSecret` (all `""`) |
| `googleWorkspace` | `enabled=false`, `email=""` |
| `linear` | `apiKey=""` |
| `homeAssistant` | `url=""`, `token=""` (tools register only when both are set) |

### audit

| Key | Default |
|---|---|
| `enabled` | `true` |
| `retentionDays` | `90` (`-1` disables the age cap) |
| `maxSizeMb` | `100` (`0` disables the size cap) |

Audit records are written as daily JSONL files under `<FLOWLY_HOME>/audit/`. These keys control retention only.

### plugins

| Key | Default |
|---|---|
| `enabled` | `[]` |
| `disabled` | `[]` |

Bundled plugins load by default unless listed in `disabled`. User plugins under `$FLOWLY_HOME/plugins/<name>/` load only if listed in `enabled`. `disabled` overrides `enabled`.

### mcpServers

A map of server name → server config. Per server: `enabled=true`; stdio (`command=""`, `args=[]`, `env={}`) or http/sse (`url=""`, `headers={}`); `transport="auto"` (`"auto"`\|`"stdio"`\|`"http"`\|`"sse"`); `timeout=120.0`, `connectTimeout=60.0`; `auth=""` (`""`\|`"oauth"`); `tools.include=[]`, `tools.exclude=[]`, `tools.resources=false`, `tools.prompts=false`; plus TLS, sampling, and `osvCheck=true`. Server names are preserved verbatim by the loader.

## Example

A minimal `config.json` after setting an Anthropic key and a Telegram bot:

```json
{
  "providers": {
    "active": "anthropic",
    "anthropic": { "apiKey": "sk-ant-..." }
  },
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-5",
      "persona": "default"
    }
  },
  "channels": {
    "telegram": { "enabled": true, "token": "123:ABC", "dmPolicy": "pairing" }
  },
  "gateway": { "host": "127.0.0.1", "port": 18790 }
}
```

## Related

- [Sessions](./sessions.md)
- [Personas](./personas.md)
- [Running as a service](./service.md)
- [Providers and models](./providers-and-models.md)
- [Sandbox and approvals](./sandbox-and-approvals.md)
- [Channels overview](../channels/overview.md)
- [Setup wizard](../getting-started/setup-wizard.md)
- [CLI commands](../reference/cli-commands.md)
- [Environment variables](../reference/environment-variables.md)
