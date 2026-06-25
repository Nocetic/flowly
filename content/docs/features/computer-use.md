---
title: Computer Use
eyebrow: Features
description: Control your macOS desktop — mouse, keyboard, screen capture, and UI automation — through the `computer` tool, driving apps via the macOS Accessibility tree and screen capture.
---

Flowly can capture the screen on its own through the `screenshot` tool. Computer use drives applications via the macOS Accessibility (AX) tree and screen capture, backed by the Desktop helper.

## Requirements

- **macOS.** The `computer` tool is macOS-only.
- **The Desktop helper.** UI automation and screen capture go through the native helper.
- **`tools.computer.enabled` must be `true`** in `~/.flowly/config.json`. The tool registers only when this gate is set.

## `computer`

A single `action` parameter selects the operation. The primary workflow is **AX-direct**: read a window's accessibility tree as structured JSON, then address elements by their index inside that snapshot — never by guessing pixel coordinates.

### AX-direct workflow (preferred)

| Action | Purpose |
|---|---|
| `activate_app` | Bring an app to the foreground (always do this first) |
| `launch_app` | Start an app if it is not running |
| `read_window_state` | JSON snapshot: `{snapshot_id, elements: [{index, role, title, description, value, actions, enabled}]}` |
| `find_element` | Read-only lookup; returns the element's index + `snapshot_id` |
| `press_by_title` | Find an element by AX title/description/value and dispatch an AX action (preferred for buttons) |
| `click_element_ax` | Dispatch an AX action on an element you already located, by `pid` + `snapshot_id` + `element_index` |
| `set_element_value` | Write to a specific text field by index |
| `clear_and_type` | Replace the focused element's content |
| `set_value` | Set a value |
| `key` | Keystroke combos (`Return`, `cmd+a`, `tab`) |
| `read_focused_text` | Read the focused element's text |
| `read_window_text` | Dump a window's AX-tree text |
| `wait` | Wait |

Semantic AX action shortcuts — `press`, `open`, `show_menu`, `pick`, `confirm`, `cancel` — route internally to `click_element_ax`, so the agent can express intent with a single `action`.

### Pointer, keyboard, and capture

| Action | Purpose |
|---|---|
| `click` / `double_click` / `move` | Pointer actions (AX-direct, or real screen coordinates) |
| `type` / `paste` | Enter text |
| `scroll` | Scroll |
| `drag` | Drag |
| `cursor_position` / `screen_size` | Pointer / display geometry |
| `clipboard_read` / `clipboard_write` | Read or write the clipboard |
| `screenshot` / `capture_window` | Capture the screen or a specific window |

### Environment introspection

| Action | Purpose |
|---|---|
| `list_apps` | List running apps |
| `list_windows` / `window_list` | List windows |
| `list_displays` | List displays |
| `frontmost_window_id` | ID of the frontmost window |

A typical flow: `activate_app` (or `launch_app`) → `read_window_state` to find the target field by role/title → `click_element_ax` or `press_by_title` to focus it → `clear_and_type` → `key('Return')` to submit → `read_focused_text` / `read_window_text` to verify.

## `screenshot`

Captures a display to an image file and returns the file path. Parameters:

| Parameter | Description |
|---|---|
| `display` | Display number to capture. `0` is the main monitor, `1` the secondary, etc. Default `0`. |
| `filename` | Optional custom filename (without extension). Defaults to a timestamp-based name. |
| `format` | `png` or `jpg`. Default `png`. |

Screenshots are saved under `~/.flowly/screenshots/`. To send a capture to the user, the agent uses the `message` tool with the screenshot's path.

## Limitations

> [!WARNING]
> **Tool success means the helper call landed, not that the user-visible effect happened.** Especially in terminal / TUI apps, a `clear_and_type` may be swallowed by the app's input handler. The agent should verify with `read_focused_text` / `read_window_text` rather than trust a success flag alone.

> [!NOTE]
> **`FOCUS_LOST` errors** mean the target app dropped out of the foreground; the app must be re-activated and `read_window_state` re-issued before retrying.

> [!WARNING]
> **Coordinate clicks are a fallback only.** Clicking without an AX snapshot can fall into a legacy resolver that defaults to the screen origin.

## Related

- [Browser control](browser.md)
- [MCP](mcp.md)
- [Tools reference](../reference/tools.md)
- [Slash commands](../reference/slash-commands.md)
