---
title: Plugins
eyebrow: Features
description: Extend Flowly at runtime with custom tools, lifecycle hooks, slash commands, and skills — without touching core code. A plugin is a directory with a plugin.yaml manifest and an __init__.py that exposes register(ctx). This page covers both using plugins and the full authoring API.
group: Extending Flowly
---

Plugins extend Flowly with custom **tools**, **lifecycle hooks**, **slash commands**, and **skills** without modifying core code. A plugin is a small directory containing two files — a manifest and an entry point — and authors interact with the runtime through a single facade object, `PluginContext`. A single plugin may register any combination of the four contribution types.

> [!NOTE]
> Plugins (code) are a separate system from the hub **skill** registry. `flowly plugins install` clones code plugins; `flowly skills install` fetches markdown skills. See [Skills](skills.md).

## When to write a plugin

| Goal | Use |
|---|---|
| Give the agent a new capability it can decide to invoke | **Tool** — `register_tool` |
| Run something automatically before/after every tool call, LLM call, or session | **Hook** — `register_hook` |
| Let the user trigger something with `/foo` from any channel | **Slash command** — `register_command` |
| Ship instructions the agent loads on demand | **Skill** — `register_skill` |

If your goal is just to package reusable *instructions* (no code), you probably want a [skill](skills.md), not a plugin.

## Plugin layout

```text
my-plugin/
├── plugin.yaml          # manifest (required)
└── __init__.py          # exposes register(ctx) (required)
```

That's the minimum. Add more `.py` files as needed and import them from `__init__.py` the usual way.

### `__init__.py`

```python
def register(ctx):
    """Plugin entry point. Called once at agent startup."""
    ctx.register_tool(...)
    ctx.register_hook(...)
    ctx.register_command(...)
    ctx.register_skill(...)
```

`ctx` is a `flowly.plugins.PluginContext` — the only surface a plugin touches. A failing `register()` disables **only that plugin**; the rest of the agent boots normally.

## Manifest format

`plugin.yaml` is probed first, then `plugin.yml`, then `plugin.json` — use whichever you prefer.

| Field | Notes |
|---|---|
| `name` | Unique key used in `plugins.enabled` / `plugins.disabled`. Defaults to the directory name. |
| `version` | Plugin version string. |
| `manifest_version` | Schema version; current and max supported is `1`. Bump when the schema changes. |
| `description` | One-liner shown in `flowly plugins list`. |
| `author` | Author / contact. |
| `kind` | Default `standalone`. See the warning below. |
| `requires_env` | Declared environment requirements (advisory in v1). |
| `provides_tools` | Descriptive only — shown in `plugins list`. |
| `provides_hooks` | Descriptive only — shown in `plugins list`. |

```yaml
name: my-plugin
version: 1.0.0
manifest_version: 1
description: "What it does, in one line."
author: "you@example.com"
kind: standalone
provides_tools:
  - my_tool
provides_hooks:
  - post_tool_call
```

`provides_tools` / `provides_hooks` are advisory — the real contributions happen inside `register()`, not the manifest.

> [!WARNING]
> v1 loads only `kind: standalone`. `backend` and `exclusive` manifests parse without error but are **skipped**, with the reason recorded (visible in `plugins list`). Unknown kinds coerce to `standalone` with a warning.

## Discovery & enablement

Plugins are discovered from three sources, scanned in order, **last writer wins** on a name collision (project > user > bundled):

| Source | Location | Activation |
|---|---|---|
| **Bundled** | `flowly/plugins_bundled/<name>/` (ships with the package) | **On by default** — loads unless listed in `plugins.disabled`. |
| **User** | `$FLOWLY_HOME/plugins/<name>/` (per-profile) | **Opt-in** — only loads when the name is in `plugins.enabled`. |
| **Project** | `./.flowly/plugins/<name>/` | **Opt-in** — set `FLOWLY_ENABLE_PROJECT_PLUGINS=1`; then treated like a user plugin. |

> [!NOTE]
> Discovery is a depth-1 scan that runs **once at startup**. There is **no hot reload** — install, enable, and disable all require a restart of `flowly` / the gateway.

`config.json` controls activation with two keys:

```json
{
  "plugins": {
    "enabled": ["my-plugin", "another"],
    "disabled": []
  }
}
```

| Key | Type | Meaning |
|---|---|---|
| `plugins.enabled` | `list[str]` | User/project plugins to load. |
| `plugins.disabled` | `list[str]` | Plugins to skip. |

`disabled` overrides everything, **including bundled plugins**.

## CLI: `flowly plugins`

| Command | Description |
|---|---|
| `flowly plugins list [--json]` | List discovered plugins and their load status. |
| `flowly plugins install <git-url \| owner/repo \| owner/repo/subpath \| local-path> [--enable/--no-enable] [--force]` | Clone/copy into `$FLOWLY_HOME/plugins/`. |
| `flowly plugins enable <name>` | Add to `plugins.enabled`. |
| `flowly plugins disable <name>` | Add to `plugins.disabled`. |
| `flowly plugins remove <name> [-y]` | Delete the plugin directory and clean up config. |

```bash
flowly plugins list
flowly plugins install owner/repo --enable
flowly plugins disable disk-cleanup
flowly plugins remove disk-cleanup -y
```

`install` supports a monorepo subpath via `owner/repo/path` or a `#fragment` (not both), with path-traversal guards. Install / enable / disable all print **"Restart flowly for changes to apply."** The `/plugins` slash command shows the same status in-chat.

## PluginContext API

The `ctx` passed to `register()` exposes four registration methods.

### `register_tool(name, schema, handler, *, check_fn=None, description="")`

Register a function-based tool. `handler` is sync or async. `schema` is either a JSON Schema dict for the parameters, or the OpenAI function schema with `parameters` inside.

```python
def register(ctx):
    async def lookup_weather(city: str) -> str:
        return await fetch(f"https://wttr.in/{city}?format=3")

    ctx.register_tool(
        name="weather",
        schema={
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        handler=lookup_weather,
        description="Look up current weather for a city.",
    )
```

If `check_fn` is provided it runs at **dispatch time** (not registration time). Returning `False` short-circuits with a clear error — useful when the tool needs an OAuth token or env var that may not exist yet:

```python
ctx.register_tool(
    ...,
    check_fn=lambda: os.environ.get("WEATHER_API_KEY") is not None,
)
```

> [!NOTE]
> `check_fn` errors are caught and returned as tool errors — don't rely on exceptions propagating out of it.

### `register_hook(event, callback)`

Subscribe `callback` to a lifecycle event. The callback receives one positional `ctx` (an event-specific context dataclass) and may return an action object to influence runtime flow. See [Hook events](#hook-events) for the full list.

```python
from flowly.agent.hooks import BlockAction

def register(ctx):
    def reject_root_writes(hook_ctx):
        if hook_ctx.tool_name == "write_file":
            path = hook_ctx.params.get("path", "")
            if path.startswith("/etc/"):
                return BlockAction("writes to /etc/ are not allowed")

    ctx.register_hook("pre_tool_call", reject_root_writes)
```

Every callback runs inside its own `try/except` — an exception in your hook is logged but **never** breaks the agent loop.

### `register_command(name, handler, *, description="", args_hint="")`

Register an in-session slash command, available across all channels (Telegram, Web, Desktop, iOS, …).

```python
def register(ctx):
    def ping(args: str) -> str:
        return "pong" + (f" {args}" if args else "")

    ctx.register_command("ping", handler=ping, description="Round-trip latency check")
```

The handler signature is `fn(raw_args: str) -> str | None`; `None` means fire-and-forget (no reply). The names `new`, `clear`, `compact`, and `help` are reserved and rejected. Names are lowercased and hyphenated automatically — `/Foo Bar` becomes `/foo-bar`.

### `register_skill(name, path, *, description="")`

Register a plugin-namespaced skill, loadable as `<plugin>:<name>` via `skill_view`.

```python
from pathlib import Path

def register(ctx):
    ctx.register_skill(
        name="onboard",
        path=Path(__file__).parent / "skills" / "onboard" / "SKILL.md",
        description="First-run user onboarding flow",
    )
```

> [!NOTE]
> Plugin skills do **not** appear in the system prompt's available-skills list — they are explicit-load only. This keeps the prompt-cache prefix stable across plugin sets. Don't pre-prefix the bare name with `<plugin>:`; the namespace is built for you, and names containing `:` are rejected.

## Hook events

Fourteen events are defined. The **Fires?** column reflects v1 wiring — events without a fire site won't trigger your callback yet, but you can register against them today and they'll start firing in a later version.

| Event | Context | Return for action | Fires? |
|---|---|---|---|
| `pre_tool_call` | `ToolHookContext` | `BlockAction` to abort | yes |
| `post_tool_call` | `ToolHookContext` | observation only | yes |
| `transform_tool_result` | `ToolHookContext` | `str` to replace result | yes |
| `transform_terminal_output` | `ToolHookContext` | `str` to replace output | no |
| `pre_llm_call` | `LLMHookContext` | `str` / `{"context": str}` to inject into the user message | no |
| `post_llm_call` | `LLMHookContext` | observation only | no |
| `pre_api_request` | `LLMHookContext` | observation only | no |
| `post_api_request` | `LLMHookContext` | observation only | no |
| `on_session_start` | `SessionHookContext` | observation only | yes |
| `on_session_end` | `SessionHookContext` | observation only | yes |
| `on_session_finalize` | `SessionHookContext` | observation only | no |
| `on_session_reset` | `SessionHookContext` | observation only | no |
| `subagent_stop` | `SubagentStopContext` | observation only | no |
| `pre_gateway_dispatch` | `GatewayDispatchContext` | `SkipAction` / `RewriteAction` | no |

### Action protocols

```python
from flowly.agent.hooks import BlockAction, RewriteAction, SkipAction

# pre_tool_call → abort dispatch with a message
return BlockAction(message="rate-limited")

# pre_gateway_dispatch → drop the message (no reply)
return SkipAction(reason="spam")

# pre_gateway_dispatch → replace the inbound text
return RewriteAction(text="redacted")
```

`pre_llm_call` callbacks return strings or `{"context": "..."}` dicts; returned values are appended to the **user message**, never the system prompt — this protects the prompt cache.

## Reference: the bundled `disk-cleanup` plugin

Exactly one plugin ships bundled: **`disk-cleanup`** (`kind: standalone`). It's the smallest practical example combining hooks and a slash command, with **zero tools** — proof that a plugin can deliver value without ever appearing in the LLM's tool list.

It auto-tracks ephemeral session files (test/temp outputs from `write_file` / `edit_file` / `exec` under `$FLOWLY_HOME` and `/tmp/flowly-*`) via a `post_tool_call` hook, runs a quick cleanup at `on_session_end`, and registers a `/disk-cleanup` slash command with subcommands `status | dry-run | quick | track | forget`. All file operations are best-effort and never break the agent loop.

```python
def register(ctx):
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and clean up ephemeral session files.",
    )
```

Read `flowly/plugins_bundled/disk-cleanup/` for a working template.

## Limitations (v1)

The following are intentionally absent in v1 and can be added later without breaking the v1 API:

- **`register_image_gen_provider` / `register_context_engine`** — no pluggable image-gen or context-engine abstractions yet.
- **`register_cli_command`** — terminal subcommand registration.
- **`inject_message` / `dispatch_tool`** — plugin-initiated message injection and tool dispatch.
- **Pip entry-point discovery** — git-installed and bundled only.
- **`kind: backend` and `kind: exclusive`** — parse but are skipped.

Hook events marked **Fires? no** exist in the registry but aren't wired into runtime call sites yet; registering against them today starts working when their fire sites land.

## Common gotchas

- **Enabled a plugin but nothing happened?** Discovery runs once at startup — restart `flowly` / the gateway.
- **Tool name collisions** — last writer wins in `ToolRegistry`. A plugin can override a built-in by registering the same name. Be deliberate.
- **Slash command names** are lowercased and hyphenated automatically.
- **Skill names with `:`** are rejected — the `<plugin>:<bare>` namespace is built for you.
- **`check_fn` errors** are caught and surfaced as tool errors, not exceptions.

## Related

- [Skills](skills.md) · [Skill bundles](skill-bundles.md) · [Feature overview](overview.md)
- [Slash commands reference](../reference/slash-commands.md) · [CLI commands reference](../reference/cli-commands.md)
