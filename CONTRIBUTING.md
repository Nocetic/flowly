# Contributing to Flowly

Thanks for helping improve Flowly! This guide covers the dev setup, where things
live, and how to get a change merged. It's short on purpose — when in doubt, read
the code or open a [discussion](https://github.com/Nocetic/flowly/issues).

> **Scope of this repo.** This is the open-source agent core: the `flowly` CLI,
> the gateway, providers, tools, skills, and channel adapters. The native
> Mac/iOS/Android apps and the hosted relay are separate, closed components — see
> [DESKTOP_VS_OSS.md](DESKTOP_VS_OSS.md). PRs here target the CLI/gateway.

---

## What to contribute

In rough priority order:

1. **Bug fixes** — crashes, wrong behavior, data loss. Always welcome.
2. **Cross-platform fixes** — macOS, Linux, and Windows should all work.
3. **Security hardening** — shell injection, path traversal, prompt injection,
   credential leakage. See [SECURITY.md](SECURITY.md).
4. **Skills** — broadly useful procedures (see *Skill, tool, or plugin?* below).
5. **Providers & channels** — new LLM adapters or messaging integrations.
6. **Docs** — fixes, clarifications, examples.

Most new capabilities should be a **skill** or a **plugin**, not a core tool.

---

## Development setup

**Prerequisites:** [uv](https://docs.astral.sh/uv/) (it manages Python for you)
and Git. Python **3.11+** is required (`pyproject.toml`); 3.12 is the default the
installer uses.

```bash
git clone https://github.com/Nocetic/flowly.git
cd flowly

uv venv --python 3.12
source .venv/bin/activate         # Windows: .venv\Scripts\activate

uv pip install -e ".[dev]"

# Point Flowly at an LLM provider (writes ~/.flowly/config.json)
flowly setup byok openrouter --key sk-or-...
# …or run the full wizard:
flowly setup

# Sanity check
flowly doctor
flowly                            # opens the terminal UI
```

Config lives at `~/.flowly/config.json` (keys are **camelCase**). To keep dev
state isolated from your real install, set `FLOWLY_HOME=/tmp/flowly-dev` (or use
`-p <profile>`) before running any command.

---

## Tests and lint

```bash
pytest                  # full suite; live-LLM tests are skipped by default
pytest -m real_llm      # opt in to live-LLM tests (needs OPENROUTER_API_KEY, spends tokens)
ruff check flowly/      # lint
ruff check --fix flowly/
```

CI runs `ruff check` and `pytest` on every PR. Run both locally first. Tests use
`pytest-asyncio` in `auto` mode — write `async def test_...` directly, no
decorator needed. Keep new tests hermetic (no network, no real keys): use
`monkeypatch` and `tmp_path`.

---

## Project layout

```
flowly/
├── cli/            # `flowly` command groups (setup, service, gateway, channels, …)
├── agent/          # agent loop, tool dispatch, subagents
│   └── tools/      # built-in tools (base.py = Tool ABC, registry.py = dispatch)
├── gateway/        # local WS daemon (127.0.0.1:18790), channel routing
├── providers/      # BYOK LLM adapters (anthropic, openai, openrouter, xai, …)
├── channels/       # Telegram, Discord, Slack, WhatsApp, iMessage, email, …
├── skills/         # bundled skills (one dir per skill, each with SKILL.md)
├── plugins/        # plugin runtime; plugins_bundled/ = ship-with plugins
├── memory/         # governed long-term memory + knowledge graph
├── sandbox/        # sandbox-exec (macOS) / bwrap (Linux) re-exec wrapper
├── config/         # config.json schema + loader (camelCase → snake_case)
├── session/  board/  cron/  mcp/  voice/  multiagent/  …
tests/              # 100+ test modules
```

User state lives under `~/.flowly/` (`config.json`, `workspace/`, `plugins/`,
`skills/`, memory store, session db).

---

## Skill, tool, or plugin?

| You want to… | Build a… |
|---|---|
| Ship instructions the agent loads on demand (a procedure, a CLI workflow) | **Skill** |
| Run code automatically before/after tool calls, LLM calls, or sessions | **Plugin hook** |
| Add a `/command` users can trigger from any channel | **Plugin command** |
| Add a capability the agent invokes that needs precise, every-time logic | **Tool** (core or plugin) |

Prefer skills and plugins — they don't touch core code and are easy to review.

### Adding a skill

Create `flowly/skills/<slug>/SKILL.md` with YAML frontmatter:

```yaml
---
name: my-skill
description: One line, ends with a period.
version: 1.0.0
license: Apache-2.0
platforms: [macos, linux, windows]   # omit to load everywhere
metadata: {"flowly": {"emoji": "🛠", "tags": ["Category"], "category": "dev"}}
---

# My Skill

Brief intro: what it does and what it doesn't.

## When to Use
## Procedure
## Pitfalls
## Verification
```

Put helper scripts in `scripts/` and longer docs in `references/` inside the skill
dir. Keep `description` short and concrete — no marketing words.

### Adding a plugin

Plugins live in `~/.flowly/plugins/<name>/` (user) or `flowly/plugins_bundled/`
(shipped). A plugin is a `plugin.yaml` manifest + an `__init__.py` exposing
`register(ctx)`. The full API (tools, hooks, commands, skills) is in
[PLUGINS.md](PLUGINS.md).

### Adding a core tool

Rarely needed. Subclass `Tool` (`flowly/agent/tools/base.py`) — implement `name`,
`description`, `parameters` (JSON Schema), and async `execute()` — then register
it with `tool_registry.register(...)` (`flowly/agent/tools/registry.py`). Read an
existing tool such as `flowly/agent/tools/message.py` as a template, and gate it
into the right toolset so the agent actually sees it.

---

## Security

Flowly has shell and filesystem access, so security review matters. When touching
exec, file paths, or credentials:

- Never log API keys, tokens, or passwords.
- Quote/escape any user input that reaches a shell; resolve symlinks before
  path-based access checks.
- Don't weaken the sandbox or the exec approval flow without saying so explicitly.

Flag any security-relevant change in your PR description. To report a
vulnerability privately, see [SECURITY.md](SECURITY.md) — don't open a public
issue for it.

---

## Pull requests

**Branches:** `fix/…`, `feat/…`, `docs/…`, `test/…`, `refactor/…`, `chore/…`.

**Commits:** [Conventional Commits](https://www.conventionalcommits.org/) —
`type(scope): description`. Common scopes: `cli`, `gateway`, `agent`, `tools`,
`skills`, `channels`, `providers`, `memory`, `sandbox`, `config`, `security`.

```
fix(gateway): don't drop the channel on a malformed ws frame
feat(providers): add a local vLLM adapter
docs(readme): clarify BYOK cascade order
```

**Before you open a PR:**

1. `ruff check flowly/` and `pytest` pass.
2. One logical change per PR — don't mix a fix, a refactor, and a feature.
3. The description says **what** changed, **why**, and **how to test** it.
4. If you touched exec, paths, or process management, note the platforms you
   tested on.

---

## Reporting issues

Use [GitHub Issues](https://github.com/Nocetic/flowly/issues). Include your OS,
Python version, `flowly` version, the full traceback, and steps to reproduce.
Search existing issues first.

---

## License

By contributing, you agree your contributions are licensed under the
[Apache 2.0 License](LICENSE).
