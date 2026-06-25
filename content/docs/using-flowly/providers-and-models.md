---
title: Providers & models
eyebrow: Using Flowly
description: Flowly talks to LLMs through pluggable providers — the hosted Flowly proxy or your own keys (BYOK) for OpenAI-compatible and native providers. This page covers the supported providers, configuration, runtime switching, key rotation, prompt caching, and the model catalog.
---

## Supported providers

| Provider key | Auth | Canonical API base |
| --- | --- | --- |
| `flowly` | Hosted — account token (`serverId:gatewayAuthToken`) | `https://useflowlyapp.com/api/v1` |
| `openrouter` | BYOK `api_key` | `https://openrouter.ai/api/v1` |
| `anthropic` | BYOK `api_key` | `https://api.anthropic.com/v1` |
| `openai` | BYOK `api_key` | `https://api.openai.com/v1` |
| `xai` | BYOK `api_key` | `https://api.x.ai/v1` |
| `xai_oauth` | OAuth (`flowly xai login`) | `https://api.x.ai/v1` |
| `gemini` | BYOK `api_key` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `groq` | BYOK `api_key` | `https://api.groq.com/openai/v1` |
| `zhipu` | BYOK `api_key` | `https://open.bigmodel.cn/api/paas/v4` |
| `vllm` | BYOK `api_key` | none (self-hosted — you must set `apiBase`) |

The API bases are built in; you normally only supply a key. All listed endpoints (except `xai_oauth`) speak the OpenAI Chat-Completions wire protocol. The `xai_oauth` provider uses xAI's Responses API instead.

> [!NOTE]
> On direct `anthropic` BYOK: the canonical base is Anthropic's native API, which is not OpenAI-Chat-Completions-shaped, so direct Anthropic BYOK is a documented-but-questionable path. To run Claude reliably, route it through OpenRouter or the Flowly hosted proxy.

## Configuring providers (BYOK)

BYOK keys go under `providers.<name>` in `~/.flowly/config.json`. On-disk keys are camelCase:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-...",
      "apiBase": "https://openrouter.ai/api/v1",
      "fallbackKeys": ["sk-or-...", "sk-or-..."]
    }
  }
}
```

| Field | Purpose |
| --- | --- |
| `apiKey` | The provider key (whitespace-stripped). |
| `apiBase` | Overrides the built-in base if set (required for `vllm`). |
| `fallbackKeys` | Extra keys for rotation (see below). |

You can enter a BYOK key via the setup wizard:

```bash
flowly setup byok <slot> --key <...>
```

## Flowly hosted

The hosted provider has no API key. Sign in with your account and Flowly uses an account-derived bearer token (`serverId:gatewayAuthToken`):

```bash
flowly login
```

Login uses a device-code flow (a one-click browser URL plus polling) and stores your account in the keychain or `~/.flowly/credentials/account.json` (mode `0600`). After a fresh login Flowly registers the machine, wires the relay channel, and auto-selects `providers.active = "flowly"` **only if nothing is set yet**. The hosted provider is gated on `providers.flowly.enabled` (default true) and a usable account.

```bash
flowly login --repair            # re-register + re-wire without a browser
flowly login --repair --dry-run
flowly logout                    # clears account; preserves BYOK keys
```

## xAI / Grok OAuth

For a Grok subscription, authenticate with xAI's OAuth (PKCE) flow:

```bash
flowly xai login          # sets active provider + default Grok model
flowly xai status
flowly xai logout
flowly xai test           # hits /v1/models
```

Tokens are stored in the keychain or `~/.flowly/credentials/xai_oauth.json` (mode `0600`), not in `config.json`. The client id is fixed (xAI has no self-service client registration). Use `flowly xai login --no-set-active` to authenticate without switching the active provider.

## Switching providers and models at runtime

The active provider is resolved in this priority order:

1. `providers.active`, if that provider is currently usable (sticky; falls through if not).
2. `flowly` hosted, if enabled and signed in.
3. The BYOK cascade — first usable of `openrouter`, `anthropic`, `openai`, `xai`, `xai_oauth`, `gemini`, `groq`, `zhipu`, `vllm`.

Switch live from the TUI:

```text
/provider [name]   # write providers.active, then hot-reload the gateway
/model [id]        # write agents.defaults.model, then hot-reload
```

Both open a picker if you omit the argument. `/model`'s picker loads the live catalog for the active provider.

### Hot-reload

`/provider` and `/model` write config and tell the running gateway to reload — no restart. The gateway re-reads config, re-resolves the active provider, and **builds the new provider before swapping**, so a build error (for example an empty key) leaves the old provider in place. If the gateway is offline, the TUI reports "gateway offline — restart to apply".

### Choosing the model

The model is chosen during `flowly setup` and changed later via `/model` (or `/provider` to switch providers). It is stored as `agents.defaults.model` in `config.json`:

```json
{
  "agents": {
    "defaults": {
      "model": "openrouter/some-model-id"
    }
  }
}
```

> [!TIP]
> Set the model interactively with `/model <id>` rather than hand-editing when possible, so the picker can validate against the live catalog.

## Key rotation

When a provider slot has **more than one key** (`apiKey` plus `fallbackKeys`), Flowly creates a key rotator:

- On an auth/rate-limit/overload failure, the current key is marked failed with a **60-second cooldown** and the next available key is picked round-robin. If every key is in cooldown, the one expiring soonest is used rather than failing outright.
- Rotation only happens with more than one key — a single key never rotates.
- **Rotation does not happen during streaming.** Streaming picks a key once; on a stream-open failure it yields an error without rotating. Only non-streaming calls rotate.
- **`xai_oauth` has no rotator** — it does a single token refresh and one retry on HTTP 401.
- **Flowly hosted does not use fallback keys** — it uses a single refreshable account token.

## Prompt caching

> [!NOTE]
> Prompt caching is **Anthropic / Claude only** — it is applied solely to models whose id contains `claude`. Other providers and models are unaffected (and the xAI OAuth provider never caches).

- **Strategy:** up to 4 cache breakpoints — one on the system prompt plus up to 3 on the most recent non-system, non-tool messages.
- **TTL:** default **`1h`**. Supported values are `5m` and `1h`. Override at process start:

  ```bash
  FLOWLY_CLAUDE_CACHE_TTL=5m flowly ...
  ```

  Invalid values fall back to `1h`.

## Model catalog (live vs empty)

Flowly fetches the model list **live, per provider**, from each provider's `/v1/models` endpoint (cached in-memory per session). Providers differ in whether a catalog is available:

| Provider | Catalog source |
| --- | --- |
| `openrouter` | Live `GET /models` (public, filtered to tool-capable models, free-first). |
| `flowly` | Live `GET {base}/models` (plan-filtered with `allowed`/`locked` tags; degrades to OpenRouter on no-account/network/401). |
| `xai` | Live `GET /v1/models` with your BYOK key. |
| `xai_oauth` | Live `GET /v1/models` with the OAuth token. |
| `anthropic`, `openai`, `gemini`, `groq`, `zhipu` | No fetcher — returns an empty list (no static catalog). |

> [!NOTE]
> For the empty-catalog providers, `/model`'s picker has nothing to enumerate; set the model id directly with `/model <id>`.

## Related

- [Sandbox & exec approvals](./sandbox-and-approvals.md)
- [Codex runtime](../features/codex-runtime.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Environment variables](../reference/environment-variables.md)
- [Setup wizard](../getting-started/setup-wizard.md)
