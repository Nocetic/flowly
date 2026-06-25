# Flowly Plugin System

Plugins extend Flowly with custom **tools**, **lifecycle hooks**, **slash
commands**, and **skills** without modifying core code. A plugin is a
small directory containing two files: a manifest and an entry point.

This document is the authoring reference. For the design rationale see
the conversation history that produced this system; for the runtime
internals read `flowly/plugins/`.

---

## When to write a plugin

| Goal | Use |
|---|---|
| Give the agent a new capability it can decide to invoke | **Tool** (`register_tool`) |
| Run something automatically before/after every tool call, LLM call, or session | **Hook** (`register_hook`) |
| Let the user trigger something with `/foo` from any channel | **Slash command** (`register_command`) |
| Ship instructions the agent loads on demand | **Skill** (`register_skill`) |

A single plugin may register any combination of the four.

---

## Plugin layout

```
my-plugin/
├── plugin.yaml          # manifest (required)
└── __init__.py          # exposes register(ctx) (required)
```

That's it. Add more `.py` files as needed; import them from `__init__.py`
the usual way.

### `plugin.yaml`

```yaml
name: my-plugin            # required — unique key in plugins.enabled
version: 1.0.0
manifest_version: 1        # bump when the schema changes; current = 1
description: "What it does, in one line."
author: "you@example.com"
kind: standalone           # v1: only standalone is supported
provides_tools:            # optional — for `flowly plugins list` only
  - my_tool
provides_hooks:
  - post_tool_call
```

`plugin.json` is also accepted if you prefer JSON; the loader probes
`plugin.yaml`, `plugin.yml`, then `plugin.json` in that order.

### `__init__.py`

```python
def register(ctx):
    """Plugin entry point. Called once at agent startup."""
    ctx.register_tool(...)
    ctx.register_hook(...)
    ctx.register_command(...)
    ctx.register_skill(...)
```

`ctx` is a `flowly.plugins.PluginContext`. See the API below.

---

## Discovery & enablement

Three sources, scanned in order, last writer wins on key collision:

1. **Bundled** — `flowly/plugins_bundled/<name>/`. Ship-with-the-package
   plugins. **Default-on**: load unless listed in `plugins.disabled`.
2. **User** — `$FLOWLY_HOME/plugins/<name>/` (per-profile). **Opt-in**:
   only loads when the name appears in `plugins.enabled`.
3. **Project** — `./.flowly/plugins/<name>/`. Opt-in via env var
   `FLOWLY_ENABLE_PROJECT_PLUGINS=1`. Treated like user plugins for
   enable/disable.

`config.json` snippet:

```json
{
  "plugins": {
    "enabled": ["my-plugin", "another"],
    "disabled": []
  }
}
```

The `disabled` list overrides everything, including bundled plugins.

CLI:

```sh
flowly plugins list                          # what's discovered + status
flowly plugins install <git-url|owner/repo>  # clone into ~/.flowly/plugins/
flowly plugins install /path/to/local-plugin # copy from disk
flowly plugins enable my-plugin              # add to plugins.enabled
flowly plugins disable my-plugin             # add to plugins.disabled
flowly plugins remove my-plugin              # uninstall (delete dir)
```

Restart `flowly` after enable/disable for changes to take effect.

---

## PluginContext API

### `register_tool(name, schema, handler, *, check_fn=None, description="")`

Register a function-based tool. `handler` is sync or async. `schema` is
either a JSON Schema dict for the parameters or the OpenAI function
schema with `parameters` inside.

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

If `check_fn` is provided, it runs at **dispatch time** (not register
time). Returning `False` short-circuits with a clear error — useful when
the tool needs an OAuth token or env var that may not exist yet:

```python
ctx.register_tool(
    ...,
    check_fn=lambda: os.environ.get("WEATHER_API_KEY") is not None,
)
```

### `register_hook(event, callback)`

Subscribe `callback` to a lifecycle event. The callback receives one
positional `ctx` (an event-specific context dataclass) and may return
an action object to influence runtime flow.

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

Full event list and semantics are in the **Hook events** section below.

### `register_command(name, handler, *, description="", args_hint="")`

Register an in-session slash command. Available across all channels
(Telegram, Web, Desktop, iOS, …).

```python
def register(ctx):
    def ping(args: str) -> str:
        return "pong" + (f" {args}" if args else "")

    ctx.register_command(
        "ping",
        handler=ping,
        description="Round-trip latency check",
    )
```

Reserved names (`new`, `clear`, `compact`, `help`) are rejected with a
warning. The handler signature is `fn(raw_args: str) -> str | None`;
`None` means fire-and-forget (no reply sent).

### `register_skill(name, path, *, description="")`

Register a plugin-namespaced skill. The skill becomes loadable as
`<plugin>:<name>` via `skill_view`. **Plugin skills do not appear in the
system prompt's available-skills list** — they are explicit-load only.
This keeps the prompt-cache prefix stable across plugin sets.

```python
from pathlib import Path

def register(ctx):
    ctx.register_skill(
        name="onboard",
        path=Path(__file__).parent / "skills" / "onboard" / "SKILL.md",
        description="First-run user onboarding flow",
    )
```

---

## Hook events

Fourteen events are defined. The "fires?" column reflects v1 wiring;
events without a fire site won't trigger your callback yet, but you can
register against them today and they'll start firing in later versions.

| Event | Context | Return for action | Fires? |
|---|---|---|---|
| `pre_tool_call` | `ToolHookContext` | `BlockAction` to abort | yes |
| `post_tool_call` | `ToolHookContext` | observation only | yes |
| `transform_tool_result` | `ToolHookContext` | `str` to replace result | yes |
| `transform_terminal_output` | `ToolHookContext` | `str` to replace output | no |
| `pre_llm_call` | `LLMHookContext` | `str` / `{"context": str}` to inject into user message | no |
| `post_llm_call` | `LLMHookContext` | observation only | no |
| `pre_api_request` | `LLMHookContext` | observation only | no |
| `post_api_request` | `LLMHookContext` | observation only | no |
| `on_session_start` | `SessionHookContext` | observation only | yes |
| `on_session_end` | `SessionHookContext` | observation only | yes |
| `on_session_finalize` | `SessionHookContext` | observation only | no |
| `on_session_reset` | `SessionHookContext` | observation only | no |
| `subagent_stop` | `SubagentStopContext` | observation only | no |
| `pre_gateway_dispatch` | `GatewayDispatchContext` | `SkipAction` / `RewriteAction` | no |

All callbacks run inside their own `try/except`; an exception in your
hook is logged but never breaks the agent loop.

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

`pre_llm_call` callbacks return strings or `{"context": "..."}` dicts;
returned values are appended to the **user message** (never the system
prompt — protects the prompt cache).

---

## Reference: bundled `disk-cleanup`

The smallest practical example combining hooks and a slash command lives
at `flowly/plugins_bundled/disk-cleanup/`. It registers two hooks
(`post_tool_call`, `on_session_end`) and one slash command
(`/disk-cleanup`), with zero tools — illustrating that plugins can
deliver value without ever appearing in the LLM's tool list.

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

Read its source for a working template.

---

## Limitations (v1)

The following items are intentionally absent in v1:

- **`register_image_gen_provider` / `register_context_engine`** — Flowly
  has no pluggable image-gen or context-engine abstractions yet.
- **`register_cli_command`** — terminal subcommand registration; can be
  added later without breaking the v1 API.
- **`inject_message` / `dispatch_tool`** — plugin-initiated message
  injection and tool dispatch; deferred.
- **Pip entry-point discovery** — git-installed and bundled only.
- **`kind: backend` and `kind: exclusive` manifests** — parse without
  error but the manager skips loading them.

The hook events listed above with "fires? no" exist in the registry but
aren't wired into runtime call sites. Registering against them today
will start working when their fire sites are added in later versions.

---

## Common gotchas

- **First-time `flowly plugins enable my-plugin`?** You also need to
  restart the gateway / agent. Plugin discovery runs once at startup.
- **Tool name collisions** — last writer wins in `ToolRegistry`. A
  plugin can override a built-in by registering with the same name. Be
  deliberate.
- **Slash command names** — automatically lowercased and hyphenated;
  `/Foo Bar` becomes `/foo-bar`.
- **Skill names with `:`** are rejected — the `<plugin>:<bare>`
  namespace is built automatically; don't pre-prefix the bare name.
- **`check_fn` errors** are caught and returned as tool errors. Don't
  rely on exceptions propagating from `check_fn`.
