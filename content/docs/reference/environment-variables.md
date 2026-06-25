---
title: Environment Variables
eyebrow: Reference
description: Environment variables Flowly reads at startup, with their defaults. Most users never need these — config.json covers the common cases.
---

Flowly is configured mainly through `~/.flowly/config.json`. The environment variables below override specific behaviors and are handy for wrapper scripts, CI, and headless setups.

## Profiles & home

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_HOME` | `~/.flowly` | The profile/home directory — where config, sessions, credentials, skills, and databases live. |
| `FLOWLY_PROFILE` | `default` | Profile name, for wrapper scripts. Resolution order: `-p` flag → `FLOWLY_PROFILE` → `~/.flowly/active_profile` → `default`. |

## Sandbox & execution

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_SANDBOX` | on | Set to `0` / `false` / `off` / `no` to disable the OS sandbox (macOS `sandbox-exec` / Linux `bwrap`). |
| `FLOWLY_SANDBOX_WRAPPED` | — | Internal recursion guard set when re-execing under the sandbox. **Do not set this yourself.** |
| `FLOWLY_CWD` | — | Override the runtime working directory for shell/exec and Codex. |
| `FLOWLY_BASH_PATH` | — | Path to the `bash` binary used for command execution. |

## Agent & LLM

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_LLM_TIMEOUT_SECONDS` | `120` | Timeout for a non-streaming LLM call. |
| `FLOWLY_LLM_STREAM_TIMEOUT_SECONDS` | `120` | Timeout for a streaming LLM call. |
| `FLOWLY_CLAUDE_CACHE_TTL` | `1h` | TTL for the Anthropic prompt cache (Claude models). |

## Cron

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_CRON_TIMEOUT` | `600` | Per-job watchdog timeout, in seconds. |
| `FLOWLY_CRON_RETENTION_DAYS` | `30` | How long per-run output archives are kept. |

## Media

Generated media (image generation, etc.) is written to `<FLOWLY_HOME>/media`. The gateway prunes it at start so it can't fill the disk; recent files are kept so chat-history re-fetch still works.

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_MEDIA_RETENTION_DAYS` | `30` | Delete generated media older than this many days at gateway start. `-1` disables the age cap. |
| `FLOWLY_MEDIA_MAX_SIZE_MB` | `500` | If `<FLOWLY_HOME>/media` is still larger than this, delete the oldest files until under cap. `0` disables the size cap. |

## Skills & plugins

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_HUB_REGISTRY` | `https://useflowlyapp.com` | Skill hub registry base URL. |
| `FLOWLY_ENABLE_PROJECT_PLUGINS` | off | Set to `1` to load project-local plugins from the working directory. |

## Provider & account

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_API_BASE` | `https://useflowlyapp.com` | Base URL for the hosted Flowly API / relay. |
| `FLOWLY_SERVER_ID` | — | Relay server id (set during `flowly login`). |
| `FLOWLY_USER_AGENT` | `FlowlyBot/1.0` | HTTP User-Agent for outbound requests. |
| `FLOWLY_XAI_OAUTH_MODEL` | `grok-4.20-reasoning` | Model used with an xAI OAuth subscription. |
| `FLOWLY_X_SEARCH_MODEL` | `grok-4.20-reasoning` | Model used by the `x_search` tool. |
| `FLOWLY_AUTH_DEBUG` | off | Set to `1` for verbose auth logging. |
| `CODEX_HOME` | `~/.codex` | State directory for the Codex CLI subprocess (Codex runtime). |

## Tool credentials

These let tools pick up credentials from the environment instead of `config.json`:

| Variable | Used by |
|---|---|
| `BRAVE_API_KEY` | `web_search` |
| `GROQ_API_KEY` | Voice STT (Groq Whisper) |
| `TRELLO_API_KEY`, `TRELLO_TOKEN` | `trello` |
| `XAI_API_KEY` | `x_search` (fallback when no OAuth subscription) |
| `XAI_BASE_URL` | `x_search` (overrides the xAI API base URL) |
| `GITHUB_TOKEN` | GitHub MCP server (from the catalog) |
| `EDITOR` | Opening the TUI draft with `Ctrl+E` |

## TUI

| Variable | Default | What it does |
|---|---|---|
| `FLOWLY_TUI_THEME` | `flowly` | Default TUI theme. |
| `FLOWLY_BROWSER_PLAN_ENABLED` | `1` | Toggles the `browser_plan` tool. |
| `FLOWLY_BROWSER_PLAN_PERSIST` | — | Controls browser-plan persistence. |

## Related

- [Configuration](../using-flowly/configuration.md)
- [CLI commands](cli-commands.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
