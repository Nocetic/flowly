---
title: Troubleshooting
eyebrow: Using Flowly
description: When something's off, start with `flowly doctor` — it runs a battery of health checks across config, providers, the gateway, the service, channels, and the data stores, and can auto-fix the routine ones.
---

## Start here

```bash
flowly doctor          # diagnose
flowly doctor --fix    # diagnose, and auto-repair the fixable issues
```

`doctor` walks ~20 checks and prints each as **ok**, **warn**, or **error**, with
a hint for anything that needs attention. The fixable ones (config formatting,
workspace scaffolding, etc.) are repaired in place when you pass `--fix`.

## What it checks

| Area | Checks |
| --- | --- |
| **State & config** | `~/.flowly` directory, `config.json` exists + parses, schema validity, duplicate keys, unknown keys |
| **Provider** | At least one API key present, a usable model selected, provider-config corruption |
| **Workspace** | Context files and memory scaffolding present |
| **Gateway** | Gateway running / reachable, gateway-token security |
| **Service** | launchd / systemd / Task Scheduler install present and the executable resolves; Linux user-linger for boot persistence |
| **Account** | Flowly Cloud tokens valid, relay health |
| **Channels** | Channel config sanity |
| **Data stores** | Memory system and session store integrity |

## Common issues

**"No provider key" / the agent won't answer.**
You haven't configured an LLM provider. Run `flowly setup` → pick a provider →
paste a key (or sign in to Flowly Cloud). `flowly doctor` flags this.

**Config won't load / duplicate keys.**
`config.json` keys are **camelCase**. A classic foot-gun is having both `apiKey`
and `api_key` — Flowly converts both to the same internal key and the last one
wins. `flowly doctor --fix` reports and helps clean these.

**Gateway changes don't take effect.**
Channel tokens, plugin enable/disable, and similar need a gateway bounce:
`flowly restart`. (Provider/model swaps hot-reload via slash commands and don't
need a restart.)

**"flowly: command not found" after install.**
The launcher isn't on PATH yet — open a new terminal, or add the install dir to
PATH. The [install script](/docs/getting-started/installation) writes this for
you; a new shell picks it up.

**The bot doesn't start on boot (Linux).**
systemd user services need *linger* enabled to run without an active login:
`loginctl enable-linger $USER`. `flowly doctor` checks this.

**Port already in use / "gateway already running".**
Something is already listening on the gateway port (default `18790`) — usually a
foreground `flowly gateway` or a duplicate of the service. `flowly service status`
shows a diagnostic and warns when a gateway is running **outside** the service.
Stop the stray process (or `flowly service stop`) before starting again. Flowly
never force-kills a gateway it didn't start — including the one Flowly Desktop
manages — so two installs won't fight over the port silently.

### Windows-specific

**Do I need an administrator shell for the service?**
No. `flowly service install` tries Task Scheduler first and, if that's denied,
automatically falls back to a Startup-folder launcher that runs the gateway at
logon — no elevation required either way.

**"flowly" isn't recognized after an update, or a `~lowly-ai` folder appears.**
An interrupted `pip` upgrade — or one that ran while `flowly.exe` was locked —
can leave a partial `~`-prefixed folder in your user site-packages. `flowly
update` now relaunches itself on Windows to avoid the locked-exe case, but if you
land in a half-broken state, delete the leftover and reinstall:

```powershell
$sp = python -m site --user-site
Remove-Item (Join-Path $sp "~lowly*") -Recurse -Force -ErrorAction SilentlyContinue
pip install --user --force-reinstall flowly-ai
```

**Console glyphs crash the gateway on older Windows.**
Fixed in current releases (the service forces UTF-8 output). If you're on an old
build and see a `UnicodeEncodeError` / `cp1252` traceback, `flowly update` to the
latest.

**Channel silent / not receiving.**
Confirm the channel is `enabled` in config and that access control
(`allowFrom` / pairing) permits the sender. See the channel's own page under
[Channels](/docs/channels/overview).

## Still stuck?

- Check the logs: `flowly service logs` (service mode) or the terminal running
  `flowly gateway`.
- Verify your install mode and version: `flowly --version`, then
  [`flowly update --check`](/docs/getting-started/updating).
- Inspect what's on disk: see the [file layout](/docs/reference/file-layout).
