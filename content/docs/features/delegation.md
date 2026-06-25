---
title: Delegation
eyebrow: Features
description: Flowly can hand work off to other agents instead of doing everything in one turn, via two structurally distinct mechanisms — lightweight in-process subagents and external CLI-subprocess agents orchestrated into teams.
group: Automation
---

The two delegation mechanisms share almost no code, behave differently, and are set up differently:

1. **In-process subagents** — lightweight agent instances that run inside the same Flowly process, with an isolated context and a restricted tool registry. Reached by the agent via the `spawn` and `builtin_agent` tools.
2. **CLI-subprocess agents and teams** — external coding CLIs (Claude Code, Codex, Gemini, opencode, droid) invoked as subprocesses. Reached via the `delegate_to` tool and orchestrated into teams by an orchestrator. Set up with `flowly setup agents`.

The desktop UI unifies both under one "Agents" tab, but the implementations are independent.

## In-process subagents

When the main agent decides a task should run in isolation (a focused write-up, a research pass, a self-review), it calls the `spawn` or `builtin_agent` tool. The `SubagentManager` then creates a subagent that:

- Runs in the **same Python process** and shares the parent's LLM provider.
- Gets an **isolated message list** — no parent conversation history is passed in. The subagent only sees a focused system prompt plus the task you assigned it.
- Gets a **fresh, restricted tool registry**. Subagents can build only these tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `memory_append`, `exec`, `web_search`, `web_fetch`, `skill_manage`, `knowledge_graph`, `artifact`. An assistant can narrow this further with `allowed_tools` (the `artifact` tool is always kept). A `self-review` run is forced to `memory_append` + `knowledge_graph` only.
- **Cannot spawn its own subagents** (no recursion). The tools `spawn`, `builtin_agent`, and `delegate_to` are blocked inside subagents, along with cron, user-facing I/O (`message`, `voice_call`, `email`), external writes (`google_*`, `linear`, `trello`, `x`), GUI/gateway tools (`screenshot`, `browser_tab`, `computer`), cross-session reads (`sessions_list`, `session_search`, `memory_search`, `memory_get` — note `memory_append` stays allowed), and `system` / `docker` / `process`.

### Results announced back to the parent

Subagents do not return their output as a normal tool result you have to wait on. When a subagent finishes, the manager **announces** its result back into the parent session as a system message: a status line, the run duration, a `Tools used: …` summary, and a result preview truncated to **2000 characters**. If the parent session is busy when the child finishes, the announcement is queued and delivered when the session frees up.

By default `spawn` runs **fire-and-forget** (async): it returns a `dispatched` envelope immediately and the result is announced later. The envelope deliberately includes verbose "required next steps" wording to stop the parent from inventing an answer before the child has actually finished.

### Concurrency and timeouts

> [!WARNING]
> **Concurrency is a hard cap of `MAX_CONCURRENT = 5`** parallel children per `SubagentManager`. This is a literal constant in the code — **it is not configurable** and there is no config key for it. Spawning over the cap returns a `rejected` status.

- **Wall-clock cap:** each subagent run is bounded by a `900`-second wall-clock timeout.
- **`spawn` per-call timeout:** defaults to `600` seconds if unset, and is clamped to the range `[120, 1800]` seconds.
- **`builtin_agent` timeout:** the tool does not pass a timeout, so the manager's `900`-second wall-clock applies.
- Each subagent also runs a bounded loop (max 15 iterations) and a 120-second per-tool timeout.

If you abort the parent turn, all of that session's running children are cancelled.

### Built-in agents (writer / researcher / coder)

Three specialist personas ship in code, all running `claude-haiku-4.5` via OpenRouter:

| Name | Role | Notes |
| --- | --- | --- |
| `writer` | Reshape supplied source material into an essay, doc, or article. | Caps output to an artifact; async dispatch. |
| `researcher` | Deep research **and** writes a self-contained final markdown report. Do not chain `writer` after it. | Caps output to an artifact; async dispatch. |
| `coder` | Code review, refactoring, debugging. | Produces a `code` artifact; runs synchronously. |

A **duplicate-dispatch guard** stops `builtin_agent` from re-running the same specialist in the same session within `600` seconds — it points you at the prior artifact instead. Bypass it by prefixing the task with `FRESH:`.

### Overriding built-ins with your own assistants

User-defined assistants live in `~/.flowly/assistants/{name}.md` — YAML frontmatter plus a markdown body. A user file **overrides a builtin of the same name**, so dropping a `writer.md` there replaces the shipped writer.

Frontmatter fields:

- Required: `name`, `description`, `model`
- Optional: `allowed_tools`, `auto_save_artifact`, `artifact_type`, `cap_to_artifact`, `async_dispatch`

> [!NOTE]
> A `timeout_seconds` field in an assistant file is **ignored** — the 900-second wall-clock governs the run. These assistants are not configured through any wizard; you just create the `.md` file.

## CLI-subprocess agents and teams

The second mechanism delegates to **external coding CLIs** running as subprocesses. The main agent calls the `delegate_to` tool with an `agent_id`; the tool is fire-and-forget:

1. It validates the agent, broadcasts a start event, and returns an immediate "Task delegated to @{agent}…" acknowledgement.
2. In the background it invokes the agent CLI with an **1800-second** timeout, then publishes the result back into your session wrapped in a `[DELEGATE_RESULT:{agent}]` marker that asks the parent to summarize it. While that result is being handled, the `delegate_to` tool is temporarily dropped to prevent re-delegation loops.

The subprocess is built per provider (for example `anthropic` → `claude --dangerously-skip-permissions`, `openai` → `codex exec`). It runs in the agent's configured `working_directory` (or your home directory), with the agent's `AGENTS.md` injected as an appended system prompt.

### Teams

You can address a single agent (`@coder fix the login bug`) or a **team** (`@dev …`). A team has a leader and members. The orchestrator invokes the leader first; teammate mentions in the leader's response drive either a sequential handoff or a **parallel fan-out**. Team chains are bounded by a maximum depth of `10`.

### Setting up agents and teams

Run the interactive wizard:

```bash
flowly setup agents
```

It loads your config, lists existing agents and teams, then offers: **Add an agent / Create a team / Remove an agent / Remove a team / Done**. Adding an agent prompts for an ID (alphanumeric, `-`, `_`) plus name, provider, and model, and writes a `MultiAgentConfig` into your config. Creating a team requires at least two agents. On completion it prints usage hints (`@coder …` for a direct agent, `@dev …` for a team; no mention goes to the main Flowly agent).

> [!NOTE]
> These config entries (`agents.agents{}` and `agents.teams{}` in `~/.flowly/config.json`) configure the **CLI-subprocess path only** — they have nothing to do with in-process subagent concurrency. Each `MultiAgentConfig` carries `name`, `provider` (`anthropic` | `openai` | `flowly`), `model`, `working_directory`, and `persona`.

## Monitoring

Background subagent runs are tracked in a registry persisted to `~/.flowly/subagents/runs.json`. Several surfaces let you watch them:

```bash
flowly sessions list
flowly sessions list --status running
flowly sessions list --watch
flowly sessions clear
flowly sessions clear --all
```

- `flowly sessions list` renders a table (Status / Label / Model / Duration / ID). Filter with `--status running|completed|failed`; `--watch`/`-w` refreshes every 2 seconds.
- `flowly sessions clear` removes completed/failed records. By default it keeps running ones; `--all` clears everything.

In the agent REPL, `/tasks` renders the same sessions table. (It is REPL-only — there is no `/tasks` in the TUI.)

In the TUI, `/subagents` and its alias `/subs` toggle the **Ctrl+A subagent sidebar**, which shows a live row per run — a spinner plus running/ok/fail status — driven by start/completed events as runs progress.

## Related

- [Features overview](../features/overview.md)
- [Sandbox and approvals](../using-flowly/sandbox-and-approvals.md)
- [Memory](../features/memory.md)
- [Codex runtime](./codex-runtime.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
