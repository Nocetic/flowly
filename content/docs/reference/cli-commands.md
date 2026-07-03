---
title: CLI Commands
eyebrow: Reference
description: Every flowly command and subcommand, generated from the CLI itself. Run flowly --help for the live tree.
---

The `flowly` command is the entry point for everything — running the agent, starting the gateway, managing channels, skills, plugins, MCP servers, and more. Run `flowly --help` for the live command tree, or `flowly <command> --help` for any subcommand.

> [!TIP]
> Add `-p <profile>` / `--profile <profile>` before any command to target a non-default profile (it sets `FLOWLY_HOME` before anything loads).

## Top-level commands

| Command | What it does |
|---|---|
| `flowly` | Smart entry: opens the terminal chat when a provider/account is configured; on a fresh machine it launches the first-run onboarding instead. |
| `flowly setup` | First-run onboarding + configuration — sign in with a Flowly account or enter your own API key, plus channels and tools. |
| `flowly enroll` | Connect a phone / remote device: enable remote access and print the LAN IP, port, token, TLS note, and firewall steps. |
| `flowly agent -m "..."` | Send a one-shot message to the agent. |
| `flowly gateway` | Start the gateway daemon (channels run through it). |
| `flowly login` / `logout` | Sign in / out of a Flowly account (OAuth, optional — BYOK works without it). |
| `flowly update` | Update Flowly in place — `git pull` for a git-checkout install, or the matching package upgrade (uv-tool/pipx/pip) otherwise. Auto-detects the install mode; `--check` previews, no-op inside Flowly Desktop. |
| `flowly restart` | Restart the gateway (auto-detects service vs foreground). |
| `flowly doctor` | Check configuration and runtime health (`--fix` to auto-repair). |
| `flowly status` | Show Flowly status. |
| `flowly service` | Manage the background gateway service. |
| `flowly channels` | Manage channels. |
| `flowly cron` | Manage scheduled tasks. |
| `flowly skills` | Manage skills (list / install / remove / search). |
| `flowly skill` | Govern self-improved skills (mine, curate, rollback, archive, usage). |
| `flowly bundles` | Manage skill bundles. |
| `flowly memory` | Inspect and correct long-term memory (list, review, accept/reject, dream, correct, undo). |
| `flowly plugins` | Manage plugins. |
| `flowly mcp` | Manage MCP servers. |
| `flowly persona` | Manage the bot persona. |
| `flowly approvals` | Manage command-execution approvals. |
| `flowly codex` | Manage the Codex app-server runtime (opt-in). |
| `flowly sessions` | Monitor background subagent tasks. |
| `flowly pairing` | Secure channel pairing. |
| `flowly xai` | Connect an xAI / Grok subscription via OAuth. |
| `flowly bootstrap` | Non-interactive workspace bootstrap (safe for installers). |
| `flowly onboard` | Alias for `flowly setup` (the first-run onboarding). |

## flowly

Bare `flowly` opens the terminal chat (TUI) once a provider/account is configured. On a fresh machine it launches the onboarding picker instead (sign in with a Flowly account or enter an API key). There is no separate `flowly tui` command.

| Option | Description |
|---|---|
| `--host` | Gateway host (default `127.0.0.1`). |
| `--port`, `-P` | Gateway port (default `18790`). |
| `--session`, `-s` | Session key (default: resume last; use `--new` for fresh). |
| `--new`, `-n` | Start a fresh session, ignoring last state. |
| `--theme` | TUI theme: `flowly`, `moonfly`, `catppuccin`, `tokyo-night`, `synthwave`, `mono`, `amber`, `hacker`. |

## flowly agent

One-shot interaction with the agent.

| Option | Description |
|---|---|
| `--message`, `-m` | Message to send. |
| `--session`, `-s` | Session ID (default `cli:default`). |

## flowly gateway

Start the gateway daemon.

| Option | Description |
|---|---|
| `--port`, `-p` | Gateway port (default `18790`). |
| `--verbose`, `-v` | Verbose output. |
| `--persona` | Bot persona: `default`, `jarvis`, `pirate`, `samurai`, `casual`, `professor`, `butler`, `friday`. |
| `--remote` | Accept connections from your phone / other devices (plain-language alias for `--host 0.0.0.0`; a token is ensured automatically). |
| `--host` | Bind address. `0.0.0.0` accepts remote clients; default `127.0.0.1` (local only). |
| `--token` | Set an explicit remote-access token (otherwise one is auto-generated on the first non-loopback bind). |
| `--rotate-token` | Generate a fresh remote-access token before starting (invalidates the old one). |

## flowly enroll

Connect a phone or another device to this gateway in one step: enables remote access (binds `0.0.0.0` + ensures a token), prints the **LAN IP** to use on the same Wi-Fi (plus the public IP for internet access), the port, token, and TLS note, and offers to open the firewall on Windows. Restart the gateway afterward so it rebinds.

## flowly setup

The first-run onboarding + configuration surface. Bare `flowly setup` seeds the workspace and opens the picker — **sign in with a Flowly account or enter your own API key** — then offers to start the gateway. Subcommands:

| Subcommand | What it does |
|---|---|
| `setup channels` | Connect messaging channels (Telegram / Discord / Slack). |
| `setup tools` | Configure tool integrations (browser, voice, Trello, …). |
| `setup byok <slug>` | Quick BYOK one-shot: save an API key (slugs: `openrouter`, `anthropic`, `openai`, `xai`, `gemini`, `groq`, `zhipu`, `sakana`). |
| `setup agents` | Set up multi-agent orchestration. |
| `setup google-workspace` | Install and authenticate the Google Workspace CLI (gws). |

## flowly service

Manage the background gateway service (launchd / systemd / Task Scheduler).

| Subcommand | What it does |
|---|---|
| `install` | Install the background service. Idempotent (re-running reinstalls cleanly; no `--force` needed). Flags include `--start`, `--remote`, `--host`, `--token`, `--port`, `--persona`. |
| `start` | Start the installed service (won't launch a duplicate if a gateway already holds the port). |
| `stop` | Stop the service. |
| `restart` | Restart the gateway. |
| `status` | Show service state, local health, and a port/process diagnostic (warns if a gateway is running outside the service). |
| `logs` | Show service logs (real-time by default). |
| `uninstall` | Remove the service definition. |

## flowly channels

| Subcommand | What it does |
|---|---|
| `status` | Show channel status. |
| `login` | Link a device via QR code. |

## flowly cron

| Subcommand | What it does |
|---|---|
| `list` | List scheduled jobs. |
| `add` | Add a scheduled job. |
| `remove` | Remove a scheduled job. |
| `enable` | Enable or disable a job. |
| `run` | Manually run a job (delegates to the running gateway). |

## flowly skills

| Subcommand | What it does |
|---|---|
| `list` | List installed skills. |
| `install` | Install a skill. |
| `remove` | Remove an installed skill. |
| `search` | Search the registry for skills. |

## flowly skill

Govern the opt-in skill self-improvement subsystem (distinct from `flowly skills`, which installs/removes skills). See [Skill self-improvement](../features/skill-self-improvement.md).

| Subcommand | What it does |
|---|---|
| `mine` | Mine recurring procedures from history into proposed skills (`--dry-run` to preview). |
| `curate` | Review and apply proposed skill improvements. |
| `usage` | Show how often each self-improved skill has been used. |
| `log` | Show the skill-change operation log. |
| `undo` | Undo the last skill change. |
| `rollback` | Roll a skill back to an earlier snapshot. |
| `archive` / `restore` | Archive a skill (and restore it later). |
| `stale` | List skills that haven't been used in a while. |

## flowly bundles

| Subcommand | What it does |
|---|---|
| `list` | List bundles for the active profile. |
| `show` | Show one bundle's full definition. |
| `create` | Create a new bundle. |
| `delete` | Delete a bundle file. |
| `reload` | Drop the in-process bundle cache. |

## flowly memory

Inspect and correct long-term memory (the governed memory store). See [Memory](../features/memory.md).

| Subcommand | What it does |
|---|---|
| `list` | List stored memories. |
| `review` | Review pending memory candidates (the `needs_review` queue). |
| `accept` / `reject` | Accept or reject a candidate memory. |
| `dream` | Run a cross-session "dreaming" pass now — scan recent chats and learn durable facts (also runs automatically on idle / daily / every N turns). `--max-messages` caps the batch. |
| `feedback` | Give 👍/👎 feedback to retune a memory's trust score. |
| `correct` | Correct a stored memory's content. |
| `undo` | Undo the last memory change. |
| `refresh` | Rebuild the `MEMORY.md` block from the governed store. |
| `status` / `stats` | Show memory store status and statistics. |
| `consolidate` | Run a consolidation pass (merge duplicates, retire stale notes). |
| `migrate` | Migrate the memory store to the latest schema. |

## flowly plugins

| Subcommand | What it does |
|---|---|
| `list` | List discovered plugins (bundled + user + project). |
| `install` | Install from a git URL, `owner/repo`, `owner/repo/subpath`, or local path. |
| `enable` | Enable a plugin (add to `plugins.enabled`). |
| `disable` | Disable a plugin. |
| `remove` | Uninstall a plugin (deletes its directory under `$FLOWLY_HOME/plugins/`). |

## flowly mcp

| Subcommand | What it does |
|---|---|
| `list` | Show configured MCP servers. |
| `add` | Register an MCP server (probes by default; `--no-probe` to skip). |
| `remove` | Drop a server entry. |
| `enable` / `disable` | Toggle whether a server loads at agent boot. |
| `configure` | Pick which of a server's tools are enabled (interactive). |
| `serve` | Run Flowly itself as an MCP server on stdio. |
| `catalog` | List the curated, ready-to-install MCP servers. |
| `install` | Install a curated server from the catalog. |
| `picker` | Browse the catalog and install interactively. |
| `test` | Connect to a server and show its tool list. |
| `login` | Run (or re-run) the OAuth flow for an OAuth-configured HTTP server. |

## flowly approvals

| Subcommand | What it does |
|---|---|
| `status` | Show exec-approvals configuration. |
| `set` | Update exec-approvals configuration. |
| `list` | List allowlist entries. |
| `add` | Add a pattern to the allowlist. |
| `remove` | Remove a pattern from the allowlist. |
| `safe-bins` | List the safe bins that are always allowed. |

## flowly codex

Manages two unrelated features that share the "Codex" name: the `codex_session` **tool** (delegate a coding turn to a `codex app-server` subprocess — see [Codex runtime](../features/codex-runtime.md)) and the `openai_codex` **provider** (run Flowly's own agent loop on your ChatGPT subscription — see [Providers & models](../using-flowly/providers-and-models.md)).

| Subcommand | What it does |
|---|---|
| `enable` | Enable the `codex_session` tool (delegate coding turns to Codex). |
| `disable` | Disable it (back to Flowly's own runtime). |
| `cwd` | Set (or show) the working directory Codex runs in — persistent. |
| `status` | Show whether the `codex_session` tool is enabled + codex CLI health + ChatGPT subscription connection state. |
| `login` | Sign in with ChatGPT (Codex OAuth) — sets `openai_codex` as the active provider. |
| `logout` | Remove the stored ChatGPT subscription tokens (leaves a `codex login` session in `~/.codex/auth.json` untouched). |

`login` options: `--device` (headless code-entry flow instead of a browser), `--no-browser`, `--manual-paste`, `--no-set-active`, `--timeout <seconds>`.

## flowly persona

| Subcommand | What it does |
|---|---|
| `list` | List available personas. |
| `set` | Set the active persona. |
| `show` | Show persona details. |

## flowly sessions

| Subcommand | What it does |
|---|---|
| `list` | List background subagent tasks. |
| `clear` | Clear completed/failed task history. |

## flowly pairing

| Subcommand | What it does |
|---|---|
| `list` | List pending pairing requests. |
| `approve` | Approve a pairing code. |
| `revoke` | Revoke access for a user. |
| `allowed` | List allowed users from the pairing store. |

## flowly xai

| Subcommand | What it does |
|---|---|
| `login` | Sign in to xAI OAuth for SuperGrok / X Premium+ API access. |
| `status` | Show xAI OAuth connection status. |
| `logout` | Remove stored xAI OAuth tokens. |
| `test` | Validate the stored token against xAI `/models`. |

## flowly login

Sign in with a Flowly account (OAuth-driven, optional — BYOK works without it).

| Option | Description |
|---|---|
| `--no-browser` | Don't try to open the authorization URL. |
| `--repair` | Re-register + re-wire relay config using existing tokens (no browser). |
| `--dry-run` | Show what `--repair` would change without writing. |
| `--key <flw_…>` | Use a Flowly account key you already have (e.g. from the Desktop app) — sets the `flowly` provider with no server record and no relay. |
| `--relay` / `--no-relay` | Force remote/phone reach (server registration + relay) on or off, skipping the interactive prompt. Default: ask. |

## flowly doctor

| Option | Description |
|---|---|
| `--fix`, `-f` | Auto-repair fixable issues. |

## Related

- [Slash commands](slash-commands.md)
- [Environment variables](environment-variables.md)
- [Configuration](../using-flowly/configuration.md)
