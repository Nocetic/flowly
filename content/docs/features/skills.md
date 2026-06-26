---
title: Skills
eyebrow: Features
description: A skill is a directory containing a SKILL.md "recipe" with YAML frontmatter and a markdown body that teaches the agent how to perform a task or use a tool. Flowly ships 135 builtin skills, and you can install more, drop them into your workspace, or have the agent author its own.
---

## SKILL.md format

```yaml
---
name: weather
description: Get current weather and forecasts (no API key required).
homepage: https://wttr.in/:help
metadata: {"flowly":{"emoji":"🌤️","requires":{"bins":["curl"]}}}
---
```

The `metadata` field is a JSON string carrying a `flowly` object. Recognized `flowly` keys:

| Key | Purpose |
| --- | --- |
| `requires.bins` | List of binaries that must be on `PATH` for the skill to be available. |
| `requires.env` | List of environment variables that must be set. |
| `os` | Restrict the skill to specific operating systems. (Only `os` is honored — a `platforms` key is accepted but not read.) |
| `always` | If truthy (and requirements met), the full body is always inlined into the prompt. |
| `requires_tools` | Hide the skill unless **all** listed tools are available. |
| `fallback_for_tools` | Hide the skill when **any** listed tool **is** available (use as a fallback). |
| `emoji` | Display emoji. |
| `install` | Install hint. |
| `related_skills` | Related skill names. |
| `tags` | Free-form tags. |

## Discovery and priority

Skills are loaded from three sources. Higher-priority sources shadow same-named skills in lower ones:

| Priority | Source | Location |
| --- | --- | --- |
| 1 (highest) | Workspace | `<workspace>/skills/` |
| 2 | Managed (hub-installed) | `~/.flowly/skills/` |
| 3 (lowest) | Builtin | bundled with the package |

The managed root honors the active profile. With a non-default profile it resolves to `$FLOWLY_HOME/skills`.

## Progressive disclosure

The agent does not see full skill bodies up front. Skills are disclosed in three tiers:

1. **Index** — a compact `<skills>` block (name, truncated description, location, `available` flag, `requires`) is injected into the system prompt. The agent is instructed to load a match with `skill_view(name)`.
2. **Full body** — `skill_view(name)` returns the parsed frontmatter, body, linked-file list, and readiness.
3. **Linked files** — `skill_view(name, file_path="references/…")` loads a single supporting file. Access is gated to the allowed subdirs and extensions with path-traversal protection.

**Always-on skills**: a skill whose `flowly.always` (or top-level `always`) is truthy **and** whose requirements are met has its full body inlined into the prompt under `# Active Skills`. An always-skill with unmet `requires` is silently dropped.

**Conditional activation**: `requires_tools` and `fallback_for_tools` filter the index against the live tool set, so skills appear or disappear depending on which tools are currently available.

## Agent-facing tools

| Tool | Capability |
| --- | --- |
| `skill_view` | Read-only. Loads and discloses a skill body (tier 2) or a linked file (tier 3). Also resolves plugin-namespaced skills as `plugin:name`. |
| `skill_manage` | Create / patch / edit / delete / list / write_file / remove_file / archive / restore for agent-authored skills under `~/.flowly/skills/<name>/`. |

`skill_manage` validates the skill name against `^[a-z0-9][a-z0-9_-]{0,63}$`, requires a `description` in frontmatter, scans content for prompt injection, and uses atomic writes. Supporting files are capped at 1 MB and restricted to the four supporting subdirs.

## CLI: `flowly skills`

A thin alias over the hub:

| Command | Description |
| --- | --- |
| `flowly skills list [--all]` | List installed skills. |
| `flowly skills install <source> [--force]` | Install a skill from the hub. |
| `flowly skills remove <skill>` | Remove an installed skill. |
| `flowly skills search <query>` | Search the hub registry. |

```bash
flowly skills list --all
flowly skills search weather
flowly skills install weather
flowly skills remove weather
```

### `flowly-hub` (full registry CLI)

For more registry operations, use `flowly-hub`:

| Command | Description |
| --- | --- |
| `search <query>` | Search the registry. |
| `install <source> [--force] [--workspace]` | Install a skill (optionally into `<workspace>/skills/`). |
| `update [--all] [--force]` | Re-install from the recorded source. |
| `remove <slug> [--workspace]` | Remove an installed skill. |
| `list [--all]` | List installed skills. |
| `info <slug>` | Show skill details. |
| `check` | List available updates. |
| `create <name>` | Scaffold a `SKILL.md` template with `scripts/` and `references/`. |
| `publish` | Points to the web publish flow. |

## Hub registry and install sources

The hub is the **skill** registry — distinct from the [plugin system](plugins.md).

- **Registry URL**: defaults to `https://useflowlyapp.com`, overridable via the `FLOWLY_HUB_REGISTRY` environment variable.
- **Install target**: managed `~/.flowly/skills/` by default, or `<workspace>/skills/` with `--workspace`.
- **Install sources**:
  - Registry slug, optionally `slug@version`.
  - `github:owner/repo/skill[@branch]` — fetched from `raw.githubusercontent.com`.
  - A direct `http(s)://…` URL to a single `SKILL.md`.
  - A local path (`./`, `/`, or `~`), copied in.

Each installed skill records a `.flowly-skill.json` metadata file (slug, version, source, install time, hash). Local modifications are detected by comparing the stored hash; `update` skips locally-modified skills unless `--force`.

> [!WARNING]
> The live `useflowlyapp.com/api/skills*` endpoints and their exact response schema have not been verified against a running server — they are inferred from the client code. Treat registry availability as best-effort. GitHub installs fetch only `SKILL.md`; bundled `scripts/`/`references/` directories on GitHub skills may not be downloaded.

## Create skills with `/learn`

`/learn` turns a source you describe into a reusable skill. It is a chat slash
command, so it works from the normal agent flow instead of a separate scaffold
command: the agent inspects the material, decides whether to create a new skill
or update an existing agent-authored skill, then writes the result through
`skill_manage`.

Use it when you have a repeatable workflow that should become part of the skill
library:

```text
/learn the workflow we just used to debug the Stripe webhook replay issue
/learn ~/work/internal-sdk/docs/auth.md
/learn https://example.com/api-guide and the notes below: ...
/learn --dry-run ./runbooks/release-checklist.md
```

If you run `/learn` with no source, Flowly treats the current conversation as
the source. That is useful right after the agent finishes a task and you want it
to preserve the reusable procedure, tool sequence, edge cases, and verification
step.

### What `/learn` does

1. **Inspects the source material.** Local files and directories are read with
   read-only tools; exact URLs use web fetch; discovery can use search; "what we
   just did" uses the current conversation.
2. **Checks for duplicates.** The agent lists existing agent-authored skills
   before creating a new one, so it can update a close match instead of adding a
   near-duplicate.
3. **Writes through `skill_manage`.** New skills use `create`, existing skills
   use `patch` or `edit`, and larger supporting material goes into
   `references/`, `scripts/`, `templates/`, or `assets/` via `write_file`.
4. **Verifies the saved skill.** The agent checks that the name, description,
   workflow, prerequisites, edge cases, and verification section are present.
5. **Reports what changed.** The final reply includes the skill name, whether it
   was created or updated, where it was saved, and what workflow it captured.

The command creates **skills**, not plugins. Plugins are Python packages that
can add tools, hooks, channels, or code-backed slash commands; `/learn` writes
Markdown skill instructions and optional supporting files.

### Skill shape produced by `/learn`

Flowly asks the agent to follow the same constraints as hand-authored skills:

| Area | Rule |
| --- | --- |
| Name | Lowercase, directory-safe, hyphenated, no spaces, max 64 characters. |
| Frontmatter | `SKILL.md` must include `name` and a specific `description`. |
| Body | Keep the main workflow focused: triggers, prerequisites, steps, edge cases, and one verification check. |
| Supporting files | Put bulky examples, schemas, templates, API notes, or scripts under `references/`, `scripts/`, `templates/`, or `assets/`. |
| Tool names | Prefer Flowly-native tool names such as `read_file`, `list_dir`, `web_fetch`, `web_search`, `exec`, `skill_view`, and `skill_manage`. |
| Uncertainty | Do not invent flags, endpoints, credentials, paths, or APIs. Unknown details should stay explicit decision points. |

### Dry run

Add `--dry-run` to preview a skill without writing anything:

```text
/learn --dry-run ~/work/internal-sdk/docs/auth.md
/learn -n the workflow from this conversation
```

Dry run mode still lets the agent inspect sources and list existing skills, but
it must not call persistent `skill_manage` actions such as `create`, `patch`,
`edit`, `write_file`, `remove_file`, `archive`, `restore`, or `delete`.

The dry-run reply should include:

- The proposed skill name.
- Whether it would create a new skill or update an existing one.
- The full `SKILL.md` draft.
- Any supporting file drafts with relative paths.
- A verification check to run after applying it.
- The follow-up `/learn ...` command to apply the same source for real.

Use dry run when the source is large, comes from an external page you have not
reviewed, overlaps with existing skills, or would affect a workflow you rely on.
Once the draft looks right, run the same command without `--dry-run`.

### Refresh after creation

When a skill is created or updated by `skill_manage` or `skill_improve` in the
active TUI session, the slash command palette refreshes automatically. You do
not need to restart the gateway just to see the new skill in autocomplete.

## Slash commands

`/skills` is a builtin in-chat slash command for listing/working with skills.
`/learn [--dry-run] [source]` creates or updates a reusable skill from paths,
URLs, notes, or the current conversation. You can also load a single skill inline
by typing `/<skill-name>`, which expands that skill's body for the current turn.
See [Skill bundles](skill-bundles.md) for grouping skills under one `/slug`.

## Related

- [Skill bundles](skill-bundles.md)
- [Skill self-improvement](skill-self-improvement.md)
- [Plugins](plugins.md)
- [Feature overview](overview.md)
- [Slash commands reference](../reference/slash-commands.md)
- [CLI commands reference](../reference/cli-commands.md)
