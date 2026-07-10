"""Help modal — categorized list of keybindings & slash commands."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown

HELP_BODY = """
# Flowly TUI · Help

## Keybindings
| Key | Action |
|---|---|
| `Enter`        | Send message |
| `Shift+Enter`  | New line in draft |
| `↑` / `↓`      | History prev/next; `↓` on an empty draft selects this chat's artifacts |
| `Ctrl+E`       | Open draft in `$EDITOR` (vim/nvim/nano) |
| `Ctrl+S`       | Open sessions picker |
| `Ctrl+M`       | Open assistants / persona picker |
| `Ctrl+A`       | Toggle subagent tree sidebar |
| `F1`           | This help modal |
| `F2`           | Activity / audit log |
| `F3`           | Pending approvals queue |
| `F4`           | Artifacts gallery |
| `Ctrl+C`       | Abort current run, or quit if idle |
| `Ctrl+L`       | Clear session (gateway-side) |
| `Ctrl+D`       | Quit (also persists current session) |
| `Esc`          | Close modals |

## Slash commands
| Command | What it does |
|---|---|
| `/help`              | Show this modal |
| `/clear`             | Wipe the current session's history (asks confirmation) · `/clear --yes` skips prompt |
| `/new`               | Start a fresh session — leaves the current one intact |
| `/retry`             | Re-submit the last user message (drops stale assistant reply) |
| `/undo`              | Pop last user+assistant turn · pre-fills composer for edit-and-resubmit |
| `/compact [hint]`    | Summarize history to save tokens |
| `/sessions`          | Switch saved session |
| `/assistants` `/model` | Pick a persona / assistant |
| `/integrations`      | Connect external services like Home Assistant, Linear, Trello, Google Workspace |
| `/channels`          | Configure messaging channels like Telegram, Slack, Discord, iMessage, Email, iOS/Web |
| `/provider`          | Pick LLM provider (arrow-key picker) · `/provider <key>` direct switch · `/provider off` clear |
| `/model`             | Pick model from active provider's catalog (OpenRouter live) |
| `/theme`             | Switch TUI theme · `/theme mono` direct switch |
| `/image <path>`      | Attach an image to the next message · `/image clear` removes pending images |
| `/video <path>`      | Attach a video for analysis via `video_analyze` |
| `/paste`             | Attach an image from the system clipboard |
| `/learn [--dry-run] [source]` | Create or update a reusable skill from paths, URLs, notes, or this chat |
| `/browser`           | Toggle browser_tab · Chrome extension link + live status |
| `/plugins`           | List installed plugins (bundled + user) · toggle enabled |
| `/mcp`               | Manage MCP servers · enable/disable/remove · install from catalog |
| `/login`             | Pair this machine for iOS access |
| `/logout`            | Unpair (disable iOS access) |
| `/whoami`            | Show pairing status + account info |
| `/status`            | Show current model + session |
| `/usage`             | Session token & cost breakdown for the active provider (+ context window) |
| `/activity`          | Activity / audit log (or F2) |
| `/approvals`         | Pending approvals queue (or F3) |
| `/permissions`       | Edit command permissions (security/ask/allowlist) |
| `/artifacts`         | Artifacts gallery (or F4) |
| `/memory` `/review`  | Review new memories inline — keep / discard the bot's pending candidates (also pops on open) |
| `/board` `/kanban`   | Show the task board inline (status groups) |
| `/subagents` `/subs` | Toggle subagent sidebar |
| `/subagents models`  | Set which model each specialist runs on |
| `/subagents <task>`  | Launch a manual background subagent |
| `/abort`             | Cancel the current turn |
| `/quit`              | Exit |

## Command-line entry points
Run these from a regular shell — they work without launching the TUI.

| Command | What it does |
|---|---|
| `flowly`                   | Smart entry: opens this TUI when a provider is configured, else opens the first-run provider picker |
| `flowly login`             | Sign in with Flowly account (zero API keys, OAuth — opens browser) |
| `flowly login --repair`    | Re-wire relay config using existing tokens (no browser) — recovers from manually-edited or partially-restored config |
| `flowly login --repair --dry-run` | Show what `--repair` would change without writing anything |
| `flowly logout`            | Clear keychain tokens + relay config + active provider (mirrors `/logout`) |
| `flowly doctor`            | Read-only health check — tokens, relay, provider, gateway, sessions |
| `flowly doctor --fix`      | Auto-repair the issues `flowly doctor` flagged as fixable |
| `flowly setup`             | Open the TUI's provider picker (the one mandatory setup step) |
| `flowly setup channels`    | Open the channels catalog (Telegram / Discord / Slack) |
| `flowly setup tools`       | Open the integrations catalog (browser, voice, …) |
| `flowly setup byok <slug> --key <k>` | Quick BYOK one-shot: save key + (optionally) set active — for CI / dotfile bootstrap |
| `flowly`                   | Launch this TUI (when a provider is configured) |
| `flowly --theme catppuccin` | Launch with a specific TUI theme (catppuccin, synthwave, gruvbox...) |
| `flowly gateway`           | Run the gateway daemon (Telegram/iOS/Discord channels go through it) |
| `flowly status`            | One-line snapshot of model + provider + session |
| `flowly memory list`       | Show active long-term memories |
| `flowly memory review`     | Memories awaiting your review — accept / reject |
| `flowly memory dream`      | Learn durable facts from recent chats now (the cross-session "dreamer"; also runs automatically on idle/daily) |
| `flowly memory consolidate` | Merge duplicate + retire stale memories |
| `flowly --help`            | Full command tree with options |

## Status bar badges
| Glyph | Meaning |
|---|---|
| `● ready` / `⠹ thinking` / `○ offline` | Connection / busy state |
| `⚠ N approval` | N pending exec approvals — press F3 |
| `◆ N` | N artifacts available — press F4 |
| `1.2k↑ 345↓` | Tokens in / out for this session |

## Shell escape
- `!cmd` runs **bash command locally** — never sent to the LLM.
  Examples: `!ls`, `!git status`, `!pwd`.
- Output appears as a code block in the transcript.
- 30s timeout, 4000 char cap.

## Queueing
- Type while the agent is streaming: messages **queue** above the input.
- Queued messages auto-send when the current turn ends.
- Queue count shows in the status bar.

## Streaming
- A blinking `▌` cursor in the assistant bubble means tokens are still arriving.
- Tool calls render as colored lines with live elapsed seconds.
- The status bar shows an animated busy ticker (`thinking · 3.1s`).

## Sessions
- Sessions persist at `~/.flowly/data/sessions/`.
- TUI remembers your last session in `~/.flowly/tui_state.json`.
- Each launch starts fresh; `flowly --resume` lists recent sessions to reopen, `-s key` opens one directly.

## Sync scope
- **CLI sessions stay local.** Everything you type here is saved only
  to `~/.flowly/sessions/*.jsonl` on this machine. Nothing in the CLI
  ever goes to Firestore or the relay.
- **iOS / desktop / Android chats auto-sync** to your Flowly account
  via the relay — but only when you're signed in (`/login`). Those
  chats are a separate stream; they don't mix with this CLI session.
- `/login` pairs this machine so your iOS app can reach the gateway
  for tool execution. It does **not** start syncing CLI chats.
- The status bar's `🔒 local` badge is a permanent reminder that
  the active CLI session is on-disk-only.

_Press Esc or click outside to close this help._
"""


class HelpModal(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > Vertical {
        width: 90%;
        max-width: 110;
        height: 90%;
        max-height: 40;
        border: thick #00a6c8;
        background: #050505;
    }
    HelpModal VerticalScroll {
        padding: 1 2;
        background: #050505;
    }
    HelpModal Markdown {
        background: #050505;
    }
    """

    BINDINGS = [
        ("escape", "dismiss(None)", "Close"),
        ("q", "dismiss(None)", "Close"),
        ("?", "dismiss(None)", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Markdown(HELP_BODY)
