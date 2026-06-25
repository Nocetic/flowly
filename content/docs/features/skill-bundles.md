---
title: Skill bundles
eyebrow: Features
description: A skill bundle is a YAML file that names a group of skills to load together under a single /slug slash command, letting you compose a reusable workflow and trigger it with one command.
---

Bundles live in `~/.flowly/skill-bundles/` (profile-scoped: `$FLOWLY_HOME/skill-bundles/` under a non-default profile).

## File format

`~/.flowly/skill-bundles/<slug>.yaml`:

```yaml
name: Research Tools                  # → slug "research-tools"; defaults to file stem
description: "Web research workflow"  # optional
instruction: "Always cite sources"    # optional, appended to every invocation
skills:                               # required, non-empty list of skill names
  - web-search
  - arxiv
```

| Field | Required | Notes |
| --- | --- | --- |
| `name` | No | Becomes the `/slug` (case-insensitively canonicalized). Defaults to the file stem. |
| `description` | No | Shown in the composed header. |
| `instruction` | No | Appended as a footer on every invocation. |
| `skills` | Yes | Non-empty list of skill names (strings). |

> [!NOTE]
> Missing/empty `skills`, malformed YAML, or a non-mapping root cause the file to be skipped with a warning — never a crash. Duplicate bundle slugs keep the first scanned and warn.

## How `/slug` works

When a user turn begins with `/`, Flowly tries to expand it. Resolution order is **bundle first, then individual skill** — a `/slug` resolves to a same-named bundle before a same-named skill.

A bundle match rewrites the message into a single composed payload for that turn:

1. A header: `[BUNDLE] name — description` plus the list of loaded skills.
2. Each referenced skill's stripped body, joined by `---`.
3. The bundle `instruction` footer (if set).
4. A `[Note]` listing any skills that could not load.
5. The user's original input as `[Task]`.

Bundles are **stateless**: the composed content is injected for that one turn only. Missing or unavailable skills are skipped, not fatal.

A bare `/<skill-name>` (no matching bundle) instead expands a single skill and surfaces its supporting files, telling the agent to call `skill_view(name, file_path=…)` for detail.

> [!IMPORTANT]
> Bundle **creation** does no name check — `flowly bundles create` will happily write a bundle whose slug collides with a built-in command, another bundle, or a plugin command. The reserved-name guard works the other way: at expansion time it stops a **skill** from shadowing a built-in command or a bundle, so the bundle (resolved first) always wins.

## CLI: `flowly bundles`

| Command | Description |
| --- | --- |
| `flowly bundles list` | List defined bundles. |
| `flowly bundles show <slug>` | Show a bundle's skills and metadata. |
| `flowly bundles create <name> [-s SKILL …] [-d DESC] [-i INSTRUCTION] [--force] [--interactive]` | Create a bundle file. |
| `flowly bundles delete <slug> [-y]` | Delete a bundle. |
| `flowly bundles reload` | Force-drop the bundle cache and re-read the directory. |

```bash
flowly bundles create research-tools -s web-search -s arxiv -d "Web research workflow"
flowly bundles list
flowly bundles show research-tools
flowly bundles reload
flowly bundles delete research-tools -y
```

Bundle files are cached by an mtime fingerprint over the directory and each `*.yaml`; the cache refreshes automatically on change, and `reload` forces it.

## Invoking and managing bundles

There is no `/bundles` slash command. Trigger a bundle from any channel with `/<bundle-slug>`; manage them with the `flowly bundles` CLI above.

## Related

- [Skills](skills.md)
- [Plugins](plugins.md)
- [Feature overview](overview.md)
- [Slash commands reference](../reference/slash-commands.md)
- [CLI commands reference](../reference/cli-commands.md)
