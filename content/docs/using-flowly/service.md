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

- **macOS (launchd):** runs at load and is kept alive (`KeepAlive`), working directory `$HOME`. launchd relaunches the gateway automatically if it exits. Loaded/unloaded via `launchctl`.
- **Linux (systemd user unit):** `Type=simple`, `Restart=always`, `RestartSec=3` — the gateway is relaunched a few seconds after any crash. `StartLimitIntervalSec=0` disables systemd's default "give up after 5 restarts in 10s" limiter, so a fast early crash-loop never lands the unit in a permanent `failed` state. To survive logout (and start at boot) it needs systemd **linger** enabled — install enables it when permitted, and `flowly doctor --fix` / `loginctl enable-linger` does it otherwise.
- **Windows (Task Scheduler):** the task starts a **console-less supervisor** (a `wscript.exe` launcher) at logon. The supervisor runs the gateway directly — no `cmd.exe`, so there's no console window for Windows to reap on logon — and **relaunches it automatically whenever it exits** (a crash, or a logon reap), with a short backoff. The task also carries `RestartOnFailure` (999 retries) and no execution-time limit. If Task Scheduler is denied because the shell isn't elevated, install **falls back to a Startup-folder launcher** that starts the same supervisor at logon — an administrator shell is *not* required, it just changes which mechanism registers it.

> [!IMPORTANT]
> On Linux, the service only survives logout when systemd linger is enabled (`flowly doctor --fix` can do this). On Windows, an elevated shell lets Flowly use Task Scheduler; without one it automatically uses the Startup folder instead — either way the gateway starts at logon.

## Logs

Gateway logs are written to:

- Windows: `~/AppData/Local/flowly/logs`
- macOS / Linux: `<FLOWLY_HOME>/logs`

The gateway always writes a rotating **`gateway.log`** (new file at midnight, 30‑day retention, `.gz` archives) in that directory on every platform — this is the canonical operational log. On macOS and Linux the service *additionally* captures stdout/stderr to `flowly-gateway.out.log` / `flowly-gateway.err.log`; on Windows the service runs console-less, so `gateway.log` is the single source of truth there. `flowly service logs` tails the right file for your platform:

```bash
flowly service logs
```

## Keeping it alive

If the gateway stops on its own after a while, work through these. Start everywhere with the log and status — a clean crash leaves a traceback at the tail of `gateway.log`:

```bash
flowly service status     # installed? running? local /health?
flowly service logs       # tail gateway.log (Ctrl+C to stop)
```

The most common self-inflicted cause is **no LLM provider configured** — the gateway exits at boot without one, which under a service manager looks like an immediate crash-loop. `flowly service status` and `flowly doctor` both flag it; run `flowly setup`, then `flowly service start`.

**Linux — the service disappears a while after you log out / close SSH.** A `--user` service is torn down when your last login session ends *unless* **linger** is enabled — and `Restart=always` can't save it, because it's the whole user manager going away, not the service crashing:

```bash
loginctl show-user "$USER" --property=Linger     # want: Linger=yes
sudo loginctl enable-linger "$USER"              # or: flowly doctor --fix
```

Genuine crashes are covered by `Restart=always` + `StartLimitIntervalSec=0` (retried indefinitely, never permanently `failed`).

**Windows — it runs, then is down a few hours later.** This is exactly what the console-less supervisor fixes: the launcher relaunches the gateway whenever it exits, so a mid-life crash recovers on its own within seconds. If it still won't stay up:

- `flowly service status` shows the task state; `flowly service logs` tails `gateway.log` for the crash reason.
- Confirm the task exists: `schtasks /query /tn ai.flowly.gateway`. If install used the **Startup-folder fallback** (no admin at install time) there is no scheduled task — the supervisor starts at logon instead, and `flowly service stop` still ends it cleanly via its stop-flag.
- A reinstall refreshes the launcher and task: `flowly service install` (idempotent).

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
