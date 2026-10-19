---
title: Delegation
eyebrow: Features
description: Flowly can hand work off to other agents instead of doing everything in one turn, via two structurally distinct mechanisms — lightweight in-process subagents and external CLI-subprocess agents orchestrated into teams.
group: Automation
---

The two delegation mechanisms share almost no code, behave differently, and are set up differently:

1. **In-process subagents** — lightweight agent instances that run inside the same Flowly process, with an isolated context and a restricted tool registry. Started by the agent with the `spawn` tool.
2. **CLI-subprocess agents and teams** — external coding CLIs (Claude Code, Codex, Gemini, opencode, droid) invoked as subprocesses. Reached via the `delegate_to` tool and orchestrated into teams by an orchestrator. Set up with `flowly setup agents`.

The desktop UI unifies both under one "Agents" tab, but the implementations are independent.

## In-process subagents

The agent starts a subagent with the `spawn` tool when you ask for one ("give this to Luna", "let two agents look at it"), or when it judges one worth it. Flowly never pushes it either way: each subagent is a separate model run with its own cost, and most work the agent simply does itself.

When you ask, the agent quotes your words in the call and Flowly checks that you really wrote them in that message, so nothing a tool returned or the agent assumed can stand in for you; the subagent is then shown as started at your request. At most **3** subagents run per conversation at once (8 across the whole agent) — a budget guard, not a rule about when to delegate.

When it starts one, the agent writes the subagent's brief itself: the goal, what it already knows, the sources to use, what is out of scope and what to return. The subagent:

- Runs in the **same Python process** and shares the parent's LLM provider. It runs on the agent's own model unless you named another one for that task.
- Reads **the conversation so far** — your messages and the agent's replies, without tool calls, tool output or reasoning — then its brief. The agent can start it without the conversation for unrelated work. It also gets your memory and known facts.
- Gets a **fresh, restricted tool registry**: `read_file`, `write_file`, `edit_file`, `list_dir`, `memory_append`, `exec`, `web_search`, `web_fetch`, `web_extract`, `skill_manage`, `knowledge_graph`, `artifact`. A `self-review` run is forced to `memory_append` + `knowledge_graph` only.
- **Cannot spawn its own subagents** (no recursion). The tools `spawn`, `agent_read`, `agent_followup`, `agent_close` and `delegate_to` are blocked inside subagents, along with cron, user-facing I/O (`message`, `voice_call`, `email`), external writes (`google_*`, `linear`, `trello`, `x`), GUI/gateway tools (`screenshot`, `browser_tab`, `computer`), cross-session reads (`sessions_list`, `session_search`, `memory_search`, `memory_get` — `memory_append` stays allowed), and `system` / `docker` / `process`.

### The agent keeps working

Starting a subagent returns at once with its run id and its agent id (the subagent itself, which keeps the same id for every task it gets). The agent does whatever doesn't depend on it, tells you briefly what is running and ends its turn, so the chat stays free while subagents work. Subagents started in the same turn report back together: when the last of them finishes, their results reach the agent in one message — each with its title, run id, the tools it used and its result (up to 8,000 characters for a single run, 24,000 shared across a batch) — and the agent answers your request as a whole from there. A subagent alone in its turn reports as soon as it finishes. The agent reads anything beyond what was shown with `agent_read`, which only ever shows runs started in the same conversation. If the conversation is busy when the results arrive, they wait until it frees up; if you write while the agent is working on them, your message is answered right after.

`agent_wait` is for the rare case where the agent cannot finish its own work in a turn without a result due shortly. It returns as soon as a subagent finishes or writes, or you write; a single wait lasts at most two minutes (30 seconds by default), and a turn waits at most five minutes in all before it ends and lets the results arrive as a message. The agent never sleeps or polls a task list.

A turn that keeps getting results it already has — re-reading the same finished result, polling something that stopped changing — is told so, and after four such results in a row it answers with what it has, without tools. A subagent going round in circles the same way wraps up with a summary.

### Giving a subagent more work

A subagent keeps its conversation after it reports, so the agent can give the same subagent another task with `agent_followup` — when you ask ("ask the same agent to also check B") or when it judges that worth it. The subagent picks up with everything it did before in view: its earlier tasks, the tool results it read and the reports it wrote. Nothing in Flowly pushes the agent toward a follow-up or a new subagent; it is the agent's call, or yours.

- **If the subagent has nothing running**, the task starts a new turn of its conversation in the background. It counts toward the same limits as a new subagent (3 running per conversation, 8 overall) and reports back the same way — together with anything else started in the same turn. Its report says which task it is ("task 2").
- **If the subagent is working**, the task reaches its running turn at its next step, like a message, and that turn's report covers it. A task that arrives just as the turn is finishing starts the subagent's next turn instead — it is kept even if Flowly restarts meanwhile — unless that turn was stopped, by you or by the agent.
- **When you asked for it**, the agent quotes your words and Flowly checks them, as for a new subagent; the task is then shown as started at your request.
- A message from the agent to a subagent with nothing running (`agent_message`) is kept for its next task; it starts nothing on its own.

The agent closes a subagent it no longer needs with `agent_close`. A closed subagent takes no new task or message; its conversation stays readable. Closing one that is working stops its task, with no report. Stopping a task (from its sidebar, the menu bar, or by asking the agent) stops only that task: the subagent stays open for the next one.

### Talking while they work

The agent can message its running subagents at any time with `agent_message` — one, several, or all of the conversation's — to steer, narrow or answer them. A subagent reads the message at its next step; a tool call already in progress is never cut short. A subagent can message the agent in turn, to ask something it can't settle itself or to report something the agent must know before it finishes, and it can wait up to five minutes for the answer. In the chat these exchanges show only as "Sent message to …" steps; the full exchange is in the subagent's transcript.

### Each subagent keeps its own conversation

Every subagent working for you has a conversation of its own, kept the way your conversations are: the task the agent gave it (with the conversation it was given, on its first turn), every step it took, every tool result as it read it, the agent's messages to it, and its result with its tokens and running time. Nothing in it is shortened for display. A tool result too long for the subagent to read in full is cut to the same length the agent itself would read, and its whole output is saved with the subagent's conversation; the subagent can read the rest from there.

- **Read only.** Only the agent that started a subagent writes to its conversation. You can read it from any app — desktop, phone, or through another of your agents — but never write to it: a message, a steer, a compaction, a model change or a deletion aimed at it is refused.
- **Private.** It is stored apart from your conversations, so it never shows up in your chat list, in conversation search, in what other apps can read through Flowly's MCP server, or in what Flowly learns about you. The agent's file tools cannot open it either.
- **Many tasks, one conversation.** Each task the subagent gets is a new turn of the same conversation, marked with its number, so its history reads from the first task to the latest.
- **How long it stays.** A subagent is open while it may still get work: until the agent closes it, or after seven days with nothing to do. Its conversation and its saved tool output are kept 30 days after it closes, then removed — unless its result never reached the conversation that started it. Deleting that conversation closes its subagents and removes their conversations right away.
- **At most 16 open.** A conversation keeps at most 16 subagents open. Starting one more closes the one that has been idle longest; the agent is told which (and that it can close the ones it no longer needs itself), and its conversation stays readable. If all 16 are working, no new one starts until one finishes or is closed.
- Subagents that ran before this version have no conversation of their own; their transcript still opens as before. Background bookkeeping (self-review, Board) never gets one.

### Limits

- A subagent has the same limits as the agent itself: the same number of steps per run and no overall time limit. Each model call and each tool call has its own timeout, and a subagent that keeps getting results it already has wraps up with a summary of what it found.
- Stopping the conversation's turn stops the agent, not its subagents — they usually outlive the turn that started them. Stop a subagent on its own from its sidebar or the menu bar, or ask the agent ("stop them all").
- If one subagent is slow, the others don't wait for it forever: five minutes after the first of them finished, the results so far reach the agent, which hears which ones are still working; theirs follow when they finish. A subagent you stopped is mentioned, not waited for.

### Long runs

A subagent manages its context the way the agent does, measured against the context window of the model it runs on — so a long run, or a run on a model with a small window, keeps working instead of failing when its history outgrows the window. As the history nears the limit, old tool output is shortened first. If that is not enough, the work so far is summarized and the subagent carries on from the summary; its brief is always kept exactly as the agent wrote it. If the model's provider still rejects a request as too large, Flowly shrinks it further and sends it once more, and remembers that provider's real limit for that model, for the agent and every subagent.

A subagent that has done many tasks has a long conversation; its next task is fitted the same way. Its earlier turns are summarized like the rest when they no longer fit — the task it was just given is always kept exactly as written — and the summary is kept with its conversation, so its next task starts from it instead of summarizing again. Its full conversation stays readable as it was.

A summary is a model call: its tokens count toward the subagent's total. While it is being made, the subagent shows as compacting, and its transcript marks the point where its history was summarized, with how much smaller it became. A summary made before a restart is kept with the subagent's working record and is never made again.

### When Flowly restarts

Subagents carry on after Flowly restarts — whether you quit it, it updated, or it crashed. A subagent keeps a private working record while it runs, and when Flowly starts again it picks up from its last finished step, with everything it had done so far. Its result reaches the conversation as usual, and a goal waiting on it keeps waiting. Its token count carries on from where it was, and its running time leaves out the time Flowly was closed.

Flowly never repeats a step that was in progress at that moment. Instead the subagent is told plainly what may have happened — for example that a command it started may have finished, been cut off or still be running — so it checks before doing anything again. A step that had not started yet is reported to it as not run.

There are limits. A subagent is continued only within a day of Flowly stopping; after a longer break, or if Flowly keeps restarting without it getting any further (three times), or after an update that cannot read its saved work, it stops where it was and the conversation is told once. The working record is deleted as soon as the subagent finishes; what it did stays in its conversation, saved once, even when it was continued after a restart. Background bookkeeping, and subagents started from a terminal session (`flowly agent`), are not continued. A later task of a subagent continues the same way, on top of its conversation; if that conversation changed meanwhile, the task stops where it was and the conversation is told once.

A subagent's result counts as delivered only once the conversation has saved the reply that dealt with it. If Flowly stops in between, the result reaches the conversation again after the restart, never twice. A result that finished up to a day before Flowly started again still arrives, saying how long ago it finished.

When Flowly is shut down, subagents are stopped before anything else, so they are saved before the app's shutdown deadline. Another Flowly process on the same machine never settles or takes over subagents that a running Flowly is still working on.

### Tokens and statistics

Every subagent counts the tokens it uses — input, output and cache reads — as it works. The count is shown live while it runs and stays with it when it ends. Flowly shows tokens, not prices.

Each run that ends also leaves a short record — when it ran, who started it, how it ended, how long it took and its tokens; never its brief or result — which outlives the 30-day run history. The bot's subagent statistics (runs, outcomes, durations and tokens, day by day, and how many were a subagent's first task or a later one) come from these records.

### No stored specialists

There are no predefined researcher, writer or coder agents and nothing to set up. Assistant files made by earlier versions under `~/.flowly/assistants/` are left on disk and no longer read.

## CLI-subprocess agents and teams

The second mechanism delegates to **external coding CLIs** running as subprocesses. The main agent calls the `delegate_to` tool with an `agent_id`; the tool is fire-and-forget:

1. It validates the agent, broadcasts a start event, and returns an immediate "Task delegated to @{agent}…" acknowledgement.
2. In the background it invokes the agent CLI with an **1800-second** timeout, then publishes the result back into your session wrapped in a `[DELEGATE_RESULT:{agent}]` marker that asks the parent to summarize it. While that result is being handled, the `delegate_to` tool is temporarily dropped to prevent re-delegation loops.

The subprocess is built per provider (for example `anthropic` → `claude --dangerously-skip-permissions`, `openai` → `codex exec`). It runs in the agent's configured `working_directory` (or your home directory), with the agent's `AGENTS.md` injected as an appended system prompt.

### Teams

You can address a single agent (`@coder fix the login bug`) or a **team** (`@dev …`). A team has a leader and members. The orchestrator invokes the leader first; teammate mentions in the leader's response drive either a sequential handoff or a **parallel fan-out**. Team chains are bounded by a maximum depth of `10`.

CLI agent subprocesses share a concurrency limit across direct delegations and team runs. The default is **5** running subprocesses per gateway. Set `agents.maxConcurrentCliAgents` in `~/.flowly/config.json` to change it (1–64); additional team members wait for a slot, and their results retain the team's mention order.

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
