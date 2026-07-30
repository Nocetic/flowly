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
Something is already listening on the gateway port (default `18790`) — usually a
foreground `flowly gateway` or a duplicate of the service. `flowly service status`
shows a diagnostic and warns when a gateway is running **outside** the service.
Stop the stray process (or `flowly service stop`) before starting again. Flowly
never force-kills a gateway it didn't start — including the one Flowly Desktop
manages — so two installs won't fight over the port silently.

### Buzz-specific

Start with:

```bash
flowly doctor
flowly service logs
```

When Buzz is enabled, Doctor checks the relay URL, private-key sources, CLI
resolution, and whether the sender policy permits anyone.

**`buzz` CLI not found.**
The executable must be installed on the machine and under the OS account that
runs the gateway. A service may not inherit the same `PATH` as your shell. Run
`command -v buzz` as that account, then set an absolute
`channels.buzz.cliPath` / `BUZZ_CLI_PATH` or install it at `~/bin/buzz`.

**Desktop cannot test the identity or load channels.**
Desktop runs discovery on the agent host through a read-only RPC. Check the
relay URL, confirm the private key is an `nsec` or 64-character hex value, and
confirm that identity already belongs to the community. On a remote
installation, having the CLI and key only on the Desktop computer is
insufficient.

**Buzz is connected but never responds.**
The secure default `allowAllUsers=false` plus `allowFrom=[]` denies everyone.
Add the sender's Nostr `npub` or hex public key, or explicitly allow every
member. Pairing approval does not apply to Buzz. In a channel,
`groupPolicy="mention"` also requires the agent identity to be addressed;
direct messages do not require a mention.

**A joined channel is not being watched.**
An empty `channels` list loads all joined group channels when the adapter
starts; restart after joining a new group channel. A non-empty list is a fixed
subset of channel IDs. Verify that the identity is joined and remove a stale
explicit list or add the missing channel ID. Desktop's reload action refreshes
the named picker before you save.

**The identity has DMs but the adapter reports no channels to watch.**
The current startup path requires at least one joined group channel before it
discovers DM conversations. Join the identity to a group channel, reload
Desktop discovery, and restart the gateway.

**Messages sent before restart do not appear.**
This is intentional. Startup fetches recent messages only to seed the
watermark and de-duplication cache; it does not replay history. Send a new
message after the adapter is ready.

**WebSocket authentication/reconnect errors.**
Use `transport="auto"` so CLI polling keeps receiving messages while the
authenticated Nostr WebSocket retries. Use `poll` if WebSockets are
intentionally blocked. A forced `websocket` transport has no polling fallback.
If the relay requires owner attestation, configure `authTag` or
`BUZZ_AUTH_TAG`. Use `BUZZ_AUTH_TAG` in the active profile `.env` when the
official CLI's identity, discovery, or send calls also require the attestation;
the config-only `authTag` field covers Flowly's WebSocket path.

**"Buzz identity is already active in another Flowly profile."**
Another process is using the same relay and public key. Stop the duplicate
gateway instead of deleting an active lock. Buzz identity locks live under
the canonical `~/.flowly/locks/buzz/` directory, even for named profiles or a
custom `FLOWLY_HOME`.

**The key or connection settings changed but runtime behavior did not.**
Restart the gateway. Also check for exported `BUZZ_*` variables: environment
overrides take precedence over `channels.buzz`, so Desktop can correctly show
the saved config while the running process uses a different value.

The full decision tables and resolution order are in the
[Buzz channel guide](/docs/channels/buzz).

### Windows-specific

**Do I need an administrator shell for the service?**
No. `flowly service install` tries Task Scheduler first and, if that's denied,
automatically falls back to a Startup-folder launcher that runs the gateway at
logon — no elevation required either way.

**A `~lowly-ai` folder appears / "flowly" isn't recognized (packaged pip install).**
Specific to a `pip install --user flowly-ai` install: an interrupted `pip`
upgrade — or one that ran while `flowly.exe` was locked — can leave a partial
`~`-prefixed folder in your user site-packages. `flowly update` now relaunches
itself on Windows to avoid the locked-exe case, but if you land in a half-broken
state, delete the leftover and reinstall:

```powershell
$sp = python -m site --user-site
Remove-Item (Join-Path $sp "~lowly*") -Recurse -Force -ErrorAction SilentlyContinue
pip install --user --force-reinstall flowly-ai
```

A git-checkout install — the default from the install script — never touches
site-packages, so this can't happen to it; re-run the installer or `flowly
update` to repair one.

**A `UnicodeEncodeError` / `cp1252` traceback on Windows.**
Flowly's `✦` logo (and other Unicode) can't encode on a non-UTF-8 Windows
console — a redirected/piped stream, or certain locales. Fixed in current
releases: **every** `flowly` command now forces UTF-8 output (not just the
gateway). If you hit it on an older build, `flowly update` to the latest.

**Channel silent / not receiving.**
Confirm the channel is `enabled` in config and that access control
(`allowFrom` / pairing) permits the sender. Buzz is default-deny and does not
use pairing codes. See the channel's own page under
[Channels](/docs/channels/overview).

## Still stuck?

- Check the logs: `flowly service logs` (service mode) or the terminal running
  `flowly gateway`.
- Verify your install mode and version: `flowly --version`, then
  [`flowly update --check`](/docs/getting-started/updating).
- Inspect what's on disk: see the [file layout](/docs/reference/file-layout).
