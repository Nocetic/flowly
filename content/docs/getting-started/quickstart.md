---
title: Quickstart
eyebrow: Getting Started
description: The fastest path from nothing to a working agent — install, pick a provider, open the chat — in under two minutes.
---

## 1. Install

```bash
curl -fsSL https://useflowlyapp.com/install.sh | bash   # macOS / Linux
# Windows: irm https://useflowlyapp.com/install.ps1 | iex
```

The script manages Python for you (via uv) and installs Flowly as a git checkout, so `flowly update` pulls new versions between releases. Works on macOS, Linux, and Windows. See [Installation](./installation.md) for details.

## 2. Pick a provider

```bash
flowly setup
```

`flowly setup` asks how to power Flowly. **Quick** signs you in with a Flowly account — no API key, no billing setup, no model to choose. **Full** opens the complete provider list if you'd rather bring your own key (OpenRouter, Anthropic, OpenAI, Gemini, Groq, xAI, Zhipu, Sakana, a ChatGPT or Grok subscription, or a self-hosted vLLM endpoint) and continues into channels and integrations. This is the only mandatory step — everything else is optional and can be added later.

Setup then sends one small request to check the provider actually answers, so a mistyped key or an empty balance surfaces here rather than in your first conversation. On a fresh install it opens automatically right after `curl … | bash`.

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

Bare `flowly` opens the terminal chat. Type a message and the agent responds. If the gateway (the background process that actually runs your agent) isn't up, `flowly` starts it for you — there is no separate "start the server" step.

Inside the chat you can switch provider and model on the fly:

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

- Keep Flowly running across logins and reboots: [Service](../using-flowly/service.md) — `flowly service install --start` (optional; `flowly` already starts the gateway on demand)
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
