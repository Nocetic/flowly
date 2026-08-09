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

Flowly installs as a **git checkout** (that's what the install script sets up),
so `update` fetches the branch from git, measures how many commits it's behind,
then pulls (`git pull --ff-only`, autostashing any local changes), reinstalls,
and restarts the gateway so the new code takes effect. There's **no
confirmation prompt**: running the command is the confirmation (use `--check`
for a dry look). Nothing has to overwrite a running executable — the launcher
runs `python -m flowly`, so a pull under a live gateway is safe.

Two places `update` deliberately does nothing:

- **Inside Flowly Desktop** — the app owns its embedded copy of the agent and
  updates it itself (see below).
- **A legacy packaged install** (the old PyPI `flowly-ai` package via
  `uv tool` / `pipx` / `pip`) — those no longer receive releases. Re-run the
  install script and it migrates the install in place, keeping your `~/.flowly`
  data and your background service:

  ```bash
  curl -fsSL https://useflowlyapp.com/install.sh | bash
  ```

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
| `--force` | Reinstall the latest even if you're already up to date. |
| `--no-restart` | Upgrade but don't bounce the gateway — run `flowly restart` yourself later. |

## What happens on a successful update

1. The checkout is pulled (`git pull --ff-only`, autostashing any local
   changes) and reinstalled editable.
2. Stale bytecode (`__pycache__`) is cleared so a restart doesn't import a
   half-old/half-new mix.
3. The gateway is restarted via the smart [`flowly restart`](/docs/using-flowly/service)
   — it bounces the launchd / systemd / Task Scheduler service if one is
   installed, or prints a hint if the gateway is running in the foreground.

## Pitfalls

- **Foreground gateway.** A gateway started with `flowly gateway` in a terminal
  can't be restarted from outside that terminal — `update` tells you, and you
  restart it where it's running.
- **Git checkout on a detached HEAD or non-fast-forward.** `update` only
  fast-forwards: if the checkout isn't on a branch, or local commits have
  diverged from the remote, it stops and points you at the repo to sort it out
  by hand. Local *uncommitted* changes are autostashed and restored around the
  pull.
- **Hot pull under a running gateway.** If the checkout is updated while the
  gateway is still running, a provider/model hot-reload is refused with a
  "restart the gateway" message rather than risking a stale-module import — run
  `flowly restart` to load the new code.
