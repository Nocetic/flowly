---
title: Plugins
eyebrow: Features
description: A plugin is a Python package that extends Flowly at runtime — a directory with a plugin.yaml manifest and an __init__.py exposing a register(ctx) function. Authors interact only through the PluginContext facade.
---

> [!NOTE]
> Plugins (code) are a separate system from the hub **skill** registry. `flowly plugins install` clones code plugins; `flowly skills install` fetches markdown skills. See [Skills](skills.md).

## Manifest format

`plugin.yaml` (probed in order `plugin.yaml → plugin.yml → plugin.json`):

| Field | Notes |
| --- | --- |
| `name` | Defaults to the directory name. |
| `version` | Plugin version. |
| `description` | Shown in `plugins list`. |
| `author` | Author. |
| `kind` | Default `standalone`. Valid: `standalone`, `backend`, `exclusive`. |
| `manifest_version` | Current/max supported = `1`. |
| `requires_env` | Declared environment requirements. |
| `provides_tools` | Descriptive only — shown in `plugins list`. |
| `provides_hooks` | Descriptive only — shown in `plugins list`. |

`provides_tools` and `provides_hooks` are advisory; the actual contributions happen inside `register()`.

> [!WARNING]
> v1 loads only `kind: standalone`. `backend` and `exclusive` manifests parse but are skipped with a recorded reason (visible in `plugins list`). Unknown kinds coerce to `standalone` with a warning.

## Discovery

Plugins are discovered from three sources. On a name collision, later sources override earlier (project > user > bundled):

| Source | Location | Activation |
| --- | --- | --- |
| Bundled | shipped with the package | On by default (unless disabled). |
| User | `$FLOWLY_HOME/plugins/<name>/` | Opt-in: list the name in `plugins.enabled`. |
| Project | `./.flowly/plugins/<name>/` | Opt-in: set env `FLOWLY_ENABLE_PROJECT_PLUGINS=1`. |

> [!NOTE]
> Discovery is a depth-1 scan. A failing `register()` disables only that plugin. There is **no hot reload** — install/enable/disable all require a restart.

## What a plugin can contribute

Through `PluginContext`, a plugin can register:

- **Tools** — `register_tool(name, schema, handler, …)` adds a function to the live tool registry.
- **Lifecycle hooks** — `register_hook(hook_name, callback)` subscribes to any of **14** lifecycle events:

  | | | |
  | --- | --- | --- |
  | `pre_tool_call` | `post_tool_call` | `transform_tool_result` |
  | `transform_terminal_output` | `pre_llm_call` | `post_llm_call` |
  | `pre_api_request` | `post_api_request` | `on_session_start` |
  | `on_session_end` | `on_session_finalize` | `on_session_reset` |
  | `subagent_stop` | `pre_gateway_dispatch` | |

  Unknown event names are accepted with a warning.

- **Slash commands** — `register_command(name, handler, description, args_hint)` adds an in-session slash command usable across all channels. The names `new`, `clear`, `compact`, and `help` are reserved and rejected.
- **Namespaced skills** — `register_skill(name, path, description)` registers a skill resolvable only as `"<plugin>:<name>"` via `skill_view`. It does **not** enter the flat `~/.flowly/skills/` index and is explicit-load-only.

## Bundled plugin

> [!NOTE]
> Exactly **one** plugin ships bundled: `disk-cleanup`.

- **`disk-cleanup`** (`kind: standalone`, hooks `post_tool_call` + `on_session_end`). It auto-tracks ephemeral session files (test/temp outputs) created by `write_file`/`edit_file`/`exec` under `$FLOWLY_HOME` and `/tmp/flowly-*` via a `post_tool_call` hook, then runs a quick cleanup at `on_session_end`. It registers a `/disk-cleanup` slash command with subcommands `status | dry-run | quick | track | forget`. All file operations are best-effort and never break the agent loop.

## CLI: `flowly plugins`

| Command | Description |
| --- | --- |
| `flowly plugins list [--json]` | List discovered plugins and their load status. |
| `flowly plugins install <git-url \| owner/repo \| owner/repo/subpath \| local-path> [--enable/--no-enable] [--force]` | Clone/copy into `$FLOWLY_HOME/plugins/`. |
| `flowly plugins enable <name>` | Add to `plugins.enabled`. |
| `flowly plugins disable <name>` | Add to `plugins.disabled`. |
| `flowly plugins remove <name> [-y]` | Remove the plugin directory and clean up config. |

```bash
flowly plugins list
flowly plugins install owner/repo --enable
flowly plugins disable disk-cleanup
flowly plugins remove disk-cleanup -y
```

`install` supports a monorepo subpath via `owner/repo/path` or `#fragment` (not both), with path-traversal guards. Install/enable/disable all print **"Restart flowly for changes to apply."**

## Configuration

Plugin activation is controlled by two keys in `~/.flowly/config.json`:

| Key | Type | Meaning |
| --- | --- | --- |
| `plugins.enabled` | `list[str]` | User plugins to load. |
| `plugins.disabled` | `list[str]` | Plugins to skip. |

`disabled` overrides `enabled` and applies even to bundled plugins. Bundled plugins load by default unless disabled; user plugins load only when listed in `enabled`.

## Slash command

`/plugins` works in-chat.

> [!WARNING]
> Some `PluginContext` methods (`register_image_gen_provider`, `register_context_engine`, `register_cli_command`, `inject_message`, `dispatch_tool`) are deferred and not wired in v1. Whether manifest-level `requires_env` gates loading has not been verified.

## Authoring

To write a plugin, see the full authoring guide at [PLUGINS.md](../../../PLUGINS.md).

## Related

- [Skills](skills.md)
- [Skill bundles](skill-bundles.md)
- [Feature overview](overview.md)
- [Slash commands reference](../reference/slash-commands.md)
- [CLI commands reference](../reference/cli-commands.md)
- [PLUGINS.md (authoring)](../../../PLUGINS.md)
