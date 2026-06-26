---
title: Workspace & context files
eyebrow: Using Flowly
description: A handful of Markdown files in your workspace shape every conversation — who the agent is, who you are, and how it should behave. Edit them by hand or let the agent maintain them.
---

## The workspace

Flowly keeps its working state in a **workspace** under `~/.flowly/workspace/`.
Alongside your [memory](/docs/features/memory) and [skills](/docs/features/skills),
it holds a small set of **context files** — plain Markdown that is injected into
the system prompt on every turn, so the agent always reads them before it acts.

`flowly setup` / `flowly bootstrap` create these with sensible starters; you can
edit them any time and the change takes effect on the next turn (no restart
needed for prompt-level files).

### Where it lives, and the runtime cwd

The workspace defaults to `~/.flowly/workspace` and is configurable via
`agents.defaults.workspace` in `config.json`. It's the home for the context
files, [memory](/docs/features/memory), and [skills](/docs/features/skills).

Don't confuse the **workspace** with the agent's **runtime working directory** —
the folder the shell and file tools actually operate in. Those are resolved
*separately*: the runtime cwd comes from `--cwd` / the `FLOWLY_CWD` environment
variable (or config), not from the workspace. So the agent reads its standing
context from the workspace while running commands and editing files in whatever
project you've pointed it at. See [Sandbox and approvals](./sandbox-and-approvals.md)
for the full runtime-cwd resolution chain.

## The files

| File | Role |
| --- | --- |
| `AGENTS.md` | **Standing instructions.** How the agent should work in this workspace — tone, guidelines, do's and don'ts. The main file you'll edit. |
| `SOUL.md` | **Personality base.** The agent's character. Empty by default; a [persona](/docs/using-flowly/personas) layers additively on top of it. |
| `USER.md` | **Your profile.** Durable facts about you the agent should always know (name, role, preferences, environment). Onboarding offers to fill it; the agent appends to it as it learns. |
| `TOOLS.md` | **Tool notes.** Optional workspace-specific guidance on how to use particular tools. |
| `IDENTITY.md` | **Identity overrides.** Optional; advanced identity tweaks. |

All five are injected (in that order) ahead of memory and skills. They're
optional — a missing file is simply skipped.

## AGENTS.md — the one you'll actually edit

`AGENTS.md` is your standing brief. Put anything here that should hold across
*every* conversation:

```markdown
# Agent Instructions

You are my engineering copilot. Be concise and direct.

## Guidelines
- Prefer running a tool over describing what you'd do.
- When you touch code, run the tests before claiming it works.
- My projects use pytest and ruff; match existing style.
```

This is the right home for **durable working rules**. One-off task details don't
belong here — those live in the conversation; recurring procedures belong in
[skills](/docs/features/skills); facts about you belong in `USER.md` or
[memory](/docs/features/memory).

## USER.md vs memory

- **`USER.md`** is the curated, human-readable profile — stable facts you (or the
  agent) wrote down deliberately.
- **Memory** is the governed, automatically-maintained store with confidence
  scores and a lifecycle.

They complement each other: `USER.md` is the always-loaded baseline; memory adds
the things Flowly learns and grooms over time.

## Security

Context files are scanned for prompt-injection payloads before they're injected.
A flagged file is replaced with a `[BLOCKED: …]` placeholder (so you can see and
fix it) rather than silently dropped — a poisoned `SOUL.md`/persona can't hijack
the agent at turn zero.

## Isolation

Cron jobs and some scheduled runs are built with context files **skipped**, so a
background task doesn't inherit your persona or profile and pollute its output.
This is automatic; you don't configure it.
