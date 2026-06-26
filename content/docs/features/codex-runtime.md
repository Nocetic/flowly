---
title: Codex Runtime
eyebrow: Features
description: Hand a heavy coding turn to OpenAI's Codex app-server while Flowly stays the shell around it. Opt-in, off by default.
group: Automation
---

Flowly can delegate a coding turn to the OpenAI **Codex app-server** instead of running the turn in its own loop. When the `codex_session` tool is enabled, Flowly spawns the `codex` CLI as a subprocess, sends the turn to it, and projects Codex's streamed items (commands, file edits, tool calls) back into the conversation inline — so it reads as one continuous session.

This is an **opt-in tool, disabled by default**. It is not a runtime swap: your normal Flowly agent loop is unchanged unless you turn it on.

> [!NOTE]
> Codex runs the coding turn, but Flowly stays the shell: sessions, memory, skills, approvals, and the gateway all still belong to Flowly.

## Prerequisites

The Codex CLI must be installed and authenticated on the same machine as Flowly:

```bash
# Install the Codex CLI
npm i -g @openai/codex

# Authenticate (writes tokens Codex reads on startup)
codex login
```

Codex auth and Flowly auth are separate — `codex login` is required even if you are signed in to Flowly.

## Enabling

Enable the tool from your shell or from a live session:

```bash
flowly codex enable      # turn the codex_session tool on
flowly codex status      # show whether it's enabled + codex CLI health
flowly codex disable     # back to Flowly's own runtime
```

Inside a session you can toggle it with the slash command:

```
/codex on
/codex off
/codex sandbox <read-only|workspace-write|full-access>
/codex tools on|off
```

Enabling sets `tools.codexSession.enabled` to `true` in `~/.flowly/config.json`.

## Working directory

Codex runs commands and edits files relative to a working directory. Set it once and it persists:

```bash
flowly codex cwd ~/projects/myapp    # set the directory Codex runs in
flowly codex cwd                     # show the current directory
```

or in a session:

```
/codex cwd ~/projects/myapp
```

The directory is stored in `tools.codexSession.cwd`. When unset, Flowly resolves a directory deterministically (explicit override → per-session value → `FLOWLY_CWD` → `agents.defaults.cwd` → your workspace) and passes it to Codex when the thread starts.

## Flowly tools inside Codex

Codex ships its own toolset (shell, file edits, planning). To keep Flowly's richer tools available during a Codex turn, Flowly registers itself as an MCP callback (`flowly-tools`) that Codex can call back into. The callback exposes:

- `web_search`
- `web_fetch`
- `video_analyze`
- `skill_view`
- `skills_list`

This is controlled by `tools.codexSession.exposeFlowlyTools`.

## Configuration

All keys live under `tools.codexSession` in `~/.flowly/config.json` (camelCase on disk):

| Key | What it does |
|---|---|
| `enabled` | Turns the `codex_session` tool on or off. |
| `codexBin` | Path to the `codex` binary (when not on `PATH`). |
| `codexHome` | Overrides `CODEX_HOME` for the subprocess (Codex state dir). |
| `cwd` | Persistent working directory for Codex. |
| `turnTimeoutS` | Hard deadline for a single Codex turn (watchdog). Default `600`. |
| `postToolQuietTimeoutS` | Wedge timeout after a tool call with no further output. Default `90`. |
| `approvalPolicy` | How Codex command/edit approvals are handled: `on-request` (default), `never`, `auto-review`, `granular`. |
| `sandbox` | Codex sandbox profile for the turn: `read-only`, `workspace-write` (default), `full-access`. |
| `exposeFlowlyTools` | Registers the `flowly-tools` MCP callback. |

> [!NOTE]
> Codex runtime requires the `codex` CLI at **version 0.125.0 or newer**; `flowly codex enable` checks this and the runtime self-heals across the 0.125 item-state format. `enable` also accepts `--sandbox <profile>` and `--expose-tools` to set those two keys up front.

## Reliability

A few safeguards keep long Codex turns from hanging:

- **Self-heal on a lost thread.** If a resumed Codex thread no longer exists, Flowly drops the dead thread and transparently starts a fresh one rather than erroring.
- **Watchdogs.** A hard per-turn deadline (`turnTimeoutS`) and a post-tool quiet timeout (`postToolQuietTimeoutS`) stop a stalled turn.
- **OAuth failures** are surfaced as a clear "run `codex login`" message.

## Related

- [Delegation & subagents](delegation.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
