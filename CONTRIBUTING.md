# Contributing to Flowly

Thanks for helping improve Flowly! This guide covers the dev setup, where things
live, and how to get a change merged. It's short on purpose — when in doubt, read
the code or open a [discussion](https://github.com/Nocetic/flowly/issues).

> **Scope of this repo.** This is the open-source agent core: the `flowly` CLI,
> the gateway, providers, tools, skills, and channel adapters. The native
> Mac/iOS/Android apps and the hosted relay are separate, closed components — see
> [DESKTOP_VS_OSS.md](content/docs/using-flowly/desktop-vs-oss.md). PRs here target the CLI/gateway.

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

The install script (`install.sh`) does a lot of setup silently. Doing it by hand
is five commands, but two of them are ones nobody guesses. Read the whole
section once — the two traps in *Which `flowly` am I running?* cost more time
than everything else here.

### Prerequisites

[uv](https://docs.astral.sh/uv/) (it manages Python for you) and Git. Python
**3.11+** is required (`pyproject.toml`); 3.12 is what the installer and CI use.

`install.sh` installs uv for you — a source checkout doesn't, so install it
first. This is the same command the installer runs:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

It lands in `~/.local/bin`, which your current shell may not have on PATH yet —
`uv: command not found` right after installing means exactly that. Open a new
terminal, or for this one:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Two system tools the install script installs for you, and a source checkout does
not: **ffmpeg** (voice notes, the media library, `video_analyze`) and
**ripgrep** (the `rg` binary the agent's shell tooling reaches for). Flowly runs
without them — the features that need them just fail.

```bash
brew install ffmpeg ripgrep                  # macOS
sudo apt install -y ffmpeg ripgrep           # Debian / Ubuntu
winget install Gyan.FFmpeg BurntSushi.ripgrep.MSVC   # Windows
```

### Clone and install

```bash
git clone https://github.com/Nocetic/flowly.git
cd flowly

uv venv --python 3.12
uv pip install -e ".[dev]"

uv run flowly --version      # ✦ flowly v3.x.y
```

`[dev]` is pytest + ruff. Web search backends (ddgs, Exa, Firecrawl, Parallel)
are a separate extra — without it web search reports a missing dependency
instead of failing mysteriously:

```bash
uv pip install -e ".[dev,search]"
```

### Which `flowly` am I running?

**Trap 1 — the `flowly` on your PATH is not your checkout.** If you have ever
installed Flowly, that install owns the name: `install.sh` puts its own checkout
in `~/.local/share/flowly/repo` and symlinks its launcher onto your PATH; a
`uv tool install` lives under `~/.local/share/uv/tools/`. Editing your clone
changes nothing about what `flowly` runs, silently.

```bash
which flowly                 # the installed copy — not this repo
```

Inside the checkout, always run the CLI through uv, which uses the venv you just
built:

```bash
uv run flowly <command>      # anything: gateway, doctor, setup, agent…
```

(The first `uv run` re-syncs `.venv` from `uv.lock`; the editable install of
`flowly` plus pytest and ruff survive that. `source .venv/bin/activate` works
too, but then a second terminal that forgot to activate is back to trap 1.)

**Trap 2 — the terminal UI attaches to whatever gateway is already running.**
`flowly` doesn't necessarily start your code: it connects to whatever is
listening on the configured gateway port (18790 by default) and only starts a
gateway if nothing is there. With your installed Flowly running, you'd be
talking to the old build while your edits sit untouched. Pick one of the two
modes below so that can't happen.

### Two development modes

|  | **Mode A — core only** | **Mode B — with Desktop / iOS** |
|---|---|---|
| For | tools, providers, channels, memory, TUI, tests — most contributions | changes you need to see through Flowly Desktop or the iOS app |
| State | a throwaway `FLOWLY_HOME`, your real `~/.flowly` untouched | your **real** `~/.flowly` |
| Port | your own (e.g. 18999) | 18790 — you take it over from the installed gateway |
| Risk | none | dev code runs against your real memory, sessions, and channel tokens |

Start with Mode A. Desktop and iOS hardcode `~/.flowly` and connect to the
configured port, so Mode B is the only way to exercise them — there is no
isolation to be had there.

#### Mode A — isolated

```bash
export FLOWLY_HOME=~/flowly-dev-home     # every path Flowly touches moves here

uv run flowly bootstrap                  # seed workspace: SOUL/USER/AGENTS/MEMORY + personas
uv run flowly setup byok openrouter --key sk-or-...   # or: uv run flowly setup
```

`flowly setup byok` writes a complete `$FLOWLY_HOME/config.json` (keys are
**camelCase** on disk — `providers.openrouter.apiKey`). Don't hand-write that
file; let the CLI create it, then change what you need.

Two values need a manual edit, because `FLOWLY_HOME` does not cover them:

```jsonc
{
  "gateway": { "port": 18999 },                     // don't collide with, or attach to, your real gateway
  "agents": { "defaults": {
    "workspace": "/Users/you/flowly-dev-home/workspace"   // written as "~/.flowly/workspace" — literally
  }}
}
```

The workspace one matters: the gateway builds the agent from
`agents.defaults.workspace` as written, so left alone, an "isolated" dev agent
reads and writes the memory in your real workspace.

Then:

```bash
uv run flowly doctor         # config + runtime health
uv run flowly gateway        # foreground, your code, your port
uv run flowly                # terminal UI, in a second shell (same FLOWLY_HOME)

curl -s http://127.0.0.1:18999/health     # {"status": "ok", …}
```

Export `FLOWLY_HOME` in every shell you use for dev work — a shell without it
talks to your real install. `uv run flowly -p dev <command>` is the alternative:
it keeps state in `~/.flowly/profiles/dev/` instead.

#### Mode B — against Desktop / iOS

The rule: **stop the installed gateway, then serve 18790 from your checkout.**

```bash
git clone https://github.com/Nocetic/flowly.git && cd flowly
scripts/dev-gateway.sh
```

That is the whole setup for this mode: the script installs uv if it's missing,
`uv run` builds the virtualenv and installs Flowly on first use, and it uses
your real `~/.flowly` — the same config, keys, and memory your everyday agent
uses, so there is nothing to configure again. It does everything below, and
restores the service on Ctrl+C. (Tests and lint need one extra step:
`uv pip install -e ".[dev]"`.)

By hand:

```bash
flowly service stop                          # NOT `kill`: launchd (KeepAlive) and
                                             # systemd (Restart=always) respawn it
lsof -nP -iTCP:18790 -sTCP:LISTEN            # confirm nothing is listening

uv run flowly gateway                        # your code, real ~/.flowly, port 18790
# … work, with Desktop / the iOS app attached …

flowly service install --start               # hand the machine back when you're done
```

**Yes, Desktop just sees it.** There is nothing to register or pair: Desktop
polls `http://localhost:<gateway.port>/health` and accepts any response carrying
the gateway's identity marker, no matter how the process was started. A gateway
you launched by hand from a checkout answers that check like any other, and
Desktop attaches to it — it shows up as an *external* instance (Desktop knows
it doesn't own it, so it won't try to keep it alive or restart it). The iOS app
reaches the same gateway through the relay.

The one thing to avoid: while your foreground gateway is up, don't use Desktop's
own start/restart control. It runs `flowly service install --port … --start
--force`, which puts the installed build back on the port and takes yours down.

### Don't run `flowly update` in your checkout

It is the *user* update path, and it acts on whatever checkout it's running
from: fetch, autostash your uncommitted work, `git pull --ff-only`, reinstall
dependencies, restart the gateway. Use `git` directly on a dev clone.

---

## Tests and lint

```bash
pytest                  # full suite; live-LLM tests are skipped by default
pytest -m real_llm      # opt in to live-LLM tests (needs OPENROUTER_API_KEY, spends tokens)
ruff check flowly/      # lint
ruff check --fix flowly/
```

From a checkout, prefix with `uv run` (`uv run pytest`) unless you've activated
the venv. The suite is ~3,800 tests and finishes in about a minute; keep
`FLOWLY_HOME` pointed at your dev home while running it, so nothing can reach
into your real state.

CI runs `ruff check` and `pytest` on every PR (Ubuntu, Python 3.11 + 3.12). Run
both locally first. Tests use `pytest-asyncio` in `auto` mode — write
`async def test_...` directly, no decorator needed. Keep new tests hermetic (no
network, no real keys): use `monkeypatch` and `tmp_path`, and set `FLOWLY_HOME`
via `monkeypatch.setenv` in any test that touches user state.

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
[PLUGINS.md](content/docs/features/plugins.md).

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
