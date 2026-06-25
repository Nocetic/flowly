---
title: Quickstart
eyebrow: Getting Started
description: The fastest path from nothing to a working agent — install, pick a provider, open the chat — in under two minutes.
---

## 1. Install

```bash
uv tool install flowly-ai           # recommended
# or: pip install --user flowly-ai
# or: curl -fsSL https://useflowlyapp.com/install.sh | bash
```

Requires **Python ≥ 3.11** on macOS, Linux, or Windows. See [Installation](./installation.md) for all methods.

## 2. Pick a provider

```bash
flowly setup
```

`flowly setup` opens the first-run picker. Choose how to power Flowly: **sign in with a Flowly account** (managed, nothing else to configure) or **bring your own API key** (OpenRouter, Anthropic, OpenAI, Gemini, Groq, xAI, Sakana — plus Zhipu and a self-hosted vLLM endpoint via the advanced picker). Configuring one of these is the only mandatory step — everything else (channels, tools, integrations) is optional. On a fresh install this picker also opens automatically right after `curl … | bash`.

> [!TIP]
> If you already have a key and want to skip the picker, do it in one shot:
>
> ```bash
> flowly setup byok anthropic --key sk-ant-...
> ```

See [Setup wizard](./setup-wizard.md) for all subcommands.

## 3. Chat

```bash
flowly
```

Bare `flowly` opens the terminal chat. Type a message and the agent responds. Inside the chat you can switch provider and model on the fly:

```bash
/provider openrouter
/model claude-sonnet-4-5
```

See [Terminal UI](../using-flowly/tui.md) for slash commands and session history.

## One-shot prompts

For scripting or a single question without entering the TUI, use `flowly agent`:

```bash
flowly agent -m "Summarize the README in this directory"
```

## Where to next

- Run Flowly in the background so it stays reachable: [Service](../using-flowly/service.md) — `flowly service install --start`
- Add a Telegram, Discord, or Slack bot: [Channels overview](../channels/overview.md)
- Tune models, tools, and behavior: [Configuration](../using-flowly/configuration.md)
- Control shell execution and approvals: [Sandbox and approvals](../using-flowly/sandbox-and-approvals.md)

## Related

- [Installation](./installation.md)
- [Setup wizard](./setup-wizard.md)
- [Terminal UI](../using-flowly/tui.md)
- [Providers and models](../using-flowly/providers-and-models.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
