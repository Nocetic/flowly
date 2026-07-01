---
title: Self-hosting
eyebrow: Using Flowly
description: Run Flowly entirely on your own machine with your own LLM keys — a laptop, a Mac mini, or a headless VPS. Nothing here needs a Flowly account; this is the end-to-end walkthrough from install to a hardened background service.
---

Flowly runs entirely on your own machine with your own LLM keys. **Nothing in this repo needs a Flowly account.** This guide covers running it on a laptop, a Mac mini, or a headless VPS, and links to the deeper pages for each step.

For what's open-source vs. cloud-only, see [Open source vs. Desktop & Cloud](desktop-vs-oss.md).

## 1. Install

```bash
# One command — sets up uv, Python, git, and a Flowly git checkout that
# `flowly update` keeps current with `git pull` (no waiting on a PyPI release)
curl -fsSL https://useflowlyapp.com/install.sh | bash
# prefer a packaged PyPI install?  uv tool install flowly-ai
```

The checkout and its virtualenv live under `~/.local/share/flowly/` (`repo/` + `venv/`); everything Flowly *stores* lives under `~/.flowly/` (config, workspace, plugins, skills, memory, session db). See [Installation](../getting-started/installation.md) and [File layout](../reference/file-layout.md).

## 2. Bring your own key

Pick any provider — OpenRouter, Anthropic, OpenAI, Gemini, Groq, xAI/Grok, Zhipu, Kimi, or any OpenAI-compatible local model (Ollama, LM Studio, vLLM):

```bash
flowly setup byok openrouter --key sk-or-...
# or run the full wizard (provider + channels + tools)
flowly setup
```

This writes `~/.flowly/config.json`. Keys there are **camelCase**:

```json
{
  "providers": {
    "active": "openrouter",
    "openrouter": { "apiKey": "sk-or-..." }
  }
}
```

`providers.active` pins the default. Leave it `""` and Flowly cascades through whatever you've configured so it always has a working model. See [Providers & models](providers-and-models.md).

## 3. Run it

**Foreground (terminal):**

```bash
flowly                 # terminal UI
flowly gateway         # just the gateway daemon, no TUI
```

**As a background service** (survives reboots and terminal close):

```bash
flowly service install --start     # launchd (macOS) / systemd (Linux) / Task Scheduler (Windows)
flowly service status
flowly service logs                # tail the logs
flowly restart
flowly service uninstall
```

The gateway listens on `127.0.0.1:18790` by default — loopback only, reachable only from the same machine. See [Running as a service](service.md).

## 4. Headless / VPS

Run Flowly on a VPS or Mac mini and talk to it from Telegram, Discord, etc. — or from the desktop/mobile apps over the network.

```bash
# 1. Install + configure a provider (steps 1–2 above)
# 2. Initialize the workspace without prompts (good for scripts/cloud-init)
flowly bootstrap

# 3a. Local-only + a messaging channel (recommended, no open ports):
flowly setup channels          # add Telegram/Discord/Slack/…
flowly service install --start

# 3b. Expose the gateway to network clients (e.g. the desktop app):
flowly service install --start --host 0.0.0.0 --port 18790 --token "$(openssl rand -hex 24)"
```

> [!WARNING]
> If you bind to a non-loopback address, secure it:
>
> - Always set a `--token`; clients must present it to connect.
> - Prefer an **SSH tunnel** or a TLS reverse proxy over exposing `0.0.0.0` directly to the internet. Restrict the port with a firewall / security group.
> - The agent has shell and filesystem access on the host — **treat gateway access as host access.** See the repo's [`SECURITY.md`](https://github.com/Nocetic/flowly/blob/main/SECURITY.md).

## 5. Sandbox

Shell/exec tooling runs inside an OS sandbox by default: **`sandbox-exec` on macOS**, **`bwrap` (bubblewrap) on Linux**. On Linux, install bubblewrap so the sandbox is active:

```bash
sudo apt install bubblewrap      # Debian/Ubuntu;  dnf install bubblewrap on Fedora
```

It denies access to things like `~/.ssh`, `~/.aws`, and keychains while allowing `~/.flowly`, your home dir, and `/tmp`. You can disable it (not recommended) with:

```bash
FLOWLY_SANDBOX=0 flowly
```

Windows has no sandbox yet. Full details in [Sandbox & approvals](sandbox-and-approvals.md).

## 6. Profiles & isolation

Run multiple independent Flowly instances (e.g. a test bot and a real one) without sharing state:

```bash
flowly -p testbot setup        # named profile → ~/.flowly/profiles/testbot/
FLOWLY_HOME=/srv/flowly flowly gateway   # fully custom home dir
```

- `FLOWLY_HOME` — override the whole state directory (default `~/.flowly`).
- `FLOWLY_PROFILE` / `-p <name>` — select a named profile under `~/.flowly/`.
- `FLOWLY_SANDBOX=0` — disable the exec sandbox.

See [Profiles](profiles.md) and [Environment variables](../reference/environment-variables.md).

## 7. Health check

```bash
flowly doctor          # diagnose config + runtime; --fix to auto-repair
flowly status
```

If something's off, `flowly doctor` is the first stop — it checks provider keys, the gateway, the service definition, and workspace layout. See [Troubleshooting](troubleshooting.md).

## Related

- [Open source vs. Desktop & Cloud](desktop-vs-oss.md)
- [Running as a service](service.md) · [Profiles](profiles.md) · [Sandbox & approvals](sandbox-and-approvals.md)
- [Providers & models](providers-and-models.md) · [Configuration](configuration.md)
