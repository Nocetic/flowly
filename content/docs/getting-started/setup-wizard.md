---
title: Setup wizard
eyebrow: Getting Started
description: flowly setup runs Flowly's first-run onboarding. Running it bare opens a picker — sign in with a Flowly account or enter your own API key — which is the one mandatory step; everything else is optional and can be added later.
---

```bash
flowly setup
```

On a fresh install this opens automatically right after the install script finishes; run it yourself any time to change how Flowly is configured.

## What the first run asks

**One question, then you're chatting.** Setup seeds your workspace and asks how to power Flowly:

| Path | What it does |
|---|---|
| **Quick** | Signs in with a Flowly account — no API key, no billing setup, and no model to choose. The fastest way to a working agent. |
| **Full** | Opens the complete provider list (bring your own key), then walks through channels, integrations and media generation. |
| **Blank** | Pick a provider and stop — everything else stays off until you ask for it. |

Quick doesn't show the provider list on purpose: on a first run there's nothing yet to base that choice on, and the account needs no key. Anything else is one command away later (`flowly setup`, `/provider`, `/model`).

**If you're already signed in to another tool**, setup says so before doing anything with it:

```
Found a ChatGPT subscription login from the Codex CLI (~/.codex/auth.json).

? How do you want to power Flowly?
› Sign in with Flowly      (recommended — hosted, nothing to configure)
  Use the ChatGPT subscription found on this machine
  Something else  ·  bring your own API key
```

Flowly can read the Codex CLI's login (and OpenCode's, for the GLM Coding Plan) so an existing subscription works with no extra steps — but it never adopts one silently. Whatever you answer is written to your config, so you're only asked once.

**Setup verifies the provider before it claims success.** After a provider is configured, Flowly sends one small request and reports what came back:

```
  Checking your Flowly account…
  ✓ Flowly account answered (2.8s) · anthropic/claude-haiku-4.5
```

If it doesn't answer, you get the reason immediately — a rejected key, an empty balance, an unreachable network — instead of discovering it in your first conversation. The configuration is still saved; fix it with `flowly setup` or `flowly doctor`.

Setup finishes by offering to keep the gateway running in the background. Declining is fine: bare `flowly` starts one when you chat.

## Subcommands

`flowly setup` is a sub-app — each area has its own subcommand that jumps straight to the relevant modal (or runs headless, for `byok`).

| Subcommand | What it does |
|---|---|
| `flowly setup` | The first-run flow above: workspace, provider, and an offer to run the gateway |
| `flowly setup channels` | Connect messaging channels (Telegram, Discord, Slack, …) |
| `flowly setup tools` | Configure tool integrations (browser, voice, Trello, …) |
| `flowly setup byok <slug> [--key K] [--no-set-active]` | Headless: store an API key for a provider, no UI |
| `flowly setup agents` | Set up multi-agent orchestration |
| `flowly setup google-workspace` | Install and authenticate the Google Workspace CLI (`gws`) |

## The mandatory step: account or API key

The agent can't run without a way to reach an LLM. Run `flowly setup` and either sign in with a Flowly account (managed) or enter your own API key — or set a key directly with the `byok` one-shot below.

Model choice is **not** part of the first run: each provider comes with a sensible default, and you can change it any time with `/model` in the chat or by picking **Full** in setup. See [Providers and models](../using-flowly/providers-and-models.md) for the full provider list and model selection.

## BYOK one-shot

To store an API key without opening the picker — handy for scripts and CI:

```bash
flowly setup byok <slug> --key <k>
```

Valid provider slugs: `openrouter`, `anthropic`, `openai`, `xai`, `gemini`, `groq`, `zhipu`, `sakana`.

```bash
flowly setup byok sakana --key <k>
```

The key is pinned as the active default automatically, and setup echoes back its last four characters (`…3f9a`) so you can confirm the paste arrived whole. To store a key **without** switching the active provider, pass `--no-set-active`:

```bash
flowly setup byok openrouter --key sk-or-... --no-set-active
```

> [!IMPORTANT]
> Keys are written to `~/.flowly/config.json`, which is stored with owner-only (`0600`) permissions because it holds secrets. See [Configuration](../using-flowly/configuration.md).

## Adding channels later

You don't have to configure channels during first setup. Add a Telegram bot, Discord, Slack, or other channel any time with `flowly setup channels` (or the matching TUI modal). The gateway auto-restarts to pick up newly enabled channels. See [Channels overview](../channels/overview.md).

## Related

- [Quickstart](./quickstart.md)
- [Installation](./installation.md)
- [Providers and models](../using-flowly/providers-and-models.md)
- [Configuration](../using-flowly/configuration.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
