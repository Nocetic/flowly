---
title: Bots
eyebrow: Features
description: Separate agents on one machine — each with its own keys, memory, skills, and permissions. What a bot is, how one is made, and what it never inherits.
---

A **bot** is a second agent living on your machine, beside the one you already
have. It has its own memory, its own keys, its own skills and its own
permissions. Nothing it learns reaches your main agent, and nothing your main
agent knows reaches it.

You might keep one for work and one for personal life. One that can run shell
commands and one that cannot. One with your calendar connected and one with
nothing connected at all.

> [!NOTE]
> A bot is a **profile** with a face. The isolation is the same mechanism
> described in [Profiles](/docs/using-flowly/profiles) — a separate
> `FLOWLY_HOME` directory. Desktop and iOS present that mechanism as bots you
> can name, colour and talk to; the CLI presents it as `--profile`. They are
> the same thing seen from two sides.

## What a bot is not

Flowly uses the word *agent* for three different things, and only one of them
is a bot. They are easy to mix up because Desktop puts two of them under the
same tab.

| | What it is | Lives for |
|---|---|---|
| **A bot** | A second Flowly, with its own keys, memory and permissions | As long as you keep it |
| **A subagent** | A helper the agent spawns inside itself for one focused task | One task |
| **A CLI agent** | An external coding tool — Claude Code, Codex, Gemini — that Flowly hands work to | One job |

A bot is the only one of the three that is yours to name, configure and talk
to. The other two are things an agent reaches for while it works; see
[Delegation](/docs/features/delegation) for those.

## What a bot has of its own

Every bot gets its own directory under `~/.flowly/profiles/<name>/`, holding:

| Folder | What lives there |
|---|---|
| `workspace/` | Files the bot works in, plus its memory, personas and skills |
| `sessions/` | Its conversations |
| `credentials/` | Its own tokens and keys |
| `skills/` | Skills installed for this bot only |
| `audit/` | What it did, recorded |
| `logs/`, `trajectories/`, `subagents/`, `screenshots/`, `media/`, `cron/` | Its runtime state, files and schedules |

Its settings, its search index, its memory store and its knowledge graph sit
alongside those as files in the same directory.

Nothing in that list is shared. Two bots on the same machine cannot read each
other's conversations, keys or memory.

> [!NOTE]
> [Profiles](/docs/using-flowly/profiles#whats-isolated-per-profile) carries the
> full list with the exact path of every file, if you need to find one on disk.

## Naming

A bot's name must be lowercase letters, digits, dashes or underscores, start
with a letter or digit, and be at most 64 characters. These names are
reserved and cannot be used: `flowly`, `default`, `test`, `tmp`, `root`,
`sudo`.

The name is the bot's identity on disk and never changes. What you see in the
app is its **display name**, which you can change whenever you like.

## What a bot looks like

Each bot carries a generated mark — a woven knot drawn from its name and a
seed allocated when it was created — and a colour. The mark is unique to that
bot and stays with it, which is what lets you tell two bots apart at a glance
in a group, in the sidebar, or beside a message.

Your main agent is the exception: it wears the Flowly symbol rather than a
generated knot.

## Creating a bot

You can create a bot empty, or clone an existing one to inherit its setup —
its provider, model, persona and skills.

### What a clone never inherits

A clone inherits a **setup**, never a **self**. When you copy a bot, Flowly
removes the things that would make two agents claim to be the same one:

- **Channel connections are removed entirely.** Telegram, Discord, Slack and
  the rest are dropped from the copy, not merely switched off. Leaving the
  tokens on disk would put a copy of your account's credentials in every bot
  you ever made, and rotating the original would reach none of them.
- **The gateway token is cleared.** Two gateways answering to one token are not
  sharing a secret; they are two processes each believing they are the
  installation.
- **The hosted-relay registration is removed.** A relay registration names one
  install. Your account-scoped provider key keeps working without it.
- **`.env` is filtered.** Only provider API keys survive the copy:
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
  `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`,
  `ZAI_API_KEY`, `ZHIPU_API_KEY`, `ZHIPUAI_API_KEY`, `VLLM_API_KEY`.
  Everything else is dropped.

This runs for **every** clone, from any surface. There is no kind of bot that
legitimately needs the identity of the one it was copied from.

### What a clone does not copy either

By default a clone takes the **setup** and nothing that happened. The original
bot's conversations, its memory, the pictures it made and its record of what it
did all stay where they are. The new bot starts with your settings and no past.

You can ask for the past as well — `--clone-all` on the command line copies
sessions, memory, generated media and the audit log too. Credentials are still
never copied. Worth thinking about before you use it: the copy then knows
everything the original knew, including anything private that ended up in its
memory.

> [!WARNING]
> Because channels are removed, a cloned bot cannot answer on Telegram or any
> other channel until you connect it yourself. That is deliberate: without it,
> two bots would reply to the same message.

## Credentials

A named bot is created with an **isolated** credential policy: it uses its own
keys, kept in its own `credentials/` folder. Your main agent keeps the
`primary` policy and its own separate set.

## Model and provider

Each bot chooses its own provider and model, independently of every other bot
and of your main agent. One can run on a fast, cheap model while another runs
on the most capable one you have access to.

> [!NOTE]
> The model you choose for a bot is not rewritten behind your back. If Flowly
> cannot confirm a model against its catalogue, it leaves your choice alone
> rather than replacing it with one it recognises.

## What a bot can do on its own

A bot is a whole agent, not a chat window with a different name. Each one has
its own:

- **Conversations**, listed and deleted per bot, each able to override the
  model for that conversation alone
- **Scheduled jobs** — its own cron entries, which it can list, add, change,
  remove and run, with their output visible while they run
- **Goals** it is working towards, which you can pause, resume or stop
- **Plan mode**, on or off for that bot
- **Approvals** it is waiting on, and questions it has asked you
- **Tools** it may reach
- **MCP servers** connected to that bot alone
- **Its own audit trail**, in its own directory

So a bot you set up to watch something overnight keeps its schedule, its goal
and its history to itself. Nothing about it appears in another bot's list.

## Permissions

What a bot may do is decided per bot, not once for the whole installation.

**Shell and code execution** — how much a bot may run:

| Setting | Meaning |
|---|---|
| `deny` | It runs nothing |
| `allowlist` | It runs only what you listed |
| `full` | It runs what it decides to run |

**When it asks you first**: never, only when a command is not on the
allowlist, or every single time.

**Codex execution** has its own approval setting — ask on request, never ask,
auto-review, or granular — and its own sandbox: read-only, write inside the
workspace, or full access.

A bot you made for drafting text can be left with nothing but the model. A bot
you made for real work on your machine can be given more, without that
decision touching anything else.

## Running and stopping

Bots start when they are needed and stop when they are not. A bot with an
active turn cannot be reconfigured or deleted until that turn finishes or is
stopped — the app says so rather than changing settings underneath a running
answer.

## Limits

| | |
|---|---|
| Bots per installation | 15, beside your main agent |
| Name | Lowercase letters, digits, `-` and `_`; 1–64 characters |
| Reserved names | `flowly`, `default`, `test`, `tmp`, `root`, `sudo` |

When you reach the cap, creating another asks you to delete one first rather
than failing silently. Counting and creating happen under one lock shared
across processes, so Desktop and the CLI cannot race past the limit together.

## Where a bot comes from and where it goes

A bot is built in a hidden temporary directory and published in one step. A
crash before that step leaves the temporary directory behind and never a
half-made bot.

Deleting one is two steps — prepare, then commit — so a bot is never removed
while it is mid-answer, and a delete that fails partway does not leave a bot
that half exists.

Deleting a bot also reaches the [groups](/docs/features/bot-groups) it was in.
A group of three loses that member and carries on. A group of **two** is
deleted with it, transcript and attachments included, because a group needs
two members to exist.

## Backing it up, sharing it, moving it

A bot can be written out to a file, and there are two kinds of file. The
difference is **whether your keys are in it**.

| | What it contains | Use it to |
|---|---|---|
| **Template** *(default)* | Everything **except credentials** | Give your setup to somebody else |
| **Backup** | Everything, **encrypted with a password** | Keep a copy, or move a bot to another machine |

**A template** is what to send someone. It carries the setup — persona,
skills, model choice — and none of your keys. It is the safe one to share, and
it is what you get unless you ask for a backup.

**A backup** is locked with a password you choose, between 12 and 1,024
characters. The password is never stored, not even in a form that could check
it — lose it and the backup cannot be opened by anyone, including you. The
unencrypted copy exists only for a moment in a private temporary folder and is
removed before the file is handed to you, and the finished file is readable
only by your user account. Backups end in `.flowly-backup`.

> [!NOTE]
> Flowly never hands you a complete copy of a bot in the clear. A file with
> your keys in it is always encrypted first; the plaintext exists only inside
> the export, for as long as it takes to encrypt it.

> [!NOTE]
> A bot must be stopped before it can be exported. An archive taken from a
> running bot could catch it mid-write.

Two things never travel. The note of which process currently holds the bot
stays behind, because it describes this machine and not the bot. And a
shortcut pointing outside the bot's own folder is refused rather than
followed, so an archive can never quietly pick up data from elsewhere on your
disk.

### Bringing one back

Importing accepts all three kinds of file, and asks which of two things you
mean:

- **As a new bot** (the default) — it gets a fresh identity. Use this when you
  are adding a copy alongside the original, so the two never get mistaken for
  each other.
- **As a restore** — it keeps its original identity. Use this when you are
  putting back the bot you had. If a bot with that identity already exists,
  the import is refused rather than creating two bots claiming to be one.

## From the command line

Everything above is available without the app. Bots are `profile` subcommands,
because a bot and a profile are the same thing (see
[Profiles](/docs/using-flowly/profiles)).

```bash
flowly profile list                    # every bot on this machine
flowly profile describe work           # one bot's details
flowly profile settings work           # what it is configured with
```

Every command takes `--json` when you want to read the output from a script
rather than with your eyes.

There is one maintenance command you are unlikely to need:
`flowly profile backfill-marks` gives a generated mark to bots made before
marks existed. Those bots draw a mark from their name instead, which spreads
colour no better than chance; this gives them a proper one. It is safe to run
at any time and does nothing when there is nothing to fix.

### Making one

```bash
# Empty
flowly profile create work --display-name "Work"

# Copy an existing bot's setup
flowly profile create work --clone-from personal --display-name "Work"

# Choose its brain up front
flowly profile create work --provider anthropic --model claude-haiku-4.5

# Give it a character
flowly profile create work --soul "You draft in a formal register."
```

Useful flags:

| Flag | Does |
|---|---|
| `--clone-from <bot>` | Copy that bot's setup — never its credentials |
| `--clone` | Copy the bot you are currently using |
| `--clone-all` | Also copy sessions, memory, generated media and the audit log |
| `--display-name`, `--description` | What clients show |
| `--provider`, `--model` | Its provider and default model |
| `--soul` | Its character, as text |
| `--mark-text`, `--mark-tone` | Override the generated mark and colour |
| `--local-only` | Strip messaging transports and relay identity, for a bot managed on this machine |
| `--json` | Machine-readable output |

### Changing and removing one

```bash
flowly profile configure work --model claude-sonnet-5
flowly profile delete work --yes
```

Deleting asks for `--yes` because it is permanent.

### Backing up and moving

```bash
# Shareable template — credentials removed. This is what you get by default.
flowly profile export work --output ~/work-template

# Complete and encrypted. Asks for a password, then asks again to confirm it.
flowly profile export work --output ~/work --backup

# Bring one back
flowly profile import ~/work.flowly-backup
flowly profile import ~/work-template.tar.gz --name work2
flowly profile import ~/work.flowly-backup --restore-identity
```

`--restore-identity` keeps the archived bot's original identity, and fails if a
bot with that identity already exists — so a restore can never produce two bots
claiming to be one. Without it, the import arrives as a new bot.

Add `--local-only` to an import to strip messaging transports from whatever
you are bringing in.

## Talking to several at once

Two or more bots can share one conversation — a **group**. See
[Bot groups](/docs/features/bot-groups).
