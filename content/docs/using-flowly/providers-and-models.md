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
| `openai_codex` | OAuth (`flowly codex login`) | `https://chatgpt.com/backend-api/codex` |
| `xai` | BYOK `api_key` | `https://api.x.ai/v1` |
| `xai_oauth` | OAuth (`flowly xai login`) | `https://api.x.ai/v1` |
| `gemini` | BYOK `api_key` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `groq` | BYOK `api_key` | `https://api.groq.com/openai/v1` |
| `zhipu` | BYOK `api_key` | `https://open.bigmodel.cn/api/paas/v4` |
| `sakana` | BYOK `api_key` | `https://api.sakana.ai/v1` (Fugu / Fugu Ultra, OpenAI-compat) |
| `vllm` | BYOK `api_key` | none (self-hosted — you must set `apiBase`) |

The API bases are built in; you normally only supply a key. All listed endpoints (except `xai_oauth` and `openai_codex`) speak the OpenAI Chat-Completions wire protocol. `xai_oauth` and `openai_codex` both use a Responses-API wire format instead.

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

## ChatGPT subscription (Codex OAuth)

For a ChatGPT Plus / Pro / Team plan, authenticate with OpenAI's Codex "Sign in with ChatGPT" OAuth (PKCE) flow — no API key, usage is billed against your plan:

```bash
flowly codex login          # sets active provider + default model (gpt-5.5)
flowly codex login --device # headless / no-browser: prints a code to enter at auth.openai.com/codex/device
flowly codex status         # shows both codex_session tool AND ChatGPT subscription state
flowly codex logout
```

Tokens are stored in the keychain or `~/.flowly/credentials/openai_codex.json` (mode `0600`), not in `config.json`. The client id is fixed (the public Codex CLI client — OpenAI has no self-service client registration for this OAuth scope).

> [!TIP]
> If you've already run `codex login` for the [Codex runtime](../features/codex-runtime.md) tool, Flowly picks up `~/.codex/auth.json` automatically as a fallback — no separate sign-in needed. Flowly's own store (if you've run `flowly codex login`) always takes priority when both exist, and Flowly writes refreshed tokens back to `~/.codex/auth.json` too, so the Codex CLI keeps working.

> [!NOTE]
> The `openai_codex` **provider** (this section) and the `codex_session` **tool** ([Codex runtime](../features/codex-runtime.md)) are unrelated features that happen to share the "Codex" name and the `flowly codex` CLI namespace. The provider makes Flowly's *own* agent loop run on GPT-5.x via your ChatGPT plan. The tool *delegates* a coding turn to a separate `codex app-server` subprocess. You can use either, both, or neither.

> [!NOTE]
> The ChatGPT Codex backend only serves current general-purpose GPT-5.x models (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`) — Codex-suffixed model ids and older versions are rejected. See [Environment variables](../reference/environment-variables.md) to override the default model or the system instructions sent as `instructions`.

## Switching providers and models at runtime

The active provider is resolved in this priority order:

1. `providers.active`, if that provider is currently usable (sticky; falls through if not).
2. `flowly` hosted, if enabled and signed in.
3. The BYOK cascade — first usable of `openrouter`, `anthropic`, `openai`, `openai_codex`, `xai`, `xai_oauth`, `gemini`, `groq`, `zhipu`, `sakana`, `vllm`.

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
- **`xai_oauth` and `openai_codex` have no rotator** — each does a single token refresh and one retry on HTTP 401. `openai_codex` treats a 403 as a plan-entitlement error (not authenticated to use Codex) and doesn't retry it.
- **Flowly hosted does not use fallback keys** — it uses a single refreshable account token.

## Prompt caching

> [!NOTE]
> Prompt caching is **Anthropic / Claude only** — it is applied solely to models whose id contains `claude`. Other providers and models are unaffected (the xAI OAuth and ChatGPT subscription providers never cache this way).

- **Strategy:** up to 4 cache breakpoints — one on the system prompt plus up to 3 on the most recent non-system, non-tool messages.
- **TTL:** default **`1h`**. Supported values are `5m` and `1h`. Override at process start:

  ```bash
  FLOWLY_CLAUDE_CACHE_TTL=5m flowly ...
  ```

  Invalid values fall back to `1h`.

## Model catalog (live vs empty)

Flowly builds the model picker from a live catalog — each provider's own `/models` endpoint where it has one, or the [models.dev](https://models.dev) community catalogue otherwise (cached locally, served stale on network failure). Providers differ in whether a catalog is available:

| Provider | Catalog source |
| --- | --- |
| `openrouter` | Live `GET /models` (public, filtered to tool-capable models, free-first). |
| `flowly` | Live `GET {base}/models` (plan-filtered with `allowed`/`locked` tags; degrades to OpenRouter on no-account/network/401). |
| `xai` | Live `GET /v1/models` with your BYOK key. |
| `xai_oauth` | Live `GET /v1/models` with the OAuth token. |
| `openai_codex` | Static curated list (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`) — the backend has no `/models` endpoint; access is plan-gated, not catalogued. Only shown once signed in. |
| `anthropic`, `openai`, `gemini`, `groq`, `zhipu` | The [models.dev](https://models.dev) community catalogue — cached locally, filtered to tool-capable models (no per-provider fetcher needed). |
| `sakana`, `vllm` | No catalog — set the model id directly. |

> [!NOTE]
> Only `sakana` and `vllm` have nothing for the `/model` picker to enumerate; for those, set the model id directly with `/model <id>`.

## Related

- [Sandbox & exec approvals](./sandbox-and-approvals.md)
- [Codex runtime](../features/codex-runtime.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Environment variables](../reference/environment-variables.md)
- [Setup wizard](../getting-started/setup-wizard.md)
