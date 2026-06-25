"""Computer use tool — desktop automation via the native macOS helper,
Electron HTTP delegation, or in-process OS-native shell-out.

Dispatch chain (priority order):
1. Native Swift helper (Flowly Desktop, macOS 14+, AX-direct)
2. Electron HTTP delegation (desktop app running, legacy path)
3. OS-native shell-out (osascript+cliclick / xdotool / PowerShell)
"""

from __future__ import annotations

import asyncio
import functools
import json
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool

def _electron_api_file() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "electron-api.json"
_PLATFORM = sys.platform  # "darwin", "linux", "win32"


# ---------------------------------------------------------------------------
# AppleScript helpers (macOS native fallback)
# ---------------------------------------------------------------------------

def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


_MAC_KEY_CODES: dict[str, int] = {
    "return": 36, "enter": 36, "tab": 48, "space": 49,
    "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MAC_MODIFIERS: dict[str, str] = {
    "cmd": "command down", "command": "command down",
    "ctrl": "control down", "control": "control down",
    "alt": "option down", "option": "option down",
    "shift": "shift down",
}


def _build_mac_osascript_key(keys: str) -> list[str]:
    """Build osascript args for a key combo like 'cmd+c'."""
    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    modifiers: list[str] = []
    key = ""
    for part in parts:
        if part in _MAC_MODIFIERS:
            modifiers.append(_MAC_MODIFIERS[part])
        else:
            key = part

    mod_str = f" using {{{', '.join(modifiers)}}}" if modifiers else ""

    if key in _MAC_KEY_CODES:
        script = f'tell application "System Events" to key code {_MAC_KEY_CODES[key]}{mod_str}'
    else:
        escaped = _escape_applescript(key)
        script = f'tell application "System Events" to keystroke "{escaped}"{mod_str}'

    return ["osascript", "-e", script]


def _build_xdotool_key(keys: str) -> str:
    """Convert key combo to xdotool format: 'cmd+c' → 'ctrl+c'."""
    mapping = {
        "cmd": "ctrl", "command": "ctrl", "option": "alt",
        "esc": "Escape", "enter": "Return", "return": "Return",
        "del": "BackSpace", "delete": "BackSpace", "backspace": "BackSpace",
        "tab": "Tab", "space": "space",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    }
    return "+".join(mapping.get(k.strip().lower(), k.strip()) for k in keys.split("+"))


def _escape_sendkeys(text: str) -> str:
    """Escape special SendKeys characters for Windows."""
    special = set("^%~+{}()")
    return "".join(f"{{{c}}}" if c in special else c for c in text)


# ---------------------------------------------------------------------------
# ComputerTool
# ---------------------------------------------------------------------------

class ComputerTool(Tool):
    """Desktop automation: AX-direct (native helper) + OS-native fallback."""

    def __init__(self, config: Any = None, screenshot_tool: Any = None):
        from flowly.config.schema import ComputerConfig
        self._config = config or ComputerConfig()
        self._screenshot_tool = screenshot_tool

    @functools.cached_property
    def _has_cliclick(self) -> bool:
        return shutil.which("cliclick") is not None

    @property
    def name(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        base = (
            "Control the desktop — mouse, keyboard, screen capture, and UI automation.\n\n"
            "PRIMARY workflow is AX-direct: read the window's accessibility tree as\n"
            "structured JSON, address elements by index inside that snapshot. NEVER\n"
            "guess pixel coordinates and NEVER call click without a snapshot_id —\n"
            "doing so falls into a legacy resolver that defaults to (0,0) and clicks\n"
            "the Apple menu in the corner of the screen.\n\n"
            "Actions (AX-direct, preferred):\n"
            "- activate_app(app_name) — bring an app to the foreground. ALWAYS first.\n"
            "- launch_app(bundle_id or app_name) — start an app if not running.\n"
            "- read_window_state(pid=) — JSON snapshot: {snapshot_id, elements: [{index, role, title, description, value, actions, enabled}]}. Use this to find UI elements.\n"
            "- press_by_title(pid, title, role?, press_action?) — find by AX title/description/value and dispatch an AX action (press/open/show_menu/pick/confirm/cancel). PREFERRED for buttons.\n"
            "- find_element(pid, title, role?) — read-only lookup, returns the element's index + snapshot_id.\n"
            "- click_element_ax(pid, snapshot_id, element_index, action='press') — dispatch an AX action on a specific element you already located via read_window_state / find_element.\n"
            "- set_element_value(pid, snapshot_id, element_index, value) — write to a specific text field by index.\n"
            "- clear_and_type(text) — replace the FOCUSED element's content (AX write, falls back to clipboard-paste on Chromium/Electron/terminal apps).\n"
            "- key(keys) — keystroke combos ('Return', 'cmd+a', 'tab').\n"
            "- read_focused_text() / read_window_text(pid=) — verify what's actually on screen.\n"
            "- list_apps / list_windows / list_displays / capture_window — environment introspection.\n\n"
            "Standard workflow for typing into an app's input:\n"
            "  1. launch_app or activate_app(app_name)\n"
            "  2. read_window_state(pid=) → identify the target field by role/title/description\n"
            "  3. click_element_ax(pid, snapshot_id, element_index, action='press') to focus it\n"
            "     OR press_by_title(pid, title, role) for a button you can name\n"
            "  4. clear_and_type(text='...') — content lands in the focused field\n"
            "  5. key(keys='Return') if you need to submit\n"
            "  6. read_focused_text() or read_window_text(pid=) — VERIFY the text actually appeared\n\n"
            "CRITICAL — click parameter contract:\n"
            "  Right: click(pid=X, snapshot_id='<uuid>', element_index=N) — AX-direct\n"
            "  Right: click(pid=X, x=NN, y=MM) — only if you have real screen coords\n"
            "  WRONG: click(element_id='0') or click(element_id='B3') — this routes to a\n"
            "  legacy resolver that returns success on lookup failure and silently clicks\n"
            "  the screen origin (Apple menu). If you're tempted to write element_id, you\n"
            "  meant press_by_title or click_element_ax with snapshot_id+element_index.\n\n"
            "VERIFICATION DOCTRINE: every tool call returns success when the helper call\n"
            "lands, NOT when the user-visible effect happened. Especially in terminal /\n"
            "TUI apps (Warp, iTerm, Claude Code) a successful clear_and_type may have\n"
            "been swallowed by a TUI input handler. After every type / click in a\n"
            "terminal: call read_focused_text or read_window_text and confirm your text\n"
            "is actually visible. Do NOT report 'sent ✓' from a success flag alone.\n\n"
            "If you get a FOCUS_LOST error, the target app dropped out of foreground.\n"
            "Call activate_app again, then re-issue read_window_state before retrying."
        )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "activate_app", "screenshot",
                        "click", "double_click", "move",
                        "type", "paste", "key", "scroll", "drag",
                        "cursor_position", "screen_size",
                        "clipboard_read", "clipboard_write",
                        "window_list",
                        # Phase 2 — AX-direct verbs + timing (macOS native helper).
                        "clear_and_type", "set_value", "read_focused_text", "wait",
                        # Window enumeration + targeted capture (vendored cua).
                        "list_windows", "frontmost_window_id", "capture_window",
                        # App enumeration (vendored cua).
                        "list_apps",
                        # Display enumeration.
                        "list_displays",
                        # AX-tree text dump per window.
                        "read_window_text",
                        # App launch (idempotent, no-focus-steal).
                        "launch_app",
                        # AX-direct UI tree snapshot + semantic click + value write.
                        "read_window_state", "click_element_ax", "set_element_value",
                        # Semantic AX action shortcuts — agent picks these
                        # by intent; they route to click_element_ax internally
                        # so the prompt doesn't need to teach a two-level
                        # action concept.
                        "press", "open", "show_menu", "pick", "confirm", "cancel",
                        # Title-based element addressing — saves the agent
                        # from counting AX-tree indices (which small models
                        # do badly). find_element returns the matching
                        # element's index; press_by_title finds + presses
                        # in one shot.
                        "find_element", "press_by_title",
                    ],
                },
                "app_name": {"type": "string", "description": "Application name (for activate_app, type, window_list, launch_app)"},
                "bundle_id": {"type": "string", "description": "Bundle id for launch_app (preferred over app_name, e.g. 'com.apple.Safari')"},
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "text": {"type": "string", "description": "Text to type, paste, set_value, clear_and_type, or write to clipboard"},
                "ms": {"type": "integer", "description": "wait duration in milliseconds (capped at 5000)"},
                "keys": {"type": "string", "description": "Key combo: 'cmd+c', 'enter', 'tab', 'alt+tab', 'f1'"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default: left)"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Scroll direction"},
                "amount": {"type": "integer", "description": "Scroll amount (default: 3)"},
                "start_x": {"type": "integer", "description": "Drag start X"},
                "start_y": {"type": "integer", "description": "Drag start Y"},
                "end_x": {"type": "integer", "description": "Drag end X"},
                "end_y": {"type": "integer", "description": "Drag end Y"},
                "display": {"type": "integer", "description": "Display index for screenshot (default: 0)"},
                "pid": {"type": "integer", "description": "Process ID for frontmost_window_id and other pid-targeted helpers"},
                "window_id": {"type": "integer", "description": "Window id from list_windows / frontmost_window_id, used by capture_window"},
                "format": {"type": "string", "enum": ["png", "jpeg"], "description": "Image format for capture_window (default: png)"},
                "quality": {"type": "integer", "description": "JPEG quality 1-100 for capture_window (ignored for png)"},
                "snapshot_id": {"type": "string", "description": "Snapshot id from read_window_state — pass to click_element_ax / set_element_value so stale indices are rejected"},
                "element_index": {"type": "integer", "description": "Index from read_window_state's elements[] — addresses an AX element for click_element_ax / set_element_value"},
                "title": {"type": "string", "description": "Exact AX title for find_element / press_by_title (case-insensitive)"},
                "title_contains": {"type": "string", "description": "Substring match on title/description/value for find_element / press_by_title"},
                "role": {"type": "string", "description": "AX role filter for find_element / press_by_title (e.g. 'AXButton', 'AXRow', 'AXSearchField')"},
                "press_action": {"type": "string", "enum": ["press", "open", "show_menu", "pick", "confirm", "cancel"], "description": "AX action that press_by_title performs after matching (default: press). Use 'open' for row-content like Spotify tracks or Finder files"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if not self._config.enabled:
            return json.dumps({"error": "Computer use disabled. Enable: tools.computer.enabled = true"})

        # Guard against a legacy call pattern: click({element_id: ...}).
        # element_id used to address elements from a now-removed UI-tree
        # backend; today every click must be either AX-direct (snapshot
        # + index) or coordinate-based. Refuse early with a directive
        # error so the LLM rewrites the call instead of falling through
        # to a tier that returns a misleading "success: true".
        if action in ("click", "double_click"):
            eid = kwargs.get("element_id")
            if eid is not None:
                eid_str = str(eid).strip()
                return json.dumps({
                    "error": (
                        f"click(element_id={eid_str!r}) is not supported. "
                        "To click an AX element from read_window_state, use: "
                        "click_element_ax(pid=<pid>, snapshot_id=<snapshot_id>, element_index=<index>, action='press'). "
                        "To press a button by label, use: press_by_title(pid=<pid>, title='<label>', role='AXButton'). "
                        "To click a screen coordinate, use: click(pid=<pid>, x=<x>, y=<y>)."
                    ),
                    "action": action,
                    "error_kind": "INVALID_PARAMS",
                    "retryable": False,
                })

        # Screenshot delegates to the screenshot tool
        if action == "screenshot":
            if self._screenshot_tool:
                return await self._screenshot_tool.execute(
                    display=kwargs.get("display", 0),
                    filename=kwargs.get("filename"),
                    format=kwargs.get("format", "png"),
                )
            return json.dumps({"error": "Screenshot tool not available"})

        try:
            return await self._dispatch(action, kwargs)
        except Exception as exc:
            logger.error("Computer {} error: {}", action, exc)
            return json.dumps({"error": str(exc), "action": action})

    # -- Dispatch chain --------------------------------------------------------
    #
    # Order matters — earlier tiers catch the action first. Each tier
    # can return:
    #   - None → action not handled by this tier, fall through.
    #   - dict with `error` key → terminal failure, surface upstream.
    #   - dict with success fields → done, return.
    #
    # Tier 0: native Swift helper bundled inside Flowly Desktop. Owns
    # the Apple-native automation surface (AX, CGEvent, NSWorkspace,
    # ScreenCaptureKit) on macOS 14+. Source: flowly-desktop/native/
    # macos-helper/. On non-macOS hosts and on macOS < 14 the helper
    # is unavailable; tier returns None and dispatch falls through.
    #
    # Tier 1: Electron HTTP /input — legacy cliclick/osascript path.
    # Covers macOS < 14 callers and any action the helper hasn't
    # implemented yet.
    #
    # Tier 2: in-process native shell-out (osascript/cliclick on macOS,
    # xdotool on Linux, PowerShell on Windows). Last-resort path when
    # Electron HTTP isn't reachable (e.g. Flowly Desktop closed).

    async def _dispatch(self, action: str, params: dict) -> str:
        # 0. Native Swift helper (priority).
        result = await asyncio.to_thread(self._execute_helper, action, params)
        if result is not None:
            return json.dumps({"action": action, **result})

        # 1. Electron HTTP /input — cliclick/osascript backend.
        result = await asyncio.to_thread(self._execute_electron, action, params)
        if result is not None:
            return json.dumps({"action": action, **result})

        # 2. OS-native fallback.
        if _PLATFORM == "darwin":
            result = await asyncio.to_thread(self._execute_darwin, action, params)
        elif _PLATFORM == "linux":
            result = await asyncio.to_thread(self._execute_linux, action, params)
        elif _PLATFORM == "win32":
            result = await asyncio.to_thread(self._execute_win32, action, params)
        else:
            result = {"error": f"Unsupported platform: {_PLATFORM}"}

        return json.dumps({"action": action, **result})

    # -- Helpers ---------------------------------------------------------------

    def _run(self, cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _delay(self) -> None:
        if self._config.action_delay_ms > 0:
            time.sleep(self._config.action_delay_ms / 1000)

    # -- Native Swift helper (Phase 1c) ---------------------------------------
    #
    # Talks to the macOS-native helper bundled inside Flowly Desktop's
    # app bundle. The Electron main process owns the JSON-RPC client;
    # we reach it over the same localhost HTTP server the screenshot
    # tool uses, with the same Bearer token (`~/.flowly/electron-api.json`).
    #
    # Returning None means "let the next dispatch tier handle this".
    # Returning a dict (success or error) means "I owned this call —
    # stop here". We classify Swift errors into terminal vs. transient:
    # AX_PERMISSION_DENIED / INVALID_PARAMS / APP_NOT_RUNNING are
    # terminal (re-trying via cliclick won't help); everything else
    # falls through to the legacy chain so a transient helper crash
    # doesn't break the agent.

    # Actions that ONLY the helper implements — no Electron HTTP or
    # native fallback covers them. Any helper error for these is terminal:
    # surface the helper's message to the agent rather than falling
    # through to a tier that returns the misleading "Unknown action".
    _HELPER_ONLY_ACTIONS: frozenset = frozenset({
        # AX-direct UI tree + semantic click (vendored cua).
        "read_window_state", "click_element_ax", "set_element_value",
        # Semantic action aliases — all route to click_element_ax.
        "press", "open", "show_menu", "pick", "confirm", "cancel",
        # AX-tree text dump + value reads.
        "read_window_text", "read_focused_text",
        # Phase 2 verbs that ONLY the helper handles.
        "clear_and_type", "set_value",
        # Window + display enumeration (no legacy tier knows these).
        "list_displays", "frontmost_window_id", "capture_window",
        # App enum + launch (legacy tiers don't have these either).
        "list_apps", "launch_app",
        # Unified permissions probe.
        "get_permissions",
        # Fuzzy element lookup + one-shot find-and-press. Legacy chain
        # has no equivalent; without this, an AX_ELEMENT_NOT_FOUND error
        # (which carries the helper's "Did you mean ...?" suggestions)
        # falls through to "Unknown action" and the agent never sees
        # why the title didn't match.
        "find_element", "press_by_title",
    })

    _HELPER_TERMINAL_KINDS: frozenset = frozenset({
        "AX_PERMISSION_DENIED",
        "INVALID_PARAMS",
        "APP_NOT_RUNNING",
        "HELPER_NOT_AVAILABLE",
        # FOCUS_LOST means the target app drifted out of frontmost
        # between activate_app and the input action. Falling through
        # to cliclick (Tier 1) would just type into the wrong window
        # silently — exactly the bug we built focus-locking to fix.
        # Keep it terminal so the agent gets the honest error and
        # can re-issue activate_app.
        "FOCUS_LOST",
        # Screen Recording is a distinct TCC bucket from Accessibility.
        # The user has to grant it explicitly; no legacy tier can take
        # over (the shell-out tier would just hit the same TCC wall).
        # Surface it loud so the agent can stop trying.
        "SCREEN_RECORDING_DENIED",
    })

    def _resolve_helper_method(self, action: str, params: dict) -> str | None:
        """
        Map Python's action enum to a Swift helper method name. Returns
        None when this action isn't routed to the helper (caller falls
        through to the Electron / native shell-out tier).
        """
        if _PLATFORM != "darwin":
            return None  # helper is macOS-only

        # Click family — Python uses one `click` action with `button`,
        # the helper splits into left_click / right_click. element_id
        # is blocked at the entry of execute() with a directive error,
        # so by the time we get here only coordinate / AX-direct clicks
        # arrive.
        if action == "click":
            button = (params.get("button") or "left").lower()
            if button == "right":
                return "right_click"
            if button == "middle":
                # Helper exposes left/right only; middle click is rare
                # enough to not warrant CGEvent code in v1.
                return None
            return "left_click"

        if action == "double_click":
            return "double_click"

        if action == "type":
            return "type"
        if action == "paste":
            return "paste"
        if action == "key":
            return "key"
        if action == "scroll":
            return "scroll"
        if action == "move":
            return "mouse_move"
        if action == "activate_app":
            return "activate_app"

        # Phase 2 — AX-direct verbs + timing. The agent picks these
        # names verbatim from the system prompt; we forward without
        # renaming so the wire-level method matches.
        if action == "clear_and_type":
            return "clear_and_type"
        if action == "set_value":
            return "set_value"
        if action == "read_focused_text":
            return "read_focused_text"
        if action == "wait":
            return "wait"

        # Window enumeration + targeted capture. Accept the legacy
        # `window_list` alias too so prompts that still use it route
        # to the same helper method.
        if action == "list_windows" or action == "window_list":
            return "list_windows"
        if action == "frontmost_window_id":
            return "frontmost_window_id"
        if action == "capture_window":
            return "capture_window"

        # App enumeration.
        if action == "list_apps":
            return "list_apps"

        # Display enumeration.
        if action == "list_displays":
            return "list_displays"

        # AX-tree text dump.
        if action == "read_window_text":
            return "read_window_text"

        # App launch (idempotent, distinct from activate_app).
        if action == "launch_app":
            return "launch_app"

        # AX-direct UI tree + semantic click.
        if action == "read_window_state":
            return "read_window_state"
        if action == "click_element_ax":
            return "click_element_ax"
        if action == "set_element_value":
            return "set_element_value"

        # Semantic action aliases — agent calls `computer(action="open",
        # pid=, snapshot_id=, element_index=)` and we forward to
        # click_element_ax with the alias as the inner ax-action name.
        # Keeps the agent prompt simple (single `action=` slot) and
        # routes intent → AX dispatch in one hop.
        if action in ("press", "open", "show_menu", "pick", "confirm", "cancel"):
            return "click_element_ax"

        # Title-based element discovery + composite press.
        if action == "find_element":
            return "find_element"
        if action == "press_by_title":
            return "press_by_title"

        return None

    def _build_helper_params(self, action: str, params: dict) -> dict | None:
        """
        Translate Python's loose params dict into the shape the Swift
        method expects. Returning None aborts the helper attempt and
        lets the next dispatch tier take over.
        """
        if action in ("click", "double_click", "move"):
            x = params.get("x")
            y = params.get("y")
            if x is None or y is None:
                return None
            return {"x": x, "y": y}
        if action == "type":
            text = params.get("text", "")
            if not isinstance(text, str):
                return None
            return {"text": text}
        if action == "paste":
            text = params.get("text", "")
            if not isinstance(text, str):
                return None
            return {"text": text}
        if action == "key":
            keys = params.get("keys", "")
            if not isinstance(keys, str) or not keys:
                return None
            return {"keys": keys}
        if action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            return {"direction": direction, "amount": amount}
        if action == "activate_app":
            # Accept either bundle_id (preferred) or app_name. Helper
            # rejects with INVALID_PARAMS when neither is populated;
            # we forward what the agent gave and let the helper validate.
            out: dict = {}
            bid = params.get("bundle_id")
            if isinstance(bid, str) and bid.strip():
                out["bundle_id"] = bid
            name = params.get("app_name")
            if isinstance(name, str) and name.strip():
                out["app_name"] = name
            if not out:
                return None
            return out

        # Phase 2 verbs.
        if action == "clear_and_type":
            text = params.get("text", "")
            if not isinstance(text, str):
                return None
            return {"text": text}
        if action == "set_value":
            text = params.get("text", "")
            if not isinstance(text, str):
                return None
            return {"text": text}
        if action == "read_focused_text":
            # No params required — helper introspects the frontmost app.
            return {}
        if action == "wait":
            # Accept int or float; helper caps at 5000ms anyway.
            ms = params.get("ms", 0)
            if isinstance(ms, bool) or not isinstance(ms, (int, float)):
                return None
            return {"ms": int(ms)}

        # Window enumeration + targeted capture.
        if action == "list_windows" or action == "window_list":
            return {}
        if action == "frontmost_window_id":
            pid = params.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            return {"pid": int(pid)}
        if action == "capture_window":
            window_id = params.get("window_id")
            if isinstance(window_id, bool) or not isinstance(window_id, (int, float)):
                return None
            out: dict = {"window_id": int(window_id)}
            fmt = params.get("format")
            if isinstance(fmt, str):
                f = fmt.lower()
                if f == "jpg":
                    f = "jpeg"
                if f in ("png", "jpeg"):
                    out["format"] = f
            quality = params.get("quality")
            if isinstance(quality, (int, float)) and not isinstance(quality, bool):
                q = int(quality)
                if 1 <= q <= 100:
                    out["quality"] = q
            return out

        # App enumeration.
        if action == "list_apps":
            return {}

        # Display enumeration.
        if action == "list_displays":
            return {}

        # AX-tree text dump.
        if action == "read_window_text":
            pid = params.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            return {"pid": int(pid)}

        # App launch — accept bundle_id (preferred) and/or app_name.
        # Helper requires at least one; if neither is set the helper
        # returns INVALID_PARAMS, so we let it report rather than
        # second-guessing the agent here.
        if action == "launch_app":
            out: dict = {}
            bid = params.get("bundle_id")
            if isinstance(bid, str) and bid.strip():
                out["bundle_id"] = bid
            name = params.get("app_name")
            if isinstance(name, str) and name.strip():
                out["app_name"] = name
            return out

        # AX-direct UI tree snapshot.
        if action == "read_window_state":
            pid = params.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            return {"pid": int(pid)}

        # AX-direct semantic click on a snapshot element.
        if action == "click_element_ax":
            pid = params.get("pid")
            element_index = params.get("element_index")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            if isinstance(element_index, bool) or not isinstance(element_index, (int, float)):
                return None
            out = {"pid": int(pid), "element_index": int(element_index)}
            snapshot_id = params.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id.strip():
                out["snapshot_id"] = snapshot_id
            act = params.get("action")
            if isinstance(act, str) and act.strip():
                out["action"] = act.lower()
            return out

        # AX-direct value write on a snapshot element.
        if action == "set_element_value":
            pid = params.get("pid")
            element_index = params.get("element_index")
            text = params.get("text", "")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            if isinstance(element_index, bool) or not isinstance(element_index, (int, float)):
                return None
            if not isinstance(text, str):
                return None
            out = {"pid": int(pid), "element_index": int(element_index), "text": text}
            snapshot_id = params.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id.strip():
                out["snapshot_id"] = snapshot_id
            return out

        # Semantic action aliases — same params as click_element_ax,
        # except `action` (the dispatcher slot) IS the AX action name.
        # We carry it through to the helper as the inner action.
        if action in ("press", "open", "show_menu", "pick", "confirm", "cancel"):
            pid = params.get("pid")
            element_index = params.get("element_index")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            if isinstance(element_index, bool) or not isinstance(element_index, (int, float)):
                return None
            out = {
                "pid": int(pid),
                "element_index": int(element_index),
                "action": action,
            }
            snapshot_id = params.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id.strip():
                out["snapshot_id"] = snapshot_id
            return out

        # find_element: title or title_contains required (helper rejects
        # if neither). pid required. role optional.
        if action == "find_element":
            pid = params.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            out = {"pid": int(pid)}
            title = params.get("title")
            if isinstance(title, str) and title.strip():
                out["title"] = title
            title_contains = params.get("title_contains")
            if isinstance(title_contains, str) and title_contains.strip():
                out["title_contains"] = title_contains
            role = params.get("role")
            if isinstance(role, str) and role.strip():
                out["role"] = role
            return out

        # press_by_title: pid + title (or title_contains) required.
        # Optional role filter + ax action name (default press).
        if action == "press_by_title":
            pid = params.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, (int, float)):
                return None
            out = {"pid": int(pid)}
            title = params.get("title")
            if isinstance(title, str) and title.strip():
                out["title"] = title
            title_contains = params.get("title_contains")
            if isinstance(title_contains, str) and title_contains.strip():
                out["title_contains"] = title_contains
            role = params.get("role")
            if isinstance(role, str) and role.strip():
                out["role"] = role
            # The "inner" AX action (press/open/etc.) for what to do
            # with the matched element. Note this is a different param
            # than the dispatcher's `action` slot — press_by_title
            # is the dispatcher action; the helper-side action is named
            # press_action so it doesn't collide.
            press_action = params.get("press_action") or params.get("ax_action")
            if isinstance(press_action, str) and press_action.strip():
                out["action"] = press_action.lower()
            return out

        return None

    def _execute_helper(self, action: str, params: dict) -> dict | None:
        method = self._resolve_helper_method(action, params)
        if method is None:
            return None  # not handled by helper — fall through

        helper_params = self._build_helper_params(action, params)
        if helper_params is None:
            # Couldn't map params cleanly — fall through to the legacy
            # Electron HTTP / native shell-out tier.
            return None

        if not _electron_api_file().exists():
            return None  # Flowly Desktop not running; legacy chain takes over

        try:
            api_data = json.loads(_electron_api_file().read_text())
            port, token = int(api_data["port"]), str(api_data["token"])
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            return None

        payload = json.dumps({"method": method, "params": helper_params}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/helper",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            # Network-level failure — Electron may be mid-restart, helper
            # binary missing on this build, etc. Fall through to legacy.
            logger.debug("helper request failed, falling through: {}", exc)
            return None
        except (ValueError, json.JSONDecodeError):
            return None

        if data.get("success"):
            # Wrap so the action result merges cleanly with the
            # downstream {"action": <name>, ...} envelope.
            result = data.get("result") or {}
            if not isinstance(result, dict):
                result = {"result": result}
            return {"success": True, **result}

        # Structured error from the helper. Decide: terminal (don't
        # retry on the legacy chain) vs. transient (fall through).
        err = data.get("error") or {}
        kind = err.get("kind", "INTERNAL_ERROR")
        # Helper-only actions have NO legacy fallback — neither Electron
        # HTTP nor the native shell-out tier implements read_window_state
        # / click_element_ax / set_element_value / press / open / etc.
        # If the helper failed for one of these, the error message is
        # the only signal the agent has to fix its approach (usually
        # "snapshot_id mismatch → re-read"). Treat ALL errors as
        # terminal for these actions so the helper's message reaches
        # the agent instead of getting clobbered by "Unknown action".
        helper_only = action in self._HELPER_ONLY_ACTIONS
        if not helper_only and kind not in self._HELPER_TERMINAL_KINDS:
            logger.debug(
                "helper non-terminal error, falling through: kind={} message={}",
                kind, err.get("message"),
            )
            return None

        # Pick the most informative message to hand back to the agent.
        # Helper-side `message` is detailed when the action carries real
        # context (e.g. find_element returns "no element matching ... .
        # Available AXButtons: 'Add', 'Subtract', ..."). The TS-side
        # `userMessage` is a fixed catalog string keyed by error kind,
        # short and UX-friendly — useful for permission deep-links but
        # opaque to the agent. Prefer the detailed helper message when
        # it actually carries more than the catalog default.
        helper_msg = err.get("message")
        user_msg = err.get("userMessage")
        if helper_msg and (not user_msg or len(helper_msg) > len(user_msg)):
            message = helper_msg
        else:
            message = user_msg or helper_msg or f"Computer Use error ({kind})"
        return {
            "error": message,
            "error_kind": kind,
            "retryable": bool(err.get("retryable", False)),
        }

    # -- Electron delegation ---------------------------------------------------

    def _execute_electron(self, action: str, params: dict) -> dict | None:
        # On Windows the Electron /input server only implements the read-only
        # cursor_position / screen_size queries — every mutating action (click,
        # type, key, scroll, move, drag, …) returns a "not supported on win32"
        # error. Forwarding those here would surface that error as a TERMINAL
        # result and mask the in-process PowerShell backend (_execute_win32),
        # which DOES implement them. Skip the Electron tier for mutating actions
        # on win32 so dispatch falls through to the native fallback.
        if _PLATFORM == "win32" and action not in ("cursor_position", "screen_size"):
            return None
        if not _electron_api_file().exists():
            return None
        try:
            api_data = json.loads(_electron_api_file().read_text())
            port, token = int(api_data["port"]), str(api_data["token"])
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            return None

        # Map actions for Electron endpoint
        electron_params = dict(params)
        electron_params["action"] = action

        # Electron /input doesn't handle see/activate_app/clipboard/window_list
        if action in ("see", "clipboard_read", "clipboard_write", "window_list"):
            return None

        if action == "activate_app":
            electron_params = {"action": "activate_app", "app_name": params.get("app_name", "")}

        payload = json.dumps(electron_params).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/input",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return None

    # -- Shared native helpers (cursor/screen) ---------------------------------

    def _native_cursor_position(self) -> dict:
        if _PLATFORM == "darwin" and self._has_cliclick:
            r = self._run(["cliclick", "p"])
            if r.returncode == 0:
                # cliclick p outputs: "x,y"
                parts = r.stdout.strip().split(",")
                if len(parts) == 2:
                    return {"success": True, "x": int(parts[0]), "y": int(parts[1])}
        if _PLATFORM == "linux" and shutil.which("xdotool"):
            r = self._run(["xdotool", "getmouselocation"])
            if r.returncode == 0:
                # "x:123 y:456 screen:0 ..."
                parts = {k: v for k, v in (p.split(":") for p in r.stdout.strip().split() if ":" in p)}
                return {"success": True, "x": int(parts.get("x", 0)), "y": int(parts.get("y", 0))}
        return {"error": "cursor_position not available on this platform"}

    def _native_screen_size(self) -> dict:
        if _PLATFORM == "darwin":
            r = self._run(["osascript", "-e",
                           'tell application "Finder" to get bounds of window of desktop'])
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    return {"success": True, "width": int(parts[2]), "height": int(parts[3])}
        if _PLATFORM == "linux" and shutil.which("xdotool"):
            r = self._run(["xdotool", "getdisplaygeometry"])
            if r.returncode == 0:
                parts = r.stdout.strip().split()
                if len(parts) == 2:
                    return {"success": True, "width": int(parts[0]), "height": int(parts[1])}
        return {"error": "screen_size not available on this platform"}

    # ==========================================================================
    # macOS NATIVE FALLBACK (osascript + cliclick)
    # ==========================================================================

    def _execute_darwin(self, action: str, params: dict) -> dict:
        app_name = params.get("app_name")

        if action == "activate_app":
            if not app_name:
                return {"error": "app_name required"}
            r = self._run(["osascript", "-e", f'tell application "{_escape_applescript(app_name)}" to activate'])
            self._delay()
            return {"success": True, "app": app_name} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action in ("click", "double_click"):
            if not self._has_cliclick:
                return {"error": "cliclick not found. Install: brew install cliclick"}
            x, y = params.get("x", 0), params.get("y", 0)
            if not x and not y:
                return {"error": "x and y coordinates required"}
            prefix = "dc" if action == "double_click" else "c"
            if params.get("button") == "right":
                prefix = "rc"
            r = self._run(["cliclick", f"{prefix}:{x},{y}"])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "move":
            if not self._has_cliclick:
                return {"error": "cliclick not found. Install: brew install cliclick"}
            r = self._run(["cliclick", f"m:{params.get('x', 0)},{params.get('y', 0)}"])
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "drag":
            if not self._has_cliclick:
                return {"error": "cliclick not found. Install: brew install cliclick"}
            sx, sy = params.get("start_x", 0), params.get("start_y", 0)
            ex, ey = params.get("end_x", 0), params.get("end_y", 0)
            r = self._run(["cliclick", f"dd:{sx},{sy}", f"du:{ex},{ey}"])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "type":
            text = params.get("text", "")
            if not text:
                return {"error": "text is required"}
            escaped = _escape_applescript(text)
            r = self._run(["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "key":
            keys = params.get("keys", "")
            if not keys:
                return {"error": "keys is required"}
            cmd = _build_mac_osascript_key(keys)
            r = self._run(cmd)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            lines = amount if direction == "up" else -amount
            r = self._run(["osascript", "-e",
                           f'tell application "System Events" to scroll area 1 of process "Finder" by {lines}'])
            # Scroll via osascript is unreliable; try cliclick if available
            if self._has_cliclick:
                scroll_dir = "u" if direction == "up" else "d"
                for _ in range(amount):
                    self._run(["cliclick", f"k{scroll_dir}:"])
            self._delay()
            return {"success": True}

        if action == "cursor_position":
            return self._native_cursor_position()
        if action == "screen_size":
            return self._native_screen_size()

        if action == "clipboard_read":
            r = self._run(["osascript", "-e", "get the clipboard as text"])
            return {"success": True, "text": r.stdout.strip()} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}
        if action == "clipboard_write":
            text = params.get("text", "")
            r = self._run(["osascript", "-e", f'set the clipboard to "{_escape_applescript(text)}"'])
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "window_list":
            script = 'tell application "System Events" to get name of every window of every process whose visible is true'
            r = self._run(["osascript", "-e", script])
            return {"success": True, "output": r.stdout.strip()[:2000]} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        return {"error": f"Unknown action: {action}"}

    # ==========================================================================
    # LINUX NATIVE (xdotool)
    # ==========================================================================

    def _execute_linux(self, action: str, params: dict) -> dict:
        if not shutil.which("xdotool"):
            return {"error": "xdotool not found. Install: sudo apt install xdotool"}

        app_name = params.get("app_name")

        if action == "activate_app":
            if not app_name:
                return {"error": "app_name required"}
            r = self._run(["xdotool", "search", "--name", app_name, "windowactivate"])
            self._delay()
            return {"success": True, "app": app_name} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action in ("click", "double_click"):
            x, y = params.get("x", 0), params.get("y", 0)
            if not x and not y:
                return {"error": "x and y coordinates required"}
            button = {"left": "1", "middle": "2", "right": "3"}.get(params.get("button", "left"), "1")
            repeat = "--repeat 2" if action == "double_click" else ""
            cmd = f"xdotool mousemove {x} {y} click {repeat} {button}".split()
            r = self._run([c for c in cmd if c])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "move":
            r = self._run(["xdotool", "mousemove", str(params.get("x", 0)), str(params.get("y", 0))])
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "drag":
            sx, sy = params.get("start_x", 0), params.get("start_y", 0)
            ex, ey = params.get("end_x", 0), params.get("end_y", 0)
            r = self._run(["xdotool", "mousemove", str(sx), str(sy),
                           "mousedown", "1", "mousemove", "--sync", str(ex), str(ey), "mouseup", "1"])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "type":
            text = params.get("text", "")
            if not text:
                return {"error": "text is required"}
            r = self._run(["xdotool", "type", "--clearmodifiers", "--", text], timeout=30)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "key":
            keys = params.get("keys", "")
            if not keys:
                return {"error": "keys is required"}
            r = self._run(["xdotool", "key", "--", _build_xdotool_key(keys)])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            button = "5" if direction == "down" else "4"
            r = self._run(["xdotool", "click", "--repeat", str(amount), button])
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "cursor_position":
            return self._native_cursor_position()
        if action == "screen_size":
            return self._native_screen_size()

        if action == "clipboard_read":
            r = self._run(["xclip", "-selection", "clipboard", "-o"])
            return {"success": True, "text": r.stdout.strip()} if r.returncode == 0 else {"error": "xclip not available"}
        if action == "clipboard_write":
            text = params.get("text", "")
            r = subprocess.run(["xclip", "-selection", "clipboard"], input=text, capture_output=True, text=True, timeout=5)
            return {"success": True} if r.returncode == 0 else {"error": "xclip not available"}

        if action == "window_list":
            r = self._run(["xdotool", "search", "--name", app_name or "", "getwindowname"])
            return {"success": True, "output": r.stdout.strip()[:2000]} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        return {"error": f"Unknown action: {action}"}

    # ==========================================================================
    # WINDOWS NATIVE (PowerShell)
    # ==========================================================================

    def _execute_win32(self, action: str, params: dict) -> dict:
        app_name = params.get("app_name")

        def ps(script: str, timeout: int = 10) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=timeout,
            )

        if action == "activate_app":
            if not app_name:
                return {"error": "app_name required"}
            r = ps(f'(New-Object -ComObject WScript.Shell).AppActivate("{app_name}")')
            self._delay()
            return {"success": True, "app": app_name} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action in ("click", "double_click"):
            x, y = params.get("x", 0), params.get("y", 0)
            if not x and not y:
                return {"error": "x and y coordinates required"}
            clicks = 2 if action == "double_click" else 1
            script = f"""
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Mouse {{
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, IntPtr dwExtraInfo);
}}
'@
[Mouse]::SetCursorPos({x}, {y})
Start-Sleep -Milliseconds 50
"""
            for _ in range(clicks):
                script += "[Mouse]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)\n"  # MOUSEEVENTF_LEFTDOWN
                script += "[Mouse]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)\n"  # MOUSEEVENTF_LEFTUP
                script += "Start-Sleep -Milliseconds 50\n"
            r = ps(script)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "move":
            x, y = params.get("x", 0), params.get("y", 0)
            script = f"""
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Mouse {{ [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y); }}
'@
[Mouse]::SetCursorPos({x}, {y})"""
            r = ps(script)
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "type":
            text = params.get("text", "")
            if not text:
                return {"error": "text is required"}
            escaped = _escape_sendkeys(text)
            script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("{escaped}")"""
            r = ps(script)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "key":
            keys = params.get("keys", "")
            if not keys:
                return {"error": "keys is required"}
            # Map to SendKeys format
            key_map = {
                "cmd": "^", "ctrl": "^", "alt": "%", "shift": "+",
                "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}",
                "escape": "{ESC}", "esc": "{ESC}", "delete": "{DELETE}",
                "backspace": "{BACKSPACE}", "space": " ",
                "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
                "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
            }
            parts = [k.strip().lower() for k in keys.split("+")]
            modifiers = ""
            key_part = ""
            for p in parts:
                if p in ("cmd", "ctrl", "alt", "shift", "command", "control", "option"):
                    modifiers += key_map.get(p, "")
                else:
                    key_part = key_map.get(p, p)
            sendkeys = f"{modifiers}{key_part}"
            script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("{sendkeys}")"""
            r = ps(script)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            wheel_delta = -120 * amount if direction == "down" else 120 * amount
            script = f"""
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Mouse {{
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, IntPtr dwExtraInfo);
}}
'@
[Mouse]::mouse_event(0x0800, 0, 0, {wheel_delta}, [IntPtr]::Zero)"""
            r = ps(script)
            self._delay()
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "cursor_position":
            r = ps("[System.Windows.Forms.Cursor]::Position | ConvertTo-Json")
            if r.returncode == 0:
                try:
                    pos = json.loads(r.stdout)
                    return {"success": True, "x": pos.get("X", 0), "y": pos.get("Y", 0)}
                except json.JSONDecodeError:
                    pass
            return {"error": "cursor_position failed"}

        if action == "screen_size":
            r = ps("[System.Windows.Forms.Screen]::PrimaryScreen.Bounds | ConvertTo-Json")
            if r.returncode == 0:
                try:
                    bounds = json.loads(r.stdout)
                    return {"success": True, "width": bounds.get("Width", 0), "height": bounds.get("Height", 0)}
                except json.JSONDecodeError:
                    pass
            return {"error": "screen_size failed"}

        if action == "clipboard_read":
            r = ps("Get-Clipboard")
            return {"success": True, "text": r.stdout.strip()} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}
        if action == "clipboard_write":
            text = params.get("text", "")
            r = ps(f'Set-Clipboard -Value "{text}"')
            return {"success": True} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        if action == "drag":
            return {"error": "drag not yet implemented on Windows"}
        if action == "window_list":
            r = ps("Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object ProcessName,MainWindowTitle | ConvertTo-Json")
            return {"success": True, "output": r.stdout.strip()[:2000]} if r.returncode == 0 else {"error": r.stderr.strip()[:300]}

        return {"error": f"Unknown action: {action}"}
