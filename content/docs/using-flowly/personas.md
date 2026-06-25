---
title: Personas
eyebrow: Using Flowly
description: A persona is a switchable system-prompt override that changes who the agent is for the session. A non-default persona becomes the agent's primary identity, overriding Flowly's default identity. SOUL.md is always loaded.
---

## Built-in personas

Flowly ships with eight built-in personas:

| Persona | |
|---|---|
| `default` | The baseline; no persona layer added |
| `jarvis` | |
| `friday` | |
| `pirate` | |
| `samurai` | |
| `casual` | |
| `professor` | |
| `butler` | |

Their files live in your workspace at `<workspace>/personas/<name>.md`. You can edit these or add your own.

## Managing personas

```bash
flowly persona list          # list available personas
flowly persona set <name>    # make a persona the default
flowly persona show <name>   # print a persona's contents
```

`flowly persona set <name>` writes `agents.defaults.persona` in `config.json` and saves. If the gateway is running, it auto-restarts to apply the change.

The config default is `agents.defaults.persona = "default"`. See [Configuration](./configuration.md).

## Switching at runtime

Inside the TUI, switch persona with a slash command:

```
/persona
```

The `/assistants` slash command is also available for assistant selection. See [Terminal UI](./tui.md).

You can also override the persona when launching the gateway:

```bash
flowly gateway --persona jarvis
```

The `--persona` flag overrides the config default for that gateway run.

## Relation to SOUL.md

Bootstrap files (including `SOUL.md`) are always loaded into the system prompt. When the active persona is anything other than `default`, the matching `<workspace>/personas/<name>.md` is injected under a **"CRITICAL PERSONA OVERRIDE"** heading that asserts the persona as the agent's **primary identity** — the model is told it is *not* Flowly and should adopt the persona instead. So a non-default persona **overrides** Flowly's default identity for the session rather than mildly layering on top of it. The memory and operational rules from `SOUL.md` are still loaded and still apply.

> [!NOTE]
> Persona and bootstrap files are scanned for prompt-injection before being applied; if a file trips the scanner, a blocked placeholder is injected instead of its content, so a poisoned persona file can't hijack the agent.

## Related

- [Terminal UI](./tui.md)
- [Configuration](./configuration.md)
- [Sessions](./sessions.md)
- [Running as a service](./service.md)
- [Providers and models](./providers-and-models.md)
- [CLI commands](../reference/cli-commands.md)
