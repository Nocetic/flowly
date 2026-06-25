---
title: Setup wizard
eyebrow: Getting Started
description: flowly setup runs Flowly's first-run onboarding. Running it bare opens a picker — sign in with a Flowly account or enter your own API key — which is the one mandatory step; everything else is optional and can be added later.
---

```bash
flowly setup
```

This opens the first-run picker: **sign in with a Flowly account** (managed, nothing else to configure) or **enter your own API key**, then it offers to start the gateway. On a fresh install it also opens automatically right after the install script finishes.

## Subcommands

`flowly setup` is a sub-app — each area has its own subcommand that jumps straight to the relevant modal (or runs headless, for `byok`).

| Subcommand | What it does |
|---|---|
| `flowly setup` | Opens the first-run picker: a Flowly account or your own API key (the one mandatory step) |
| `flowly setup channels` | Opens the TUI channels modal (Telegram, Discord, Slack, …) |
| `flowly setup tools` | Opens the TUI integrations modal |
| `flowly setup byok <slug> [--key K] [--no-set-active]` | Headless: store an API key for a provider, no TUI |
| `flowly setup agents` | Configure multi-agent settings |
| `flowly setup google-workspace` | Configure Google Workspace integration |

## The mandatory step: account or API key

The agent can't run without a way to reach an LLM. Run `flowly setup` and either sign in with a Flowly account (managed) or enter your own API key — or set a key directly with the `byok` one-shot below. See [Providers and models](../using-flowly/providers-and-models.md) for the full provider list and model selection.

## BYOK one-shot

To store an API key without opening the picker — handy for scripts and CI:

```bash
flowly setup byok <slug> --key <k>
```

Valid provider slugs: `openrouter`, `anthropic`, `openai`, `xai`, `gemini`, `groq`, `zhipu`, `sakana`.

```bash
flowly setup byok sakana --key <k>
```

The key is pinned as the active default automatically. To store a key **without** switching the active provider, pass `--no-set-active`:

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
