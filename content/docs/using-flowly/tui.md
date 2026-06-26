---
title: Terminal UI
eyebrow: Using Flowly
description: Bare `flowly` opens the terminal UI — a full-screen chat with your agent, with slash commands, live model/provider switching, session history, inline panels for activity and approvals, image/video attachments, and a local shell escape. No browser, no account required.
---

Running `flowly` with no arguments opens the **terminal UI (TUI)** — a full-screen chat client that talks to the local gateway. It's the default way to use Flowly on your own machine: everything the agent can do from a messaging channel, you can do here, plus a few terminal-only conveniences (a `$EDITOR` draft, a local shell escape, live panels).

```bash
flowly            # open the terminal UI
```

If the gateway isn't running yet, the TUI starts one for you and connects to it. To talk to a gateway running elsewhere (a VPS, a service), the TUI connects over the same WebSocket protocol every client uses — see [Service](./service.md) and [Sessions](./sessions.md).

> [!TIP]
> Just want a one-shot answer without entering the UI? Use `flowly agent -m "…"` instead — see [CLI commands](../reference/cli-commands.md).

## The layout

- **Composer** (bottom) — where you type. `Enter` sends; `Shift+Enter` adds a new line. A header line shows the active model and session.
- **Transcript** (middle) — your messages and the agent's streaming reply. Tool calls (file edits, shell, search, computer-use) render inline as collapsible blocks so you can watch what the agent is doing in real time.
- **Panels** — activity log, approvals queue, artifacts gallery, and the subagent sidebar slide in over the transcript on demand (see [Panels](#panels) below).

## Sending messages

Type a message and press `Enter`. The agent's reply streams token by token. While it's still working:

- **Keep typing.** Anything you enter while the agent is streaming is **queued** above the composer and sent automatically when the current turn finishes — you don't have to wait.
- **Interrupt.** `Ctrl+C` aborts the current turn (press it again when idle to quit). `/abort` does the same from the composer.

## Slash commands

Type `/` as the first character to run a **command** instead of sending a message to the agent. A palette appears as you type; `?` in an empty composer prints a quick cheat sheet, and `/help` (or `F1`) opens the full help modal.

A few you'll reach for constantly:

| Command | Does |
|---|---|
| `/new` (`/clear`) | Start a fresh conversation |
| `/model`, `/provider` | Switch model / LLM provider live (see below) |
| `/sessions` | Jump to a saved session |
| `/retry`, `/undo` | Re-run the last message / pop the last turn to edit it |
| `/compact [hint]` | Summarize history to reclaim context tokens |
| `/<skill-name>` | Invoke an installed skill for one turn (e.g. `/research`) |

The full list — session, model, tools, permissions, account, and the keybindings — lives in **[Slash commands](../reference/slash-commands.md)**.

## Switching model & provider

`/provider` and `/model` open in-flow pickers at the bottom of the screen (an arrow-key list you confirm with `Enter`). They take effect immediately, mid-conversation, with no restart:

```text
/provider openrouter        # or just /provider for the picker
/model claude-sonnet-4-5    # catalog is live for OpenRouter
```

`/provider <key>` switches directly; `/provider off` clears the pin and lets the cascade choose. See [Providers and models](./providers-and-models.md) for the cascade and the full catalog.

## Personas

Switch the agent's persona (its voice and default behavior) with `/assistants` (alias `/persona`), or press **`Ctrl+M`** for the picker. See [Personas](./personas.md).

## Sessions & history

Conversations are saved automatically. Press **`Ctrl+S`** (or `/sessions`) to browse and switch between saved sessions. In a single-line draft, `↑` / `↓` walk back through your **input history** (the messages you've sent), so you can recall and resend a prompt. Quitting with `Ctrl+D` persists the current session. Full details — where sessions live, how history is compacted, resuming — are in [Sessions](./sessions.md).

## Editing a long draft

For anything longer than a quick line, press **`Ctrl+E`** to open the current draft in your `$EDITOR` (vim, nano, VS Code, …). Save and close the editor and the text drops back into the composer, ready to send. Handy for multi-paragraph prompts, pasted code, or careful edits.

## Attaching images & video

| Command | Attaches |
|---|---|
| `/image <path>` | An image file to the next message (`/image clear` removes pending) |
| `/paste` | An image from the system clipboard |
| `/video <path>` | A video for analysis via the `video_analyze` tool |

Attachments ride your next message; the agent sees them alongside your text. See [Image generation](../features/image-generation.md) for the reverse direction (the agent *producing* images).

## Panels

Function keys open full-height panels over the transcript; `Esc` closes any of them:

| Key | Panel | What it shows |
|---|---|---|
| `F1` | **Help** | Every command and keybinding |
| `F2` | **Activity** | The live [audit log](../features/audit-log.md) — every tool call and result |
| `F3` | **Approvals** | The pending [approvals](./sandbox-and-approvals.md) queue — approve/deny shell commands and tools |
| `F4` | **Artifacts** | The [artifacts](../features/artifacts.md) gallery — canvases, docs, code the agent produced |
| `Ctrl+A` | **Subagents** | A sidebar tracking [delegated](../features/delegation.md) sub-agents |

You can also bring the [task board](../features/board.md) inline with `/board`, and connect tools/channels without leaving the chat via `/integrations`, `/channels`, `/mcp`, and `/plugins`.

## Shell escape

Prefix a line with `!` to run a bash command **locally** — it never goes to the LLM. The output appears as a code block in the transcript (30-second timeout, 4000-character cap):

```bash
!git status
!ls -la ~/.flowly
```

This is for *your* quick checks; the agent has its own sandboxed shell tool governed by [approvals](./sandbox-and-approvals.md).

## Keybindings at a glance

| Key | Action |
|---|---|
| `Enter` / `Shift+Enter` | Send / new line |
| `↑` / `↓` | Input history prev / next |
| `Ctrl+E` | Edit draft in `$EDITOR` |
| `Ctrl+S` / `Ctrl+M` | Sessions / personas picker |
| `Ctrl+A` | Toggle subagent sidebar |
| `F1`–`F4` | Help · Activity · Approvals · Artifacts |
| `Ctrl+C` | Abort the run, or quit when idle |
| `Ctrl+L` | Clear the session |
| `Ctrl+D` | Quit (saves the session) |
| `Esc` | Close a modal/panel |

The complete table, including every slash command, is in [Slash commands](../reference/slash-commands.md).

## Related

- [Slash commands](../reference/slash-commands.md)
- [Sessions](./sessions.md)
- [Personas](./personas.md)
- [Providers and models](./providers-and-models.md)
- [Sandbox and approvals](./sandbox-and-approvals.md)
- [CLI commands](../reference/cli-commands.md)
- [Quickstart](../getting-started/quickstart.md)
