---
name: plugin-creator
description: Create a new Flowly plugin from a conversational description. Use when the user asks to "create a plugin", "build me a plugin", "add a tool", "react to events", "add a slash command", or describes functionality that would extend Flowly itself (not just a one-off task). Plugins live at ~/.flowly/plugins/ and persist across sessions, unlike inline scripts. If the user just wants something done once, do not use this skill — write a one-off script instead.
metadata: {"flowly":{"emoji":"🧩"}}
---

# Plugin Creator

Author a working Flowly plugin from a natural-language description. The output is a directory at `~/.flowly/plugins/<name>/` with a manifest and Python code. The user must restart the gateway after.

## When this triggers

YES — use this skill:
- "Create a plugin that searches my Notion"
- "I want a /pricing command that returns our prices"
- "Make a plugin that redacts phone numbers from incoming messages"
- "Block any tool call that uses sudo"
- "Notify Slack when the agent edits a file"

NO — do NOT use this skill:
- "Search my Notion for X" (one-off — just do it)
- "Write me a Python script that..." (script ≠ plugin)
- "How do plugins work?" (explain, don't generate)

If unsure, ask: "Do you want this as a permanent plugin, or just done once?"

## Architecture in 30 seconds

A plugin is a directory with two files:

```
~/.flowly/plugins/<name>/
├── plugin.yaml      # metadata + capability declaration
└── __init__.py      # register(ctx) function
```

`register(ctx)` is called once at gateway startup. It uses four primitives:

| Primitive | When |
|---|---|
| `ctx.register_tool(...)` | Agent calls it during conversation (e.g. `notion_search`) |
| `ctx.register_hook(event, callback)` | React to lifecycle events (tool call, message inbound, session end) |
| `ctx.register_command(name, handler)` | User-typed shortcut (e.g. `/pricing`) |
| `ctx.register_skill(name, path)` | Markdown skill the agent can load on demand |

A single plugin can use any combination of these.

## DO NOT use these tools for plugin creation

These look related but are wrong:

- `skill_manage(action="create", ...)` — that creates a SKILL at `~/.flowly/skills/`, not a plugin. Skills are markdown-only context files; plugins are Python directories with `register()`. Different system entirely.
- `skill_view(...)` — only used to load THIS skill once for guidance; never used as part of writing the plugin.

Plugin creation = `write_file` + edit `~/.flowly/config.json` + `exec` for syntax check. Nothing else.

## Workflow

### Step 1 — Discover the requirement

Ask the user (in their language, conversationally — don't dump a form):

1. **Goal** — what should the plugin do, in one sentence?
2. **Trigger** — when does it run? (agent decides → tool, user types `/x` → command, automatic on event → hook, on-demand instructions → skill)
3. **Name** — short slug, lowercase, hyphens. Examples: `notion-search`, `pii-redactor`. Must match `^[a-z0-9][a-z0-9_-]{0,63}$`.
4. **External access** — does it call an API or shell command? If yes, what auth (API key env var, OAuth, none)?
5. **Inputs/outputs** — for tools and commands, what arguments come in, what string goes out?

Don't move on until you have all five. If something is ambiguous, propose a default and confirm.

### Step 2 — Pick the primitive

Match the goal to the right primitive (multiple is fine):

- **Tool** — agent picks when to call it. Use for: lookups, computations, integrations the agent decides about.
- **Slash command** — user picks. Use for: shortcuts, status checks, manual triggers.
- **Hook** — neither agent nor user picks; it fires automatically. See "Hook events" below.
- **Skill** — markdown content the agent loads explicitly. Use for: large reference docs, multi-step procedures.

### Step 3 — Validate the name

Before writing anything, check:

```bash
ls ~/.flowly/plugins/<name> 2>/dev/null
```

If it exists, ask the user: overwrite, rename, or abort.

For slash commands, reject these reserved names:
- `new`, `clear`, `compact`, `help`

### Step 4 — Generate files

Create the directory and write both files. Templates are below — pick the one matching the primitive(s), substitute placeholders, write to disk.

### Step 5 — Enable in config.json (CRITICAL — DO NOT SKIP)

User plugins are opt-in. **Without this step the plugin will NOT load** and the user will see nothing change. This is the single most common failure mode — be explicit, be deterministic.

`~/.flowly/config.json` is OUTSIDE the workspace, so `read_file` will be denied with "Access denied — path outside workspace". Don't waste tokens trying — use `exec` with a single inline Python invocation.

**USE THIS EXACT SCRIPT.** Do not improvise with `jq`, `sed`, `awk`, or shell echo — those will corrupt the JSON. The script below is atomic, idempotent, and creates a backup before touching the file.

```bash
python3 - <<'PY'
import json, os, shutil, tempfile

p = os.path.expanduser("~/.flowly/config.json")
backup = p + ".bak"

# 1. Read existing config (or start fresh if missing)
if os.path.exists(p):
    with open(p) as f:
        original_text = f.read()
    cfg = json.loads(original_text)
    # 2. Backup the original so user can recover if anything goes wrong
    shutil.copy(p, backup)
else:
    cfg = {}

# 3. Mutate in memory
plugins = cfg.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
disabled = plugins.setdefault("disabled", [])
if "<plugin-name>" not in enabled:
    enabled.append("<plugin-name>")
if "<plugin-name>" in disabled:
    disabled.remove("<plugin-name>")

# 4. Atomic write: write to temp file in same dir, then rename.
#    os.replace is atomic on POSIX — either old file or new file
#    exists at all times, never a half-written one.
new_text = json.dumps(cfg, indent=2)
dir_, base = os.path.split(p)
fd, tmp = tempfile.mkstemp(dir=dir_, prefix="." + base + ".", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
except Exception:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise

# 5. Verify by reading back
with open(p) as f:
    verify = json.loads(f.read())
assert "<plugin-name>" in verify.get("plugins", {}).get("enabled", []), "verification failed"
print("OK — enabled:", verify["plugins"]["enabled"])
PY
```

Substitute `<plugin-name>` with the actual name (in BOTH places). The script:
- Reads + parses the existing JSON (crashes safely if the file is malformed — original untouched)
- Saves a `.bak` copy before any mutation, so the user can recover with `cp ~/.flowly/config.json.bak ~/.flowly/config.json`
- Mutates in memory
- Writes to a temp file in the same directory, fsyncs, then atomically renames over the original. POSIX guarantees the rename is all-or-nothing — at no point can the file be half-written or zero-byte
- Reads back and verifies the plugin appears in `enabled` before printing OK

If anything fails, the original config.json is untouched and the `.bak` exists as a second safety net.

**Bundled plugins are enabled by default** — they don't need this step. **User plugins always do.** Anything you create with this skill is a user plugin.

### Step 6 — Syntax check

```bash
python3 -m py_compile ~/.flowly/plugins/<name>/__init__.py
```

If this fails, fix the code and retry. Do not leave broken Python on disk. Note: `python3 -c "import ast; ast.parse(...)"` requires importing `ast` first; `py_compile` is simpler.

### Step 7 — Final response to the user

Tell the user **exactly**:

> ✓ Plugin **<name>** ready at `~/.flowly/plugins/<name>/`, enabled in config.
>
> Restart the gateway: `flowly service restart` (or kill + relaunch your dev gateway).
>
> Then try: `<concrete first action>`.

The "concrete first action" is critical — give them the exact slash command, tool prompt, or test invocation.

## Pre-flight checklist (do this before declaring done)

Before responding to the user, mentally run through:

- [ ] `~/.flowly/plugins/<name>/__init__.py` exists and `py_compile` passed
- [ ] `~/.flowly/plugins/<name>/plugin.yaml` exists with required fields (name, version, manifest_version, description, kind)
- [ ] `name:` in manifest equals the directory name
- [ ] `<name>` is now in `plugins.enabled` of `~/.flowly/config.json` and NOT in `disabled`
- [ ] If env vars needed, told user where to put them
- [ ] Final response includes restart instruction AND a concrete test invocation

If any box is unchecked, finish that step before responding. Skipping any of these = plugin won't work for the user.

## Hook events

Live (fires today):
- `pre_tool_call(ToolHookContext)` — before any tool dispatch. Return `BlockAction(message=...)` to abort.
- `post_tool_call(ToolHookContext)` — after tool dispatch, with `result`, `duration_ms`, `success`. Observation only.
- `transform_tool_result(ToolHookContext)` — return a `str` to replace the result the agent sees.
- `pre_llm_call(LLMHookContext)` — before each LLM call. Return a `str` to inject into the user message (wrapped in `<plugin_context>`).
- `on_session_start(SessionHookContext)` — first message of a session.
- `on_session_end(SessionHookContext)` — after every turn.
- `pre_gateway_dispatch(GatewayDispatchContext)` — for every inbound message before processing. Return `SkipAction(reason=...)` to drop or `RewriteAction(text=...)` to replace content.

Planned (defined but not yet fired by runtime):
- `transform_terminal_output`, `post_llm_call`, `pre_api_request`, `post_api_request`, `on_session_finalize`, `on_session_reset`, `subagent_stop`

If the user's idea needs a planned event, say so — don't write code that won't fire.

## Manifest spec (`plugin.yaml`)

Required:
```yaml
name: <slug>                       # must match the directory name
version: 0.1.0                     # semver
manifest_version: 1
description: "One-line summary"
kind: standalone                   # ALWAYS this exact value in v1
```

**`kind` MUST be `standalone`.** The only other accepted values (`backend`, `exclusive`) are reserved for future use and will not load. Do NOT invent values like `command`, `tool`, `hook`, `service` — those will silently fall back to `standalone` with a warning, but the manifest is wrong and the warning pollutes logs.

Optional, for UI accuracy and tooling — declare what `register()` actually does:
```yaml
provides_tools:
  - <tool_name>
provides_hooks:
  - <event_name>
provides_commands:
  - <command_name>
requires_env:
  - name: <ENV_VAR>
    description: "Why this is needed"
```

The `provides_*` lists are HINTS read by the desktop UI. Keep them honest — if `register()` adds a tool, list it here.

## Code templates

Each template is a complete `__init__.py`. Substitute `<placeholders>` in angle brackets.

### Template A — Static text command

User types `/<command>` and gets a fixed string back. Zero deps, zero auth.

```python
"""<one-line description>"""


def register(ctx):
    def handler(args: str) -> str:
        return """<your text here, can be multiline markdown>"""

    ctx.register_command(
        "<command-name>",
        handler=handler,
        description="<what it does>",
    )
```

Manifest:
```yaml
name: <slug>
version: 0.1.0
manifest_version: 1
description: "<description>"
kind: standalone
provides_commands:
  - <command-name>
```

### Template B — HTTP tool with API key

Agent calls a function that hits an external API. API key from env.

```python
"""<one-line description>"""

import os
import httpx


def register(ctx):
    async def search(query: str, limit: int = 10) -> str:
        token = os.getenv("<ENV_VAR>")
        if not token:
            return "Plugin not configured: <ENV_VAR> env var is missing."
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "<base_url>/search",
                params={"q": query, "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return f"API returned {r.status_code}: {r.text[:200]}"
            data = r.json()
        if not data.get("results"):
            return f"No results for {query!r}."
        return "\n".join(f"- {item['title']}: {item['url']}" for item in data["results"])

    ctx.register_tool(
        name="<tool_name>",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "<what to search>"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
        handler=search,
        check_fn=lambda: bool(os.getenv("<ENV_VAR>")),
        description="<one-line description for the agent>",
    )
```

Manifest:
```yaml
name: <slug>
version: 0.1.0
manifest_version: 1
description: "<description>"
kind: standalone
provides_tools:
  - <tool_name>
requires_env:
  - name: <ENV_VAR>
    description: "<auth source>"
```

After writing, ALSO append the env var to `~/.flowly/.env` (creating the file with mode 0600 if it doesn't exist). Tell the user where it landed.

### Template C — Webhook slash command

User types `/<command>` to fire a webhook (Zapier, n8n, internal automation).

```python
"""<one-line description>"""

import os
import json
import httpx


def register(ctx):
    async def handler(args: str) -> str:
        url = os.getenv("<ENV_VAR>") or "<hardcoded_url_or_placeholder>"
        payload = {"text": args, "source": "flowly"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 300:
            return f"Webhook failed: {r.status_code}"
        return f"Triggered. Args: {args!r}"

    ctx.register_command(
        "<command-name>",
        handler=handler,
        description="<what it does>",
        args_hint="[<input_hint>]",
    )
```

### Template D — Pre-tool-call guard (hook)

Block tool calls matching a pattern before they run.

```python
"""<one-line description>"""

import re
from flowly.agent.hooks import BlockAction


_FORBIDDEN = [
    # add compiled patterns here, e.g.:
    # re.compile(r"\brm\s+-rf\s+/"),
]


def register(ctx):
    def guard(hook_ctx):
        if hook_ctx.tool_name != "<tool_to_guard>":
            return None
        target = str(hook_ctx.params.get("<param_to_inspect>", ""))
        for pattern in _FORBIDDEN:
            if pattern.search(target):
                return BlockAction(
                    message=f"<plugin> blocked: pattern {pattern.pattern}"
                )
        return None

    ctx.register_hook("pre_tool_call", guard)
```

### Template E — Pre-gateway-dispatch rewrite (PII / spam)

Modify or drop inbound messages before the agent sees them.

```python
"""<one-line description>"""

import re
from flowly.agent.hooks import RewriteAction, SkipAction


_REPLACEMENTS = [
    # (compiled_regex, replacement_string), e.g.:
    # (re.compile(r"\b\d{11}\b"), "[REDACTED_TC]"),
]


def register(ctx):
    def on_inbound(hook_ctx):
        msg = hook_ctx.event
        text = msg.content if msg else ""
        if not isinstance(text, str) or not text:
            return None
        new_text = text
        for rx, repl in _REPLACEMENTS:
            new_text = rx.sub(repl, new_text)
        if new_text == text:
            return None
        return RewriteAction(text=new_text)

    ctx.register_hook("pre_gateway_dispatch", on_inbound)
```

### Template F — Domain context injection (pre_llm_call)

Inject a fixed orientation paragraph into every user message so the agent picks the right tools.

```python
"""<one-line description>"""


def register(ctx):
    def inject(hook_ctx) -> str | None:
        return (
            "<one paragraph orienting the agent: who the user is, what "
            "tools are relevant, when to prefer them over generic tools>"
        )

    ctx.register_hook("pre_llm_call", inject)
```

Use sparingly — every turn pays the token cost. Skip injection when the relevant data is empty (return `None`).

### Template G — Post-tool-call observer (audit / notify)

React to every tool call without changing behaviour.

```python
"""<one-line description>"""

import json
import time
from pathlib import Path


_LOG = Path.home() / ".flowly" / "<plugin>" / "log.jsonl"


def _emit(record: dict) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **record}) + "\n")
    except Exception:
        pass


def register(ctx):
    def observe(hook_ctx):
        _emit({
            "session": hook_ctx.session_id,
            "tool": hook_ctx.tool_name,
            "duration_ms": hook_ctx.duration_ms,
            "success": hook_ctx.success,
        })

    ctx.register_hook("post_tool_call", observe)
```

### Template H — Plugin-namespaced skill

Bundle a markdown skill the agent loads on demand.

```python
"""<one-line description>"""

from pathlib import Path


def register(ctx):
    ctx.register_skill(
        name="<skill-name>",
        path=Path(__file__).parent / "skills" / "<skill-name>" / "SKILL.md",
        description="<when to load this>",
    )
```

Plus create `<plugin_dir>/skills/<skill-name>/SKILL.md` with valid frontmatter (`name`, `description`).

The agent loads it via `skill_view("<plugin_name>:<skill-name>")`.

## Combining primitives

Templates are starting points. A plugin can use several. Example: a Notion plugin might combine Template B (`notion_search` tool), Template F (context injection telling the agent when to use it), and Template H (a skill explaining the user's Notion workspace structure).

In that case `register()` calls all four primitives in one function. The structure stays:

```python
def register(ctx):
    # ... tool ...
    ctx.register_tool(...)
    # ... hook ...
    ctx.register_hook("pre_llm_call", _inject)
    # ... skill ...
    ctx.register_skill(...)
```

And the manifest declares all of them under `provides_tools` / `provides_hooks` / `provides_commands`.

## Pitfalls — read before writing

0. **You MUST add the plugin name to `plugins.enabled` in `~/.flowly/config.json`.** This is the #1 cause of "I created a plugin but it doesn't work" — the files exist but the gateway never loads them because user plugins are opt-in. If you forget this, the user types their slash command and gets nothing. See Step 5.

1. **Don't put secrets in `__init__.py`.** API keys, tokens, passwords go in `~/.flowly/.env` as env vars, read via `os.getenv(...)`. The manifest `requires_env` documents them. If the env var is missing at dispatch time, the tool returns a polite "not configured" string — never crashes.

2. **Async vs sync handlers.** Tools and slash command handlers can be either. Hooks can be either. The runtime detects awaitables. Use `async def` when calling `httpx`, `asyncio`, or any I/O library. Use plain `def` for CPU work and simple string ops.

3. **`check_fn` runs at dispatch time, not at registration.** Use it to gate tools that need OAuth/env vars. When `check_fn()` returns `False`, the tool returns "unavailable" without invoking the handler.

4. **Hook callbacks must be defensive.** A failing hook is logged but does not break the agent loop. Still — wrap risky work in try/except and degrade gracefully (return `None`).

5. **Reserved slash commands** (`/new`, `/clear`, `/compact`, `/help`) are silently rejected. Pick a different name.

6. **Plugin name must match directory name.** If the directory is `~/.flowly/plugins/notion-search/`, the manifest's `name:` field must be `notion-search`.

7. **Restart is required.** Plugins are discovered at gateway startup. Writing files is not enough — the gateway must restart to load them.

8. **Test what you wrote.** Before reporting completion, run the syntax check (`python3 -c "import ast; ast.parse(...)"`). If it fails, fix and retest.

## Final response template

After everything is written and validated, respond to the user with this shape (adapt to language):

> ✓ Plugin **<name>** created at `~/.flowly/plugins/<name>/`
>
> What it does: <one line>
>
> **Next:**
> 1. <if env var needed> Add `<ENV_VAR>=<value>` to `~/.flowly/.env`
> 2. Restart the gateway: `flowly service restart`
> 3. Try it: `<concrete test invocation>`

Keep it tight. The user just answered five questions; they don't need a recap.
