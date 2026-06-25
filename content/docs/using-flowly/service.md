---
title: Running as a service
eyebrow: Using Flowly
description: The Flowly gateway can run as a background service so it stays up without a terminal session — surviving reboots and terminal close — keeping your channels reachable.
---

## Install and lifecycle

```bash
flowly service install --start   # install and start immediately
flowly service start
flowly service stop
flowly service restart
flowly service status
flowly service logs
flowly service uninstall
```

`install` registers the service; it starts the service after install by default (pass `--no-start` to skip). It is **idempotent** — re-running reinstalls cleanly, so `--force` is no longer required (the flag is kept only for back-compat). Flags on `install`: `--start`/`--no-start`, `--label`, `--port`, `--persona`, `--cwd`, plus the remote-access flags `--remote`, `--host`, and `--token` (see below). The default port is `18790`; `status` reports health by hitting `http://127.0.0.1:<port>/health` and warns if a gateway is running outside the service.

### Remote access

To make the background service reachable from your phone or another device, install it with remote access on:

```bash
flowly service install --start --remote
```

`--remote` is a plain-language alias for `--host 0.0.0.0` and ensures an access token automatically (use `--host <ip>` / `--token <t>` for explicit control). After installing, run `flowly enroll` to print the LAN IP, port, token, TLS note, and firewall steps for connecting a device. See [`flowly enroll`](../reference/cli-commands.md#flowly-enroll).

## Platform backends

Flowly uses each OS's native service manager. The service label is `ai.flowly.gateway`.

| Platform | Backend | Service file |
|---|---|---|
| macOS | launchd | `~/Library/LaunchAgents/ai.flowly.gateway.plist` |
| Linux | systemd (user unit) | `~/.config/systemd/user/ai.flowly.gateway.service` |
| Windows | Task Scheduler | `~/AppData/Local/flowly/ai.flowly.gateway.xml` |

Behavior per platform:

- **macOS (launchd):** runs at load and is kept alive, working directory `$HOME`. Loaded/unloaded via `launchctl`.
- **Linux (systemd user unit):** `Type=simple`, `Restart=always`. To survive logout it needs systemd **linger** enabled — `flowly doctor --fix` runs `loginctl enable-linger` for you.
- **Windows (Task Scheduler):** registered with `schtasks`, launched hidden (no console window). If Task Scheduler is denied because the shell isn't elevated, install **falls back to a Startup-folder launcher** that runs the gateway at logon — so an administrator shell is *not* required, it just changes which mechanism is used.

> [!IMPORTANT]
> On Linux, the service only survives logout when systemd linger is enabled (`flowly doctor --fix` can do this). On Windows, an elevated shell lets Flowly use Task Scheduler; without one it automatically uses the Startup folder instead — either way the gateway starts at logon.

## Logs

Gateway logs are written to:

- Windows: `~/AppData/Local/flowly/logs`
- macOS / Linux: `<FLOWLY_HOME>/logs`

The service writes `flowly-gateway.out.log` and `flowly-gateway.err.log` in that directory on every platform (macOS, Linux, and Windows). View them with:

```bash
flowly service logs
```

## Smart restart

```bash
flowly restart
```

`flowly restart` is a smart dispatch: it detects how the gateway is running (launchd / systemd / Task Scheduler, or a plain foreground process) and restarts it the right way. Some actions — such as changing the active persona — auto-restart the gateway for you.

## Related

- [Installation](../getting-started/installation.md)
- [Configuration](./configuration.md)
- [Sessions](./sessions.md)
- [Personas](./personas.md)
- [Sandbox and approvals](./sandbox-and-approvals.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
- [Environment variables](../reference/environment-variables.md)
