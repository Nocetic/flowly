---
title: Slash Commands
eyebrow: Reference
description: Commands you type in the Flowly TUI to control sessions, models, tools, and channels — plus keybindings and the shell escape.
---

Inside the Flowly TUI, type `/` to run a command instead of sending a message to the agent. Type `?` in an empty composer for a quick cheat sheet, or open the full help modal with `/help` (or `F1`).

## Session

| Command | What it does |
|---|---|
| `/clear` or `/new` | Reset the conversation (gateway-side, asks confirmation). `/clear --yes` skips the prompt. |
| `/retry` | Re-submit the last user message (drops the stale assistant reply). |
| `/undo` | Pop the last user + assistant turn; pre-fills the composer for edit-and-resubmit. |
| `/compact [hint]` | Summarize history to save tokens. |
| `/sessions` | Switch to a saved session. |
| `/status` | Show the current model + session. |
| `/abort` | Cancel the current turn. |
| `/quit` | Exit. |

## Model & persona

| Command | What it does |
|---|---|
| `/provider` | Pick the LLM provider (arrow-key picker). `/provider <key>` switches directly; `/provider off` clears. |
| `/model` | Pick a model from the active provider's catalog (live for OpenRouter). |
| `/assistants` (`/persona`) | Pick a persona / assistant. |
| `/theme` | Switch the TUI theme. `/theme mono` switches directly. |

## Tools & integrations

| Command | What it does |
|---|---|
| `/integrations` | Connect external services — Home Assistant, Linear, Trello, Google Workspace. |
| `/channels` | Configure messaging channels — Telegram, Slack, Discord, Email, iOS/Web. |
| `/mcp` | Manage MCP servers — enable / disable / remove, install from the catalog. |
| `/plugins` | List installed plugins (bundled + user) and toggle them. |
| `/browser` | Toggle the `browser_tab` tool; shows the Chrome extension link + live status. |
| `/computer` | Explains that computer use (desktop control) needs the [Flowly Desktop app](/docs/features/computer-use) — the terminal can't hold the required macOS permissions. |
| `/image <path>` | Attach an image to the next message. `/image clear` removes pending images. |
| `/video <path>` | Attach a video for analysis via `video_analyze`. |
| `/paste` | Attach an image from the system clipboard. |
| `/skills [filter]` | Search and manage skills the loader knows about (read-only; optional substring filter). Works in the CLI + gateway. |
| `/learn [--dry-run] [source]` | Create or update a reusable skill from paths, URLs, notes, or the current conversation. |
| `/codex [on\|off\|sandbox\|cwd\|tools]` | Manage the opt-in Codex runtime live (no restart). |
| `/<skill-name>` | Invoke an installed skill for one turn (e.g. `/research`). |

### `/learn`

Create or update a reusable skill from source material you provide:

```text
/learn [source]
/learn --dry-run [source]
```

`[source]` can be a local path, directory, URL, pasted notes, or a phrase such as
"what we just did". With no source, Flowly uses the current conversation as the
material to distill.

Examples:

```text
/learn the release checklist workflow from this conversation
/learn ~/work/internal-sdk/docs/auth.md
/learn https://example.com/api-guide and these notes: ...
/learn --dry-run ./runbooks/customer-escalation.md
```

Normal mode saves the skill through `skill_manage`: it lists existing
agent-authored skills to avoid obvious duplicates, creates or updates the skill,
writes supporting files when needed, and verifies the result. New or updated
skills appear in the TUI slash palette automatically after a successful
`skill_manage` or `skill_improve` write in the active session.

Dry run mode previews the same plan without writing. It may inspect sources and
list existing skills, but it must not call persistent `skill_manage` actions
such as `create`, `patch`, `edit`, `write_file`, or `delete`. The reply includes
the proposed skill name, create/update decision, full `SKILL.md` draft,
supporting file drafts, verification check, and the command to run when you want
to apply it.

Short alias: `-n` is accepted as a dry-run flag.

## Permissions & activity

| Command | What it does |
|---|---|
| `/permissions` (`/policy`) | Edit command permissions (security / ask / allowlist). |
| `/plan [task\|on\|off\|status]` | [Plan mode](/docs/features/plan-mode) — the agent proposes a plan and waits for approval before acting. Bare `/plan` toggles the standing mode; see below. |
| `/memory` (`/review`) | Review the bot's pending memory candidates inline — keep / discard them one at a time. Pops automatically on open when the review queue is non-empty; `Esc` dismisses it until you re-enter the TUI. |
| `/approvals` | Open the pending approvals queue (or `F3`). |
| `/activity` | Open the activity / audit log (or `F2`). |
| `/artifacts` | Open the artifacts gallery (or `F4`). |
| `/subagents` (`/subs`) | Toggle the subagent sidebar. |
| `/board` (`/kanban`) | Show the [task board](/docs/features/board) inline; `/board run\|done\|cancel\|del <id>`, `/board add <title>`, `/board clear`. |

### `/plan`

Turn on [plan mode](/docs/features/plan-mode): the agent decomposes the task into
steps, shows you the plan, and runs no side-effecting tool until you approve it.

```text
/plan                 # toggle the standing mode on/off
/plan on              # turn it on
/plan off             # turn it off AND abort the active plan
/plan status          # is it on? what's the active plan's progress?
/plan <task>          # plan THIS task only, without entering the standing mode
```

Bare `/plan` is a **mode**, not a one-shot: while it's on, every message plans
first. It's the same mode `Shift+Tab` cycles to (**▣ Plan**), and it works on
every surface that sends text — the TUI, Desktop, iOS, and chat channels.

`/plan <task>` is the exception: it forces plan-first for that message only.

Aliases: `/plan stop` and `/plan cancel` behave like `/plan off`; `/plan ?`
behaves like `/plan status`.

## Account

| Command | What it does |
|---|---|
| `/login` | Pair this machine for iOS access. |
| `/logout` | Unpair (disable iOS access). |
| `/whoami` | Show pairing status + account info. |

## Keybindings

| Key | Action |
|---|---|
| `Enter` | Send message |
| `Shift+Enter` | New line in the draft |
| `Shift+Tab` | Cycle the permission level: 🔒 Ask → ⚖️ Auto → 🚀 YOLO → ▣ [Plan](/docs/features/plan-mode) |
| `↑` / `↓` | History prev / next (single-line draft) |
| `Ctrl+E` | Open the draft in `$EDITOR` |
| `Ctrl+S` | Open the sessions picker |
| `Ctrl+M` | Open the assistants / persona picker |
| `Ctrl+A` | Toggle the subagent sidebar |
| `F1` … `F4` | Help · Activity · Approvals · Artifacts |
| `Ctrl+C` | Abort the current run, or quit if idle |
| `Ctrl+L` | Clear the session (gateway-side) |
| `Ctrl+D` | Quit (persists the current session) |
| `Esc` | Close modals |

## Shell escape

Prefix a line with `!` to run a bash command **locally** — it is never sent to the LLM. Output appears as a code block in the transcript.

```bash
!ls
!git status
!pwd
```

There is a 30-second timeout and a 4000-character output cap.

> [!TIP]
> While the agent is streaming, anything you type is **queued** above the input and sent automatically when the current turn ends.

## Related

- [CLI commands](cli-commands.md)
- [Tools](tools.md)
- [Sessions](../using-flowly/sessions.md)
