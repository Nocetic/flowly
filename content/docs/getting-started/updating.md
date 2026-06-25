---
title: Updating Flowly
eyebrow: Getting Started
description: Keep the CLI current with `flowly update`. It detects how Flowly was installed, runs the right upgrade, and bounces the gateway — and does nothing inside Flowly Desktop, which manages its own binary.
---

## The short version

```bash
flowly update            # check, upgrade in place, restart the gateway
flowly update --check    # just tell me if a newer version exists
```

Flowly reads the latest released version from PyPI, compares it to what you're
running, and — if there's something newer — upgrades in place and restarts the
gateway so the new code takes effect. There's **no confirmation prompt**: running
the command is the confirmation (use `--check` for a dry look). On Windows,
`update` relaunches itself through a small detached helper so the running
`flowly.exe` isn't locked while pip replaces it.

## Install-mode aware

`flowly update` figures out **how** Flowly is installed and uses the matching
upgrade path. You never pick the command:

| How you installed | What `update` runs |
| --- | --- |
| Install script / `uv tool` | `uv tool upgrade flowly-ai` |
| `pipx` | `pipx upgrade flowly-ai` |
| `pip` | `pip install --upgrade flowly-ai` |
| Git checkout (source) | nothing — prints the `git pull` + reinstall steps |
| **Inside Flowly Desktop** | **nothing** — the app owns its binary (see below) |

## Flowly Desktop is separate

Flowly Desktop ships its own compiled copy of the agent. Running inside the
desktop app, `flowly update` detects that it's the managed binary and **no-ops**
with a pointer to update the app instead. The desktop app updates itself (and the
bundled agent) through its own updater — a CLI update and a desktop update never
touch each other, because they're physically separate installs.

> [!NOTE]
> So you can safely run `flowly update` on a machine that also has Flowly
> Desktop installed: it only ever upgrades the CLI on your PATH.

## Flags

| Flag | Effect |
| --- | --- |
| `--check` | Only report whether a newer version exists; don't install. |
| `--yes`, `-y` | No-op, kept for back-compat — `update` no longer prompts, so there's nothing to confirm. |
| `--force` | Reinstall the latest even if you're already up to date (or PyPI is unreachable). |
| `--no-restart` | Upgrade but don't bounce the gateway — run `flowly restart` yourself later. |

## What happens on a successful update

1. The package is upgraded via the matching command.
2. Stale bytecode (`__pycache__`) is cleared so a restart doesn't import a
   half-old/half-new mix.
3. The gateway is restarted via the smart [`flowly restart`](/docs/using-flowly/service)
   — it bounces the launchd / systemd / Task Scheduler service if one is
   installed, or prints a hint if the gateway is running in the foreground.

## Pitfalls

- **PyPI unreachable.** If the version check can't reach PyPI, `update` stops
  unless you pass `--force`.
- **Foreground gateway.** A gateway started with `flowly gateway` in a terminal
  can't be restarted from outside that terminal — `update` tells you, and you
  restart it where it's running.
- **Source checkouts** update with git, not this command: `git pull` then
  reinstall your editable install.
