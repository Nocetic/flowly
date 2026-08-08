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
The launcher is on PATH in your shell *profile*, but the shell you ran the
installer from hasn't re-read it — most common right after a `curl … | bash`
install, which runs in a child shell. Activate it in the current shell with the
line the installer prints:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Or just open a new terminal — the [install script](/docs/getting-started/installation)
already updated your shell profile, so fresh shells pick it up.

**The bot doesn't start on boot (Linux).**
systemd user services need *linger* enabled to run without an active login:
`loginctl enable-linger $USER`. `flowly doctor` checks this.

**`flowly service restart` says ok, but the gateway never comes back.**
The classic cause: the service unit was installed by a *previous* install
(e.g. the old PyPI package) and still points at a binary that a later install
retired — the service manager keeps reporting success while nothing ever binds
the port. One command rewrites the unit onto the current install and starts it:

```bash
flowly service install --start
```

Current releases close this loophole from both ends: the install script adopts
a unit that dangles or belongs to the install it is replacing (one pointing at
a *different, working* install is deliberately left alone), and `service
restart`/`start` detect the stale executable and print exactly this fix instead
of a false ✓.

**"Gateway not reachable" when I run `flowly`.**
`flowly` starts the gateway itself when none is running, so seeing this means
the start was refused or failed — and the line underneath says which. Common
reasons: you passed a `--host` that names another machine (nothing here to
start — check that machine and the firewall), you passed a one-off `--port`
that isn't the configured one, you're not on an interactive terminal, or
`FLOWLY_NO_GATEWAY_AUTOSTART=1` is set. If the service manager itself refused,
the last line of its error is shown; `flowly service logs --no-follow` has the
rest.

**Port already in use / "gateway already running".**
Something is already listening on the gateway port (default `18790`). If that
something is *your own* background service, `flowly gateway` handles it for you:
it asks, stops the service, serves the port itself, and restarts the service when
you exit. Anything else — a foreground gateway you started elsewhere, another
app, the one Flowly Desktop manages — is never touched; `flowly service status`
names it, and you stop it yourself. Flowly force-kills nothing it didn't start,
so two installs can't fight over the port silently.

**macOS asks for a keychain, or says "a keychain cannot be found".**
Expected on macOS, and nothing is wrong with your Mac. The CLI runs inside
Flowly's sandbox, which hides `~/Library/Keychains` from the agent on purpose,
so credentials are written to `0600` files under `~/.flowly/credentials/`
instead. Current versions skip the keychain outright while sandboxed, so the
panel no longer appears; if you saw it once on an older version, the fallback
already saved your tokens correctly. `flowly keychain status` shows where
credentials live, and `flowly keychain retry` clears the "keychain unavailable"
marker if you want Flowly to try again (for instance when running with
`FLOWLY_SANDBOX=0`).

**Setup said the provider didn't answer.**
`flowly setup` sends one small request before claiming success, so this is the
provider talking, not Flowly. The line tells you which case it is — a rejected
key (paste it again; setup echoes the last four characters so you can check it
arrived whole), no credit left, a plan that doesn't cover the model, or an
unreachable network. Your configuration is saved either way; rerun `flowly
setup` or `flowly doctor` after fixing it.

### Windows-specific

**Do I need an administrator shell for the service?**
No. `flowly service install` tries Task Scheduler first and, if that's denied,
automatically falls back to a Startup-folder launcher that runs the gateway at
logon — no elevation required either way.

**A `~lowly-ai` folder appears / "flowly" isn't recognized (legacy pip install).**
Specific to the old PyPI-era `pip install --user flowly-ai` installs: an
interrupted `pip` upgrade could leave a partial `~`-prefixed folder in your
user site-packages. Those installs no longer receive updates — clean up the
leftover and migrate to the current install, which never touches site-packages:

```powershell
$sp = python -m site --user-site
Remove-Item (Join-Path $sp "~lowly*") -Recurse -Force -ErrorAction SilentlyContinue
irm https://useflowlyapp.com/install.ps1 | iex
```

**A `UnicodeEncodeError` / `cp1252` traceback on Windows.**
Flowly's `✦` logo (and other Unicode) can't encode on a non-UTF-8 Windows
console — a redirected/piped stream, or certain locales. Fixed in current
releases: **every** `flowly` command now forces UTF-8 output (not just the
gateway). If you hit it on an older build, `flowly update` to the latest.

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
