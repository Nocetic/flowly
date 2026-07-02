---
title: Installation
eyebrow: Getting Started
description: Flowly is a single Python package, flowly-ai, that runs entirely on your machine. It needs Python ≥ 3.11 and runs on macOS, Linux, and Windows; the first-run picker (sign in with a Flowly account or enter your own API key) opens automatically after install.
---

## Install methods

| Method | Command | When to use |
|---|---|---|
| Native script | `curl -fsSL https://useflowlyapp.com/install.sh \| bash` | **Recommended** — git checkout; `flowly update` pulls new versions between releases |
| `uv tool` | `uv tool install flowly-ai` | Packaged PyPI install, isolated env |
| `pip --user` | `pip install --user flowly-ai` | Standard Python users |
| Source | `git clone … && pip install -e .` | Contributors |

```bash
# Recommended (macOS / Linux)
curl -fsSL https://useflowlyapp.com/install.sh | bash

# Recommended (Windows PowerShell)
irm https://useflowlyapp.com/install.ps1 | iex

# Packaged PyPI install
uv tool install flowly-ai
pip install --user flowly-ai
```

All methods install the same `flowly` CLI. The native script clones the repo into an isolated, uv-managed virtualenv and installs Flowly editable — so it needs no pre-installed Python (uv provides it), and `flowly update` can fast-forward it with `git pull` without waiting for a PyPI release. The packaged methods track PyPI releases instead.

**Already have Flowly installed?** Running the native script over an existing PyPI/`uv tool` install migrates it in place: your `~/.flowly` data is untouched, the old package is retired only after the new install proves it works, and an installed background service is rewritten onto the new install and restarted — so nothing keeps pointing at the retired binary.

## First run

On a fresh machine the **first-run picker opens automatically** right after the install script finishes. It asks how to power Flowly — **sign in with a Flowly account** (managed, nothing else to configure) or **enter your own API key** — which is the one mandatory step before the agent can run. The same picker also seeds your workspace and offers to start the gateway.

If it didn't open automatically (e.g. you installed via `uv tool`/`pip` in a non-interactive shell), run it yourself:

```bash
flowly setup
```

See [Setup wizard](./setup-wizard.md) for every subcommand and the BYOK one-shot.

## Updating

The simplest way is the built-in updater — it detects how Flowly was installed and upgrades in place (no prompt). For a native-script (git checkout) install it runs `git pull --ff-only` + reinstall; for the packaged methods it upgrades the package (on Windows the PyPI paths relaunch through a detached helper so the running `flowly.exe` isn't locked):

```bash
flowly update
```

Or upgrade manually with the same tool you installed it with:

```bash
flowly update                      # git checkout: git pull + reinstall
uv tool upgrade flowly-ai          # if installed via uv
pip install -U --user flowly-ai    # if installed via pip
```

Or re-run the native install script, which fast-forwards the checkout to the latest commit:

```bash
curl -fsSL https://useflowlyapp.com/install.sh | bash
```

Check the installed version with:

```bash
flowly --version
```

## Running as a background service

By default Flowly runs in your terminal session. To keep the gateway running without a terminal — surviving reboots and terminal close — install it as a background service:

```bash
flowly service install --start
```

This registers a service using your platform's native scheduler:

| Platform | Backend | Service file |
|---|---|---|
| macOS | launchd | `~/Library/LaunchAgents/ai.flowly.gateway.plist` |
| Linux | systemd (user unit) | `~/.config/systemd/user/ai.flowly.gateway.service` |
| Windows | Task Scheduler | `~/AppData/Local/flowly/ai.flowly.gateway.xml` |

The service label is `ai.flowly.gateway`.

> [!NOTE]
> On Linux, enabling systemd linger lets the service survive logout — `flowly doctor --fix` can enable this for you. On Windows, Flowly tries Task Scheduler first; if that's denied (no administrator shell) it automatically falls back to a Startup-folder launcher that runs the gateway at logon — so **admin is not required**.

For the full lifecycle (`start`, `stop`, `restart`, `status`, `logs`, `uninstall`), see [Service](../using-flowly/service.md).

## Verify your install

```bash
flowly doctor          # diagnose config + runtime health
flowly status          # show gateway status
```

## Related

- [Quickstart](./quickstart.md)
- [Setup wizard](./setup-wizard.md)
- [Configuration](../using-flowly/configuration.md)
- [Running as a service](../using-flowly/service.md)
- [Providers and models](../using-flowly/providers-and-models.md)
- [CLI commands](../reference/cli-commands.md)
- [Environment variables](../reference/environment-variables.md)
