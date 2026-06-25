<div align="center">
  <img src="https://raw.githubusercontent.com/Nocetic/flowly/main/assets/banner.png" alt="Flowly — one brain, everywhere you work" width="100%">
  <p>
    <a href="https://pypi.org/project/flowly-ai/"><img src="https://img.shields.io/pypi/v/flowly-ai?style=for-the-badge&label=pypi&color=7C5CFC" alt="PyPI"></a>
    <img src="https://img.shields.io/badge/python-%E2%89%A53.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-14181F?style=for-the-badge" alt="Platform">
    <a href="https://github.com/Nocetic/flowly/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-3B82F6?style=for-the-badge" alt="License"></a>
  </p>
</div>

**Flowly is an open-source AI agent that runs on _your_ machine, meets you on every channel you already use, and gets better the longer you use it.** One shared memory, one library of skills, your own LLM keys. It remembers across conversations, maintains and improves itself over time, schedules its own work, and connects to anything that speaks [MCP](https://modelcontextprotocol.io) — from a $5 VPS, a Mac mini, or the laptop in front of you.

## Install

```bash
# One command sets up uv, Python, and Flowly
curl -fsSL https://useflowlyapp.com/install.sh | bash

# Already manage tools with uv?
uv tool install flowly-ai

flowly setup     # pick an LLM provider, add any channels
flowly           # open the terminal UI
```

## What's inside

- **One agent, every channel** — Terminal TUI · Telegram · Discord · Slack · Microsoft Teams · WhatsApp · iMessage · Email · voice. A single gateway process speaks to all of them, with one conversation memory shared across every surface.
- **Bring your own key** — OpenRouter, Anthropic, OpenAI, Google Gemini, Groq, xAI/Grok, Zhipu/GLM, and any OpenAI-compatible local model (Ollama, LM Studio, vLLM). When nothing is pinned, Flowly cascades through whatever you've configured so it always has a working model. Switch live with `/provider` and `/model`. Sign in to xAI with your SuperGrok / X Premium+ subscription instead of an API key.
- **A closed learning loop** — every fact becomes a governed memory with a calibrated trust score; a background pass merges duplicates and retires stale notes; structured facts land in a knowledge graph. Opt in and Flowly writes and refines its own skills — every change snapshotted and reversible.
- **135 built-in skills** plus skill bundles and drop-in Markdown skills, compatible with the open [agentskills.io](https://agentskills.io) standard.
- **MCP, both directions** — connect any MCP server (`flowly mcp install …`) *and* run Flowly itself as an MCP server (`flowly mcp serve`) for Claude Desktop, Cursor, or Claude Code.
- **Delegates and parallelizes** — spawn isolated sub-agents, run a cross-channel task board sequentially or in parallel, and hand heavy coding off to a local Codex session (opt-in).
- **Scheduled & unattended** — built-in cron schedules any natural-language prompt to any channel, and the gateway runs as a background service that survives reboots.
- **Yours to extend & contain** — full Python plugins (tools, slash commands, channels, lifecycle hooks), switchable personas, and an OS sandbox (`sandbox-exec` on macOS, `bubblewrap` on Linux).

## Self-host or cloud

Flowly's agent core is **Apache 2.0** — self-host on your laptop, a VPS, or a Mac mini with your own LLM keys, no account required. Optional [Flowly Cloud](https://useflowlyapp.com) adds native Mac/iOS/Android apps, cross-device sync, hosted LLM access, and a managed relay that keeps your bot reachable when your laptop sleeps.

## Links

- **Documentation** — https://useflowlyapp.com/docs
- **Source & issues** — https://github.com/Nocetic/flowly
- **Website & apps** — https://useflowlyapp.com

---

Apache 2.0. Fork it, ship it, embed it. Self-hosted use with your own LLM keys is unrestricted.
