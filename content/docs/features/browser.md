---
title: Browser Control
eyebrow: Features
description: Drive your real Chrome browser — your actual logged-in tabs and sessions — through a companion Chrome extension, with a plan-and-evidence layer to keep multi-step tasks honest.
---

Two tools power this: `browser_tab` for the actions, and `browser_plan` for keeping multi-step browser tasks honest.

> [!NOTE]
> Both tools are off by default. They register only when `tools.browserTab.enabled` is `true` in `~/.flowly/config.json`; `browser_plan` registers alongside `browser_tab`.

## `browser_tab`

Drives your real Chrome through the Flowly extension. A single `action` parameter selects what to do, with per-action arguments. The available actions:

| Action | Purpose |
|---|---|
| `read_page` | Read the current page |
| `get_page_text` | Extract page text |
| `navigate` | Go to a URL |
| `click` | Click an element |
| `type` | Type text |
| `form_input` | Fill a form field |
| `upload_file` | Upload a file |
| `upload_image` | Upload an image |
| `hover` | Hover over an element |
| `find` | Find an element |
| `wait` | Wait |
| `evaluate` | Evaluate JavaScript |
| `console_log` | Read console logs |
| `dialog` | Handle a dialog |
| `screenshot` | Capture the page |
| `scroll` | Scroll |
| `key` | Send a keystroke |
| `tabs_list` | List open tabs |
| `tabs_create` | Open a new tab |
| `tabs_close` | Close a tab |
| `tabs_context` | Tab context |
| `read_network_requests` | Read network requests |
| `batch` | Run a batch of actions |

Because it operates on your real browser, the agent works inside whatever sites you are already signed in to — no separate login or headless session.

## `browser_plan`

An explicit plan-and-evidence layer for browser tasks. Long browser flows tend to drift after many tool calls — the agent forgets what it was doing or reports success when the page state does not match the request. `browser_plan` solves this by making the agent commit to a plan up front and verify each step with evidence before marking it done.

It has four actions:

| Action | Purpose |
|---|---|
| `create` | Start a plan (`goal`, `steps`) |
| `view` | View the current plan |
| `update_step` | Update a step's status (must attach evidence before `done`) |
| `complete` | Complete the plan |

Each step carries a `successCriteria` and an `evidence` slot — a screenshot description, DOM observation, or URL change — that must be filled before the step can move to `done`.

## How the connection works

`browser_tab` talks to the Flowly Chrome extension, which controls your real browser tabs. You enable the tool and confirm the extension is connected through the `/browser` modal (below).

> [!IMPORTANT]
> Until the extension is installed and connected, `browser_tab` actions cannot reach the browser.

## The `/browser` slash command

In the TUI, `/browser` opens the Browser Use modal, where you can:

- Toggle the `browser_tab` enable flag on or off.
- See live extension-connection status.
- Open the Chrome Web Store to install the extension if it is not installed yet.

Saving the toggle restarts the gateway so the change takes effect; the TUI confirms with `browser_tab enabled` / `disabled · gateway restarted`.

## Configuration

| Key | Purpose |
|---|---|
| `tools.browserTab.enabled` | Enables `browser_tab` (and `browser_plan`). Default off. |

> [!TIP]
> You can flip this key by hand in `~/.flowly/config.json`, but using `/browser` is preferred since it also restarts the gateway for you.

## Related

- [Computer use](computer-use.md)
- [MCP](mcp.md)
- [Tools reference](../reference/tools.md)
- [Slash commands](../reference/slash-commands.md)
