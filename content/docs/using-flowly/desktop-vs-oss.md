---
title: Open source vs. Desktop & Cloud
eyebrow: Using Flowly
description: Flowly is open-core — the agent (brain, tools, skills, channels, gateway) is open source under Apache 2.0, and the Desktop app embeds this exact compiled core. This page draws the line between what's open and what's a closed convenience.
---

Flowly is **open-core**. The agent — its brain, tools, skills, channels, and gateway — is open source under Apache 2.0 and lives in this repo. The native apps and the hosted infrastructure around it are separate, closed components.

The key thing to understand: **the Desktop app embeds this exact open-source core** (compiled), then wraps it in a native UI and optional cloud services. There is no separate "lite" build — the agent you self-host is the agent that ships inside the app.

## What's in this repo (open source, Apache 2.0)

Everything the agent needs to run on your own machine, with your own keys, with no account:

- **Agent core** — the loop, tool dispatch, sub-agents, planning, streaming
- **40+ tools** — files, shell (sandboxed), web, computer-use, documents, media
- **135 skills** + skill bundles + drop-in Markdown skills
- **All channel adapters** — Telegram, Discord, Slack, Teams, WhatsApp, iMessage, email, voice
- **BYOK providers** — Anthropic, OpenAI, OpenRouter, Gemini, Groq, xAI/Grok, Zhipu, and OpenAI-compatible local models
- **Gateway** — the local WebSocket daemon every client connects to
- **Self-maintaining memory** + knowledge graph, **board**, **cron**, **MCP** (both directions), **plugins**, **sandbox**
- **Terminal UI** — the full `flowly` TUI

If you self-host, you get all of the above and never need to sign in.

## What's closed (Desktop & Cloud)

These are *not* in this repo and are not open source:

- **Native apps** — the Mac, iOS, and Android apps and the browser extension (the GUI shells that embed the compiled core)
- **Hosted LLM access** — use Flowly's models without bringing your own key
- **Managed relay** — keeps your bot reachable when your laptop sleeps, without exposing a port yourself
- **Cross-device sync** — conversations and settings across your devices
- **Account backend** — the OAuth/identity service behind `flowly login`

These are opt-in conveniences. The agent never depends on them.

## At a glance

| Capability | Open source (this repo) | Desktop / Cloud |
|---|:---:|:---:|
| Agent core, tools, skills | ✅ | ✅ *(same compiled core)* |
| All messaging channels | ✅ | ✅ |
| BYOK LLM providers | ✅ | ✅ |
| Terminal UI (TUI) | ✅ | — *(uses the native GUI)* |
| Memory, board, cron, MCP, plugins | ✅ | ✅ |
| Run on your own machine, no account | ✅ | ✅ |
| Native Mac / iOS / Android apps | — | ✅ closed |
| Browser extension | — | ✅ closed |
| Hosted LLM (no key required) | — | ✅ |
| Managed relay (reach it while your laptop sleeps) | — | ✅ |
| Cross-device sync | — | ✅ |

`flowly login` is optional and only wires up the cloud features above. Without it, everything in this repo still works.

## What this means for contributors

- **This repo** is where CLI, gateway, agent, tools, skills, providers, and channel work happens. PRs here are welcome — see [`CONTRIBUTING.md`](https://github.com/Nocetic/flowly/blob/main/CONTRIBUTING.md).
- The **native apps and the hosted relay are closed** and developed separately; we don't take PRs for them here. Bugs or requests for the apps go through their own support channels at [useflowlyapp.com](https://useflowlyapp.com).
- Because the Desktop app embeds the compiled core from this repo, **fixing an agent bug here fixes it everywhere.**

## Related

- [Self-hosting](self-hosting.md)
- [Flowly Cloud](flowly-cloud.md)
