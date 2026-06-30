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

How Flowly checks for an update depends on how it's installed. A native-script
**git checkout** fetches its branch from git and measures how many commits it's
behind; the **packaged** installs read the latest release from PyPI. Either way,
if there's something newer Flowly upgrades in place and restarts the gateway so
the new code takes effect. There's **no confirmation prompt**: running the
command is the confirmation (use `--check` for a dry look). On Windows, the
**PyPI** upgrade paths relaunch through a small detached helper so the running
`flowly.exe` isn't locked while pip replaces it; the git-checkout path doesn't
need that — its launcher runs `python -m flowly`, so nothing has to overwrite a
running executable.

## Install-mode aware

`flowly update` figures out **how** Flowly is installed and uses the matching
upgrade path. You never pick the command:

| How you installed | What `update` runs |
| --- | --- |
| Native install script (git checkout) | `git pull --ff-only` + editable reinstall |
| `uv tool` | `uv tool upgrade flowly-ai` |
| `pipx` | `pipx upgrade flowly-ai` |
| `pip` | `pip install --upgrade flowly-ai` |
| **Inside Flowly Desktop** | **nothing** — the app owns its binary (see below) |

The native `curl … | bash` / `irm … | iex` installers produce the git-checkout
(source) install, so most users land on the first row: `flowly update` pulls the
latest commit straight from git, no PyPI release required.

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

1. Flowly is upgraded via the matching command — a git checkout is pulled
   (`git pull --ff-only`, autostashing any local changes) and reinstalled
   editable; a packaged install is upgraded in place.
2. Stale bytecode (`__pycache__`) is cleared so a restart doesn't import a
   half-old/half-new mix.
3. The gateway is restarted via the smart [`flowly restart`](/docs/using-flowly/service)
   — it bounces the launchd / systemd / Task Scheduler service if one is
   installed, or prints a hint if the gateway is running in the foreground.

## Pitfalls

- **PyPI unreachable.** For a packaged install, if the version check can't reach
  PyPI, `update` stops unless you pass `--force`. A git checkout fetches from its
  git remote instead, so this doesn't apply to it.
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
