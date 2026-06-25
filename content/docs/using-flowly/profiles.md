---
title: Profiles
eyebrow: Using Flowly
description: Run multiple isolated Flowly setups — separate config, keys, sessions, and memory — and switch between them with one flag.
---

## What a profile is

A **profile** is a fully self-contained Flowly environment. Each profile has its own config file, its own API keys and credentials, its own sessions and memory, and its own skills — none of it shared with any other profile. Switching profiles is like switching to a different, completely separate Flowly install, except everything still lives under one home directory and you select between them with a single flag.

Under the hood there is exactly one mechanism: the `FLOWLY_HOME` directory. Every path in Flowly resolves from `FLOWLY_HOME`, so pointing it at a different directory gives you a different, isolated instance. A profile is just a named `FLOWLY_HOME`.

> [!NOTE]
> Profiles are about **isolation on one machine**, not multi-user accounts. Each profile is its own data directory; they never read each other's config, keys, sessions, or memory.

## Default vs named profiles

There are two kinds of profile:

- **Default** — lives at `~/.flowly`. This is what you get when you run `flowly` with no profile selected. It is the original, backward-compatible location.
- **Named** — lives at `~/.flowly/profiles/<name>/`. For example, a profile called `work` lives at `~/.flowly/profiles/work/`.

```
~/.flowly/                      ← "default" profile
~/.flowly/profiles/work/        ← named profile "work"
~/.flowly/profiles/personal/    ← named profile "personal"
~/.flowly/active_profile        ← sticky default profile name (a one-line text file)
```

A named profile directory holds the same set of files and folders the default profile does — its own `config.json`, `sessions/`, `credentials/`, `skills/`, and the memory and session databases — all kept separate from every other profile.

> [!NOTE]
> Profile names use lowercase letters, digits, hyphens, and underscores (up to 64 characters). A handful of names are reserved and can't be used, including `default`, `flowly`, `test`, `tmp`, and `root`.

## How to select a profile

Flowly resolves which profile to use **before it loads anything else**, in this priority order. The first one that matches wins:

1. **The `-p` / `--profile` flag** — highest priority, per-command.
2. **The `FLOWLY_PROFILE` environment variable** — for wrapper scripts and shells.
3. **The sticky `~/.flowly/active_profile` file** — a persisted default.
4. **`default`** — the fallback when nothing else is set.

### 1. The `-p` / `--profile` flag

Put the flag before your command. It applies to that one invocation only:

```bash
flowly -p work chat
flowly --profile work chat
flowly --profile=work chat
```

All three forms are equivalent. This is the most explicit way to pick a profile and it overrides everything else.

### 2. The `FLOWLY_PROFILE` environment variable

Set the variable and every `flowly` command in that shell uses the named profile, without repeating the flag:

```bash
export FLOWLY_PROFILE=work
flowly chat          # uses the "work" profile
flowly sessions      # also "work"
```

This is handy for a dedicated terminal tab, or for a small wrapper script:

```bash
#!/bin/sh
exec env FLOWLY_PROFILE=work flowly "$@"
```

> [!TIP]
> Save the script above as something like `work` on your `PATH` and `chmod +x` it. Then `work chat` always runs Flowly in the `work` profile — the env var is scoped to that one command, so your other terminals are unaffected.

### 3. The sticky `active_profile` file

`~/.flowly/active_profile` is a one-line text file holding a profile name. When you run plain `flowly` with no flag and no `FLOWLY_PROFILE`, Flowly reads this file and uses whatever profile it names. If the file is missing, empty, or contains `default`, you get the default profile.

```bash
# Make "work" the default for plain `flowly` (one line, no quotes):
echo work > ~/.flowly/active_profile

# Go back to the default profile:
rm ~/.flowly/active_profile
```

> [!NOTE]
> The `-p` flag and `FLOWLY_PROFILE` always win over `active_profile`. The sticky file only decides what plain `flowly` does when you haven't picked a profile any other way.

> [!TIP]
> Because the flag has the highest priority, you can leave `active_profile` set to your everyday profile and still reach any other one on demand — for example `flowly -p personal chat` even when `work` is the sticky default.

## What's isolated per profile

Selecting a profile changes `FLOWLY_HOME`, and **everything** Flowly stores resolves from there. Concretely, each profile keeps its own copy of:

| What | Path (relative to the profile home) | Notes |
| --- | --- | --- |
| Config | `config.json` | Settings and provider config; stored owner-only because it holds keys |
| Credentials / keys | `credentials/` | API keys and provider credentials, separate per profile |
| Sessions | `sessions/` | Chat session transcripts |
| Session index | `session_index.sqlite` | Search index over sessions |
| Memory | `memory_index.sqlite` | Long-term memory store |
| Knowledge graph | `knowledge_graph.sqlite3` | Entity / relationship graph |
| Skills | `skills/` | Installed skills for this profile |
| Subagents | `subagents/` | Subagent definitions |
| Workspace | `workspace/` | Persona, memory, and skill working files |
| Logs | `logs/` | Per-profile logs |
| Audit | `audit/` | Audit records |
| Cron | `cron/` | Scheduled jobs |
| Screenshots / media | `screenshots/`, `media/` | Captured artifacts |

For the **default** profile the home is `~/.flowly`, so these resolve to `~/.flowly/config.json`, `~/.flowly/sessions/`, and so on. For a **named** profile they live under `~/.flowly/profiles/<name>/` — for example `~/.flowly/profiles/work/config.json`.

> [!NOTE]
> Because keys and credentials are per profile, a profile only has access to the API keys you configure inside it. A leak or mistake in one profile can't reach another profile's credentials.

## Common uses

**Work vs personal.** Keep a `work` profile with your work API keys, work sessions, and work memory, and a separate `personal` profile for everything else. Each remembers its own context and bills to its own keys.

```bash
flowly -p work chat
flowly -p personal chat
```

**Testing and experiments.** Spin up a throwaway profile to try a risky config change, a new skill, or a different model without touching your real setup. If it goes wrong, the rest of your profiles are untouched.

```bash
export FLOWLY_PROFILE=sandbox
flowly chat
```

**A clean demo.** Use a dedicated profile when you want a predictable, empty-history environment to show Flowly to someone, so your personal sessions and memory stay private.

> [!TIP]
> Most things scoped to a profile — including the background gateway service — are name-aware, so running the same profile in different terminals stays consistent.

## Related

- [Configuration](./configuration.md)
- [Sessions](./sessions.md)
- [Environment variables](../reference/environment-variables.md)
