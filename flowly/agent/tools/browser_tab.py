"""Browser tab tool — interact with web pages via the Flowly Chrome extension.

The extension connects to the gateway via WebSocket. This tool sends
action requests to the extension and waits for results. All actions
execute in the user's REAL browser — they see everything in real-time.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.agent.tools.base import Tool

def _screenshots_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "screenshots"


# Recovery hints for known extension error codes. Surfacing the next-step
# inline cuts agent recovery from "fail → think → maybe-retry → fail"
# (multi-turn) to "fail-with-hint → correct-call" (single turn).
_ERROR_HINTS: dict[str, str] = {
    "PERMISSION_DENIED":
        "User has disabled this action's category for the tab. Tell the "
        "user which permission toggle to flip in the side panel — do not "
        "retry blindly.",
    "URL_DRIFT":
        "The tab navigated since the last read_page; element refs are "
        "stale. Call read_page to refresh, then retry the action.",
    "EVALUATE_GUARDED_API":
        "The evaluate snippet hit the deny-list (network requests, "
        "service workers, eval, location.href reassignment). If the "
        "snippet is benign, retry with unsafe=true (audited).",
    "USE_UPLOAD_FILE_ACTION":
        "Don't click <input type=file> — the OS picker can't be filled. "
        "Call browser_tab(action='upload_file', selector=<css>, "
        "paths=['/abs/path']) instead.",
    "USE_FORM_INPUT_FOR_SELECT":
        "Native <select> dropdowns can't be opened by CDP click. Call "
        "browser_tab(action='form_input', ref=<this ref>, value=<option "
        "value or text>) to pick an option programmatically.",
    "SENSITIVE_NAVIGATION":
        "The tab navigated to a sensitive site (banking, gov-auth, "
        "healthcare, password manager) mid-action. Flowly refuses to "
        "act on these by default. Tell the user which site was reached "
        "and let them decide whether to enable it via the side panel — "
        "do NOT retry blindly.",
    "TYPE_NOT_PERSISTED":
        "The text was sent but did not appear in the field. Most likely "
        "you typed into the wrong ref (a label/wrapper next to the actual "
        "input). Read the page again, look for the textbox/contenteditable "
        "near the field you meant, and use that ref. Do NOT just retry "
        "the same ref — verify the target first.",
    "NOT_EDITABLE":
        "The ref points to a <button>/<a>/<select> — not editable. The "
        "agent picked the wrong ref. Read the page and find the input "
        "field nearby (usually the next textbox).",
    "NOT_TEXT_INPUT":
        "The ref is an <input> but not a text-accepting type (checkbox, "
        "radio, file, etc.). Use action='click' to toggle it, or pick a "
        "different ref for typing.",
    "ELEMENT_DISABLED":
        "The element is disabled — clicking will silently no-op. Disabled "
        "controls almost always need a PREREQUISITE: select a range first "
        "(many Sheets menu items), fill a required field, pick from a "
        "parent dropdown, accept a license, etc. Re-read the page (look "
        "for required/empty/invalid markers) or screenshot to find what's "
        "missing. Do NOT retry the same click — the answer is not 'try "
        "harder', it's 'do something else first'.",
}

# Substring → hint, applied when error_code isn't set but the error
# string contains a recognisable pattern. Cheap fallback while we
# normalise extension responses to always emit error_code.
_ERROR_HINT_PATTERNS: list[tuple[str, str]] = [
    ("stale", _ERROR_HINTS["URL_DRIFT"]),
    ("no longer resolves", _ERROR_HINTS["URL_DRIFT"]),
    ("USE_UPLOAD_FILE_ACTION", _ERROR_HINTS["USE_UPLOAD_FILE_ACTION"]),
    ("USE_FORM_INPUT_FOR_SELECT", _ERROR_HINTS["USE_FORM_INPUT_FOR_SELECT"]),
    ("EVALUATE_GUARDED_API", _ERROR_HINTS["EVALUATE_GUARDED_API"]),
]


class BrowserTabTool(Tool):
    """Interact with web pages in the user's real browser via Chrome extension."""

    # Inner-LLM model for semantic find. Cheap + fast; quality of
    # element matching far exceeds the extension's substring/regex
    # find with no impact on outer agent flow.
    SEMANTIC_FIND_MODEL = "anthropic/claude-haiku-4.5"

    def __init__(self, gateway_server: Any = None, provider: Any = None, registry: Any = None):
        self._gateway = gateway_server
        # Optional. When set, the `find` action transparently upgrades
        # to semantic matching via an inner Haiku call. When None we
        # fall through to the extension's substring matcher.
        self._provider = provider
        self._allowed_actions_cache: frozenset[str] | None = None
        # Registry — used to read _active_session_id for plan context
        # auto-injection. Optional; when None plan context is omitted.
        self._registry = registry

    def _build_plan_context(self) -> dict[str, Any] | None:
        """Return a tight summary of the current plan, or None if no
        plan exists. Injected into every browser_tab tool result so
        the agent always sees its position in the plan (Manus-style
        external memory)."""
        if self._registry is None:
            return None
        sess = getattr(self._registry, "_active_session_id", "") or ""
        if not sess:
            return None
        try:
            from flowly.agent.planner.state import get_plan_state
            plan = get_plan_state().get(sess)
        except Exception:
            return None
        if not plan or plan.status not in ("active",):
            return None
        cur = plan.current_step()
        progress = plan.progress_summary()
        ctx: dict[str, Any] = {
            "goal": plan.goal[:120],
            "progress": progress,
            "status": plan.status,
        }
        if cur:
            ctx["currentStep"] = {
                "id": cur.id,
                "content": cur.content[:120],
                "successCriteria": cur.successCriteria[:200],
                "status": cur.status,
                "retries": cur.retries,
            }
        # Surface blocked steps prominently — agent should address them
        blocked = [s for s in plan.steps if s.status == "blocked"]
        if blocked:
            ctx["blocked"] = [
                {"id": s.id, "content": s.content[:80], "reason": s.evidence or ""}
                for s in blocked[:3]
            ]
        return ctx

    @property
    def _allowed_actions(self) -> frozenset[str]:
        """Cached frozenset of every action declared in the schema.

        Single source of truth — the enum in `parameters` defines what
        the LLM is told it can call AND what the wrapper accepts. If
        someone adds a new action to the enum without updating this,
        both surfaces stay in sync automatically.
        """
        if self._allowed_actions_cache is None:
            try:
                enum_vals = self.parameters["properties"]["action"]["enum"]
                self._allowed_actions_cache = frozenset(enum_vals)
            except Exception:
                # Fail-open with empty set — defensive check still rejects
                # everything, but avoids crashing tool registration.
                self._allowed_actions_cache = frozenset()
        return self._allowed_actions_cache

    @property
    def name(self) -> str:
        return "browser_tab"

    @property
    def description(self) -> str:
        return (
            "Interact with web pages in the user's REAL browser (via Flowly Chrome extension). "
            "The user can see every action in real-time.\n\n"
            "Actions:\n"
            "- read_page: Get element map with ref IDs (text, NOT a screenshot). "
            "Returns interactive elements like buttons, links, inputs with unique refs.\n"
            "- click: Click by ref ID (ref='ref_3'), CSS selector (selector='[data-testid=\"tweetButton\"]'), "
            "or visible text (text='Post'). Selector and text are most reliable for known elements.\n"
            "- type: Type text into element (ref='ref_5', text='hello'). "
            "REPLACES existing content by default (so re-running on a field "
            "with default text correctly overwrites it). Pass clear=false to "
            "append instead (rare — chat boxes etc.).\n"
            "- form_input: Set form field value (ref='ref_7', value='test@email.com')\n"
            "- upload_file: Upload file(s) to <input type=\"file\"> via Chrome DevTools "
            "Protocol (selector='input[type=file]', paths=['/abs/path/file.mp4']). "
            "NEVER opens the OS native picker — files are injected directly. "
            "Use this for ALL file uploads.\n"
            "- hover: Hover the cursor over an element (ref or selector). "
            "Triggers dropdown menus, tooltips, hover-revealed buttons.\n"
            "- wait: Wait for a UI condition before proceeding. Modes: "
            "selector='#result' (until visible), network_idle=true (idle_ms default 500), "
            "or just timeout_ms=2000 (sleep). MUCH cheaper than spamming read_page in a loop.\n"
            "- evaluate: Run a small JS snippet in the page's MAIN world (same "
            "context as the site's own scripts). Use as escape hatch when "
            "read_page can't expose state (canvas, custom widgets, app stores). "
            "code='document.title'. Result is JSON-stringified, truncated at 4KB. "
            "GUARDED: snippets are limited to 2KB and refused if they reference "
            "cookie/storage APIs, fetch/XHR/WebSocket, eval/Function/document.write, "
            "Worker/ServiceWorker, or location.href reassignment. The user must "
            "also flip the `evaluate` permission ON for the tab — it defaults OFF. "
            "If the site really needs a guarded API and the call is benign, retry "
            "with unsafe=true (logged with extra weight in the audit trail). "
            "OUTPUT IS FOR YOUR REASONING — do NOT paste the raw result into "
            "your reply to the user; summarize instead.\n"
            "- console_log: Read recent console output from the page (console.log/warn/error "
            "+ uncaught exceptions). Capture starts on first call. limit=N (default 30), "
            "clear=true to flush. PURELY A DEBUG TOOL. The console can contain API tokens, "
            "signed URLs, third-party errors — it is SENSITIVE. Never paste raw console lines, "
            "stack traces, or error messages into your user-facing reply. If an error explains "
            "a failure, summarize in one plain sentence (e.g. \"the site's API returned an "
            "auth error\") and never quote the raw text.\n"
            "- dialog: ARM the next JS dialog (alert/confirm/prompt/beforeunload) on "
            "this tab to be auto-handled. Call BEFORE the action that opens the dialog. "
            "accept=true|false, promptText='...' (only for prompt). One-shot — re-arm if needed.\n"
            "- tabs_close: Close a tab from the Flowly group (tabId required, from tabs_list).\n"
            "- find: Search for elements by description (query='Post button', 'search input'). "
            "Use when you can't find an element with read_page.\n"
            "- navigate: Go to URL (url='https://youtube.com')\n"
            "- get_page_text: Get page text content\n"
            "- screenshot: Capture the tab as a JPEG and INCLUDE THE IMAGE IN YOUR "
            "CONTEXT (you'll see it directly via vision input — no text-relayed "
            "description). Your PRIMARY visual sense — "
            "use it liberally any time the page state is uncertain, especially: "
            "(a) before clicking ANY menu item / button you're not 100% sure about, "
            "(b) right after opening a menu / dropdown / modal / sidebar so you can "
            "see what items are actually there, "
            "(c) on any page in a UI language you don't speak natively (Turkish/German/etc.) — "
            "the visual confirms which menu is which regardless of localized labels, "
            "(d) on canvas-rendered apps (Sheets/Figma/Miro/Excel Online) where read_page "
            "returns almost nothing useful inside the canvas. read_page is for ref IDs; "
            "screenshot is for understanding what's on screen. They complement each other. "
            "Old advice 'use read_page only' was wrong and caused agents to loop forever "
            "on unfamiliar UIs — IGNORE that. Cost is ~5KB per shot, basically free. "
            "ZOOM (use this on dense UIs): pass ref='ref_X' to crop to that "
            "element + small padding. The cropped image is ~2x the effective "
            "resolution of the same element in a full-viewport screenshot, "
            "which is the difference between 'I can read this dropdown item' "
            "and 'all the menu text is blurry'. USE THIS WHEN: reading "
            "tooltips, dropdown menu items, error toasts, dense canvas cells, "
            "small icons, validation messages near a specific input, or "
            "anything that looks tiny in a full-viewport shot. Pass "
            "bbox={x,y,width,height} in CSS viewport pixels for an arbitrary "
            "region (e.g. when zooming into a sidebar). Default (no ref/bbox) "
            "= full viewport for general situational awareness.\n"
            "- scroll: Scroll page (direction=down/up, amount=3)\n"
            "- key: Press keyboard key or combo (key='Enter', key='Ctrl+a')\n"
            "- tabs_list: List tabs in the Flowly tab group\n"
            "- tabs_create: Open new tab (url='https://...')\n"
            "- tabs_context: Enriched multi-tab snapshot — per-tab perms, "
            "sensitive-domain classification, focused-tab marker, last "
            "read_page URL + age. Cheap; call at the start of a turn when "
            "you need to coordinate across multiple managed tabs.\n"
            "- batch: Run up to 50 atomic sub-actions sequentially in ONE "
            "tool call. Use for form filling, spreadsheet entry, or any "
            "type+Tab+type+Tab pattern — without this each cycle costs a "
            "full LLM round-trip and 20 fields take minutes. Pass "
            "actions=[{action:'key',params:{key:'Tab'}}, "
            "{action:'type',params:{text:'foo'}}, ...]. delay_ms (default "
            "80) gives the page time to react between steps; raise it for "
            "lagging sites. stop_on_error=true (default) aborts the rest "
            "after the first failed sub-action. wait/dialog/batch are "
            "FORBIDDEN inside a batch — use them as separate calls. "
            "Returns {success, executed, total, stoppedAt, durationMs, results:[...]}. "
            "DON'T batch when the next action depends on observing the "
            "previous result (e.g. read_page → decide which ref to click) "
            "— do those as individual calls. DO batch when the sequence is "
            "deterministic (typing a known value into known fields).\n"
            "- read_network_requests: Read recent HTTP requests captured on "
            "the tab (XHR/fetch/document/image/script/...). Filters: "
            "url_contains, method, type, status_min, status_max, since_ms, "
            "limit (default 30). Capture starts on first call. METADATA "
            "ONLY — no response bodies. SENSITIVE — same handling as "
            "console_log: never paste raw URLs/params verbatim, summarize.\n"
            "- upload_image: Inject a base64 image into a file input OR a "
            "drag-drop zone (selector required). Pass dataUrl='data:image/"
            "png;base64,...' (filename, mimeType optional). Use this for "
            "chat composers / editors that only accept drag-drop, not "
            "<input type=file>. For local-disk paths use upload_file.\n\n"
            "Default workflow (simple known pages): "
            "tabs_list → navigate → read_page → click by ref → type → read_page (verify).\n"
            "\n"
            "Workflow for UNFAMILIAR / LOCALIZED / CANVAS UIs (Sheets, Notion, "
            "Figma, anything in a non-English UI, anything you've never operated "
            "before): tabs_list → navigate → screenshot → read_page → "
            "(decide which ref to click using BOTH the visual + ref labels) → "
            "click → screenshot to verify the right menu/modal opened → read_page "
            "if you need new refs from inside the popover. SCREENSHOT IS YOUR "
            "GROUND TRUTH on these. read_page alone gives you ref labels in the "
            "page's UI language; the screenshot tells you which one is actually "
            "the menu you want. Without it you WILL click the wrong menu item.\n"
            "\n"
            "After clicking a menu/dropdown/modal-opening button: wait(timeout_ms=400) "
            "then screenshot. The popover animates over ~300ms; reading too early "
            "catches the pre-open DOM and the screenshot too early shows the page "
            "without the menu open. Wait first, then look.\n\n"
            "VERIFY EVERY CLICK: After any click that should change state (open dialog, "
            "submit form, navigate), call read_page and confirm the expected change "
            "actually happened. Do NOT claim success based on click result alone — "
            "the extension reports DOM-level success, not the user-visible side effect.\n\n"
            "FILE UPLOADS (<input type=file>): ALWAYS use action='upload_file' with a "
            "CSS selector and absolute file paths. Do NOT use click on file inputs — "
            "it opens the OS native picker that can't be filled. If you accidentally "
            "click one and get error='USE_UPLOAD_FILE_ACTION', re-issue the operation "
            "as upload_file(selector=..., paths=['/abs/path']). After upload_file "
            "returns success, wait 1-2s and call read_page to verify the file appears "
            "in the upload list / progress UI before claiming success.\n\n"
            "MODALS AND OVERLAYS: When a click opens a modal/dialog/overlay, the next "
            "read_page focuses on it and includes a `context` field like 'A modal is "
            "open (\"<name>\") — elements below are inside it'. Read this context! "
            "If you see it, your next actions apply to the MODAL's elements, not the "
            "background page. To close a modal without submitting: key(key='Escape'), "
            "or click its close button (often aria-label='Close' or '×'). If you expect "
            "a modal but read_page shows the background page instead, try wait(selector="
            "'<modal selector>', timeout_ms=3000) first — the modal may still be "
            "animating in.\n\n"
            "EXPANDABLE / COLLAPSIBLE SECTIONS: When you click a button that expands "
            "a section (rules panel, FAQ, accordion), its `aria-expanded` flips to "
            "'true' and the hidden content becomes readable. Call read_page AFTER "
            "the expand animation finishes (use wait if needed) to see the expanded "
            "content. The tree now includes expanded regions even when they have "
            "structural roles like 'group' or 'region'.\n\n"
            "CANVAS-RENDERED APPS (Google Sheets, Excel Online, Figma, Linear "
            "graph view, Miro, etc.): the main work area is drawn into a "
            "<canvas> element so read_page sees almost nothing inside it — "
            "individual cells/shapes have no DOM refs you can click. DRIVE "
            "THESE APPS WITH THE KEYBOARD via the `key` action, not click.\n"
            "  Google Sheets — basic editing:\n"
            "    key='Ctrl+Home' → jump to A1.\n"
            "    Arrow keys → move between cells.\n"
            "    key='F2' (or just type) → start editing the focused cell.\n"
            "    type(text='hello') → fill it.\n"
            "    key='Enter' → commit + move down. key='Tab' → commit + move right.\n"
            "    key='Ctrl+G' opens Go-To, type='B5' + key='Enter' jumps to B5.\n"
            "  Sheets — sort a range:\n"
            "    Click the column letter at top to select the column → right-click → "
            "menu has 'Sort sheet by column X, A→Z' (label is in user's locale: "
            "Turkish 'Sayfayı sütuna göre sırala', German 'Tabelle nach Spalte "
            "sortieren', etc.). Read the dropdown items in the user's UI language, "
            "don't search for English text.\n"
            "    Or: select range, then click Data menu (Veri/Daten/Datos/Données), "
            "find 'Sort range' (Aralığı sırala/Bereich sortieren).\n"
            "  Sheets — KNOW WHEN TO BAIL OUT to Apps Script:\n"
            "    Conditional formatting (multi-rule), pivot tables, complex chart "
            "config, named ranges, data validation — these need 20-40 sequential "
            "UI steps with sidebar state changes between each. Doing them through "
            "menu navigation is unreliable; the sidebar's color/format pickers are "
            "often custom canvas widgets without DOM hooks. Don't try to brute-force "
            "it. Instead use the Apps Script escape hatch (next section).\n"
            "  Apps Script escape hatch for complex Sheets / Docs / Forms work:\n"
            "    See the system-prompt browser_tab guidance section for the "
            "full hierarchy and the Extensions-menu warning. Quick recipe:\n"
            "    1. Click the EXTENSIONS menu (Uzantılar / Erweiterungen / "
            "Extensions) — NOT the Tools menu (Araçlar / Werkzeuge / Outils). "
            "Google moved Apps Script to Extensions in 2022; clicking Tools "
            "leads you to translation/named-ranges/etc. and you'll loop. "
            "Then click Apps Script (sometimes labelled 'Apps Script editor').\n"
            "    2. A new tab opens with a Monaco-like code editor (DOM-friendly).\n"
            "    3. read_page → find the code area (textarea or contenteditable).\n"
            "    4. type() the script — normal JavaScript with SpreadsheetApp / "
            "DocumentApp / FormApp APIs.\n"
            "    5. key='Ctrl+S' to save, then click the Run button (key='Ctrl+R' "
            "is intercepted by Chrome). First run prompts for permissions — the "
            "user has to click Allow once.\n"
            "    6. Switch back to the spreadsheet tab and read_page to verify.\n"
            "    Apps Script is a LAST resort — for ad-hoc coloring use the "
            "toolbar Fill color button instead (~6 actions vs Apps Script's 30+). "
            "See the system-prompt three-tier hierarchy."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The browser action to perform",
                    "enum": [
                        "read_page", "click", "type", "form_input", "upload_file",
                        "upload_image",
                        "hover", "find", "wait", "evaluate", "console_log", "dialog",
                        "navigate", "get_page_text", "screenshot",
                        "scroll", "key", "tabs_list", "tabs_create", "tabs_close",
                        "tabs_context", "read_network_requests",
                        "batch",
                    ],
                },
                "ref": {
                    "type": "string",
                    "description": "Element ref ID from read_page output (e.g. 'ref_3')",
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "Plain CSS selector (NOT Playwright/jQuery extensions). "
                        "Valid: '[data-testid=\"tweetButton\"]', 'button.submit', "
                        "'#header > nav a'. INVALID and will fail: ':has-text(...)', "
                        "':contains(...)', ':visible', ':nth-text(...)' — these are "
                        "Playwright/jQuery only, NOT browser CSS. For text matching use "
                        "text= parameter instead. PREFER ref= from read_page over "
                        "guessed selectors — guessing is slow and fails. Use selector "
                        "only when YOU verified it from read_page or DOM inspection."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "For type: text to type. "
                        "For click: visible text MUST match the page's actual text EXACTLY "
                        "(case-insensitive, but no translation). On a Turkish UI 'Format' "
                        "will NOT find the 'Biçim' menu — pass text='Biçim' or use ref= "
                        "from read_page. NEVER pass an English translation when the UI "
                        "is in another language; the agent must read the actual on-screen "
                        "text from a screenshot or read_page result first. When in doubt, "
                        "prefer ref= or selector=; text= is a last resort."
                    ),
                },
                "value": {
                    "type": "string",
                    "description": "Form field value to set",
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to or open in new tab",
                },
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction",
                },
                "amount": {
                    "type": "integer",
                    "description": "Scroll amount (default: 3)",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Press Enter after typing (default: false)",
                },
                "clear": {
                    "type": "boolean",
                    "description": "Clear the field before typing. Default: true (replaces existing content). Set to false to append (rare — e.g. continuing a draft in a chat composer).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for find action (e.g. 'Post button', 'search input')",
                },
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g. 'Enter', 'Tab', 'Ctrl+a')",
                },
                "interactive": {
                    "type": "boolean",
                    "description": "Filter read_page to interactive elements only (default: true)",
                },
                "bbox": {
                    "type": "object",
                    "description": "Screenshot region in CSS viewport pixels: {x, y, width, height}. Use with action='screenshot' for an arbitrary rectangle (prefer ref= when targeting one element).",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "tabId": {
                    "type": "integer",
                    "description": "Target tab ID (from tabs_list). Uses active group tab if omitted.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute file paths for upload_file action (e.g. ['/Users/name/video.mp4']). All paths must start with '/'.",
                },
                "code": {
                    "type": "string",
                    "description": "JS code to run in page MAIN world (evaluate action). Last expression value is returned. Capped at 2KB.",
                },
                "unsafe": {
                    "type": "boolean",
                    "description": "evaluate: opt-in to run a snippet that touches the guarded APIs (cookie/storage/fetch/eval/etc.). Default false. Logged separately in the audit trail.",
                },
                "accept": {
                    "type": "boolean",
                    "description": "For dialog action: true to confirm, false to dismiss the next JS dialog.",
                },
                "promptText": {
                    "type": "string",
                    "description": "For dialog action with accept=true on a window.prompt() — text to fill into the prompt field.",
                },
                "limit": {
                    "type": "integer",
                    "description": "console_log: max lines to return (default 30, capped at 200).",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "wait: max time to wait in ms (default 5000, capped at 60000).",
                },
                "network_idle": {
                    "type": "boolean",
                    "description": "wait: wait until no network activity for idle_ms (default 500ms idle window).",
                },
                "idle_ms": {
                    "type": "integer",
                    "description": "wait + network_idle: required quiet window in ms (default 500).",
                },
                "dataUrl": {
                    "type": "string",
                    "description": "upload_image: base64 data URL of the image (e.g. 'data:image/png;base64,iVBORw...').",
                },
                "filename": {
                    "type": "string",
                    "description": "upload_image: filename to attach to the dropped File (default 'image.png').",
                },
                "mimeType": {
                    "type": "string",
                    "description": "upload_image: MIME type override (default 'image/png'; auto-detected from dataUrl header when present).",
                },
                "url_contains": {
                    "type": "string",
                    "description": "read_network_requests: substring filter on URL (case-insensitive).",
                },
                "method": {
                    "type": "string",
                    "description": "read_network_requests: HTTP method filter (e.g. 'GET', 'POST').",
                },
                "type": {
                    "type": "string",
                    "description": "read_network_requests: resource type filter ('xhr', 'fetch', 'document', 'image', 'script', 'stylesheet', ...).",
                },
                "status_min": {
                    "type": "integer",
                    "description": "read_network_requests: minimum HTTP status code (inclusive).",
                },
                "status_max": {
                    "type": "integer",
                    "description": "read_network_requests: maximum HTTP status code (inclusive).",
                },
                "since_ms": {
                    "type": "integer",
                    "description": "read_network_requests: only requests started within the last N ms.",
                },
                "actions": {
                    "type": "array",
                    "description": "batch: list of {action, params} entries to execute sequentially in one extension round-trip. Max 50.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["action"],
                    },
                },
                "delay_ms": {
                    "type": "integer",
                    "description": "batch: pause in ms between sub-actions (default 80, max 2000). Lower for fast-typing forms, higher for sites that lag.",
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "batch: abort the rest of the batch on the first error (default true). Set false to push through.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if not self._gateway:
            return json.dumps({"error": "Gateway not available"})

        # Check if extension is connected
        if not self._gateway.has_extension_client():
            return json.dumps({
                "error": "Flowly Chrome extension not connected. "
                "Install the extension and open the side panel to connect."
            })

        # Defensive action allowlist — never forward arbitrary action
        # strings to the extension, even if the schema enum is bypassed
        # (LLM hallucination, mis-serialised tool call, downstream
        # provider quirks). Cheap O(1) check, eliminates a class of
        # bugs where typos like "click_button" fail with a vague
        # extension error after a network round-trip.
        allowed_actions = self._allowed_actions
        if action not in allowed_actions:
            return json.dumps({
                "error": f"Unknown browser_tab action: {action!r}. "
                f"Valid actions: {sorted(allowed_actions)}"
            })

        request_id = str(uuid.uuid4())
        params = {k: v for k, v in kwargs.items() if v is not None}

        # SSRF protection for navigate and tabs_create actions
        if action in ("navigate", "tabs_create") and params.get("url"):
            from flowly.agent.tools.web import _validate_url
            allowed, reason = _validate_url(params["url"])
            if not allowed:
                return json.dumps({"error": reason, "url": params["url"]})

        # Semantic find — intercept before forwarding so the agent's
        # plain `find` call upgrades to LLM-powered matching when a
        # provider is wired. No agent-prompt change needed; the tool
        # description is unchanged.
        if action == "find" and self._provider is not None and params.get("query"):
            try:
                return await self._semantic_find(params)
            except Exception as e:
                logger.warning("semantic find fell through to extension: {}", e)
                # Fall through to the extension's substring matcher.

        # Action-specific timeouts (Fix #8: generous timeouts prevent false timeout-after-action)
        action_timeouts = {
            "read_page": 20.0, "find": 20.0, "screenshot": 15.0,
            "navigate": 20.0, "get_page_text": 15.0,
            "click": 10.0, "type": 10.0, "form_input": 10.0,
            "hover": 8.0,
            # upload_file goes through CDP attach + DOM.querySelector +
            # DOM.setFileInputFiles. The CDP round-trip itself is fast, but
            # large multi-file uploads benefit from headroom.
            "upload_file": 20.0,
            # wait dynamically extends its own timeout (timeout_ms + 2s buffer
            # is added in the extension), so this is just a hard ceiling.
            "wait": 65.0,
            "evaluate": 15.0, "console_log": 10.0, "dialog": 10.0,
            "scroll": 8.0, "key": 8.0,
            "tabs_list": 8.0, "tabs_create": 10.0, "tabs_close": 8.0,
            # tabs_context queries Chrome storage + tabs API per managed tab,
            # cheap but scales with tab count. 8s is generous.
            "tabs_context": 8.0,
            # read_network_requests is a memory-only ring-buffer read in
            # the extension, fast even with 200 entries.
            "read_network_requests": 10.0,
            # upload_image: MAIN-world script execution + DataTransfer
            # construction + DragEvent dispatch. Larger images take longer
            # to base64-decode in the page, so headroom helps.
            "upload_image": 20.0,
            # batch: max 50 sub-actions × ~2s avg + 80ms gaps = ~110s
            # worst-case ceiling. Most batches finish in 2-10s; the
            # extension's per-sub-action timeouts cap individual hangs.
            "batch": 120.0,
        }
        timeout = action_timeouts.get(action, 15.0)

        try:
            result = await asyncio.wait_for(
                self._gateway.send_extension_tool_request(request_id, action, params),
                timeout=timeout,
            )

            # Transparent semantic fallback for click(text=X) on
            # localized UIs. Pattern from production logs: agent calls
            # click(text="Conditional formatting") on a Turkish Sheets
            # ("Koşullu biçimlendirme") — extension's exact-text matcher
            # returns "No element with text X found" — agent retries the
            # same English text and loops. Fix: when text-based click
            # fails AND we have a semantic provider, run the SAME query
            # through Haiku-powered _semantic_find. If a high-confidence
            # match exists, retry the click with that ref. The agent
            # never sees the failure — the click just works.
            if (
                action == "click"
                and isinstance(result, dict)
                and result.get("error")
                and "No element with text" in str(result.get("error", ""))
                and params.get("text")
                and self._provider is not None
            ):
                fallback = await self._semantic_click_fallback(
                    text=str(params["text"]),
                    tab_id=params.get("tabId"),
                )
                if fallback is not None:
                    result = fallback

            # Screenshot: keep base64 inline so the agent's vision can
            # actually see it. Loop detects the `_render_as_image` flag
            # and builds an image content block for the LLM. We also
            # save to file (for user delivery via media_paths), but the
            # critical bit is the inline image — without it the agent
            # is BLIND on canvas-rendered apps (Sheets/Figma/Miro etc.)
            # and that's the #1 reason it loops forever on those.
            if action == "screenshot" and isinstance(result, dict) and result.get("screenshot"):
                data_url = result["screenshot"]
                file_payload = self._save_screenshot_to_file({"screenshot": data_url})
                result = {
                    "success": True,
                    "_render_as_image": True,
                    "image_data_url": data_url,
                    "path": file_payload.get("path"),
                    "sizeKB": file_payload.get("sizeKB"),
                    "note": (
                        "Screenshot taken. The image follows in your context — look at it directly. "
                        "Use the visual to identify menu items, button positions, cell contents, etc. "
                        "Don't ask read_page to confirm what you can see in the image."
                    ),
                }

            # Add recovery hints for known error codes — saves the agent
            # a turn of trial-and-error figuring out what to do next.
            if isinstance(result, dict):
                err_text = str(result.get("error", ""))
                hint = _ERROR_HINTS.get(result.get("error_code") or "")
                if not hint:
                    # Best-effort: match common patterns in the error string
                    # itself. Extension errors haven't been fully normalized
                    # to error_code yet, so this catches the common cases.
                    for needle, recovery in _ERROR_HINT_PATTERNS:
                        if needle in err_text:
                            hint = recovery
                            break
                if hint and "hint" not in result:
                    result["hint"] = hint

            # Auto-inject plan context (Manus pattern). When a plan
            # exists for the current session, append its current state
            # to every browser_tab result tail so the agent has fresh
            # external memory each turn — solves "lost in the middle"
            # after 50+ tool calls. Cheap (~150 chars/result), safe
            # (only when plan exists).
            if isinstance(result, dict):
                try:
                    pc = self._build_plan_context()
                    if pc:
                        result["_planContext"] = pc
                except Exception:
                    # Plan context is best-effort — never fail the
                    # underlying browser_tab call because of it.
                    pass

            return json.dumps(result) if isinstance(result, dict) else str(result)
        except asyncio.TimeoutError:
            return json.dumps({
                "error": f"Extension did not respond in {timeout}s ({action}). "
                "The action may have succeeded in the browser — verify with read_page before retrying.",
            })
        except Exception as e:
            logger.error("browser_tab {} error: {}", action, e)
            return json.dumps({"error": str(e)})

    async def _semantic_click_fallback(
        self, text: str, tab_id: int | None = None
    ) -> dict[str, Any] | None:
        """When click(text=X) fails, try semantic find + click on the top match.

        Returns None if the fallback couldn't run or didn't find a
        confident match — caller surfaces the original error. Returns
        a dict shaped like a normal click result on success.

        Cap: only retries ONCE per call. No recursion, no infinite
        loops. Adds metadata so the agent (and operator logs) can see
        the fallback fired.
        """
        find_params: dict[str, Any] = {"query": text}
        if tab_id is not None:
            find_params["tabId"] = tab_id

        try:
            find_raw = await self._semantic_find(find_params)
        except Exception as e:
            logger.warning("semantic click fallback: find failed: {}", e)
            return None

        try:
            find_result = json.loads(find_raw)
        except Exception:
            return None

        matches = find_result.get("matches") if isinstance(find_result, dict) else None
        if not matches:
            return None

        # Pick the first (highest-confidence) match. Could weigh by
        # `reason` length / keywords later — for now first is fine.
        top = matches[0]
        ref = top.get("ref") if isinstance(top, dict) else None
        if not isinstance(ref, str) or not ref.startswith("ref_"):
            return None

        # Issue the actual click against the resolved ref. Reuse the
        # gateway path directly so we don't re-enter execute() (which
        # would re-trigger our schema/sensitive checks unnecessarily —
        # we already ran them for the original call).
        click_params: dict[str, Any] = {"ref": ref}
        if tab_id is not None:
            click_params["tabId"] = tab_id
        click_request_id = str(uuid.uuid4())
        try:
            click_result = await asyncio.wait_for(
                self._gateway.send_extension_tool_request(
                    click_request_id, "click", click_params
                ),
                timeout=10.0,
            )
        except Exception as e:
            logger.warning("semantic click fallback: click failed: {}", e)
            return None

        if not isinstance(click_result, dict) or not click_result.get("success"):
            return None

        click_result["via"] = (click_result.get("via") or "") + "+semantic_text"
        click_result["semantic_match"] = {
            "query": text,
            "matched_ref": ref,
            "reason": top.get("reason", "") if isinstance(top, dict) else "",
            "alternates": len(matches) - 1,
        }
        click_result["note"] = (
            f"text={text!r} didn't match any element exactly (likely a localized UI). "
            f"Used semantic match to {ref}. If this clicked the wrong thing, call read_page "
            f"and pick the ref explicitly."
        )
        return click_result

    async def _semantic_find(self, params: dict[str, Any]) -> str:
        """LLM-powered element matching for the `find` action.

        The extension's built-in find does substring/regex matching on
        the accessibility tree's `name` field. That fails the moment a
        site uses non-obvious labels — "Yayınla" instead of "Post",
        icon-only buttons whose name is a data-testid, etc.

        Pipeline:
          1. Call extension's read_page to get the live a11y tree (with
             ref IDs the agent will use to act).
          2. Hand the tree + the agent's natural-language query to a
             small, fast model (Haiku 4.5). Ask it to return ref IDs
             that semantically match.
          3. Parse the model's response and return them as `matches`,
             same JSON shape the extension's find produces so the agent
             doesn't notice the upgrade.

        Falls back to the extension matcher on any error (caller's
        try/except handles that — this method just raises).
        """
        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError("semantic find called with empty query")

        # Step 1: pull the a11y tree. Same call the agent could have
        # made; we just front-run it so it's already in our hand.
        tree_request_id = str(uuid.uuid4())
        tree_params: dict[str, Any] = {}
        if params.get("tabId") is not None:
            tree_params["tabId"] = params["tabId"]
        tree_result = await asyncio.wait_for(
            self._gateway.send_extension_tool_request(
                tree_request_id, "read_page", tree_params
            ),
            timeout=20.0,
        )
        if not isinstance(tree_result, dict) or "elements" not in tree_result:
            raise RuntimeError(
                f"read_page returned no elements: {tree_result!r}"
            )
        elements_text = tree_result.get("elements", "")
        page_url = tree_result.get("url", "")
        page_title = tree_result.get("title", "")

        # Step 2: ask Haiku. Tight, deterministic prompt — single JSON
        # object output, no chatter. Limit token budget so this never
        # runs away cost-wise.
        system = (
            "You are a precise element matcher for a browser-automation agent. "
            "You receive a list of page elements (each prefixed with a `ref_N` ID) "
            "and a natural-language query describing the element the agent needs. "
            "Return the best-matching ref IDs as JSON.\n\n"
            "OUTPUT RULES:\n"
            "- Return EXACTLY one JSON object: {\"matches\": [{\"ref\": \"ref_N\", \"reason\": \"short why\"}]}\n"
            "- At most 5 matches. Prefer fewer, higher-confidence ones.\n"
            "- If nothing matches, return {\"matches\": []}.\n"
            "- Do NOT add prose before or after the JSON."
        )
        user = (
            f"Page: {page_title} ({page_url})\n\n"
            f"Query: {query}\n\n"
            f"Elements:\n{elements_text}"
        )
        try:
            response = await asyncio.wait_for(
                self._provider.chat(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    model=self.SEMANTIC_FIND_MODEL,
                    max_tokens=600,
                    temperature=0.0,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("semantic find LLM timed out after 15s")

        raw = (response.content or "").strip()
        # Some providers wrap JSON in code fences. Strip them.
        if raw.startswith("```"):
            raw = raw.strip("`")
            # Drop a leading "json" hint line
            if raw.lstrip().lower().startswith("json"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Last-ditch: try to find a JSON object inside the response.
            start = raw.find("{")
            end = raw.rfind("}")
            if 0 <= start < end:
                parsed = json.loads(raw[start : end + 1])
            else:
                raise RuntimeError(
                    f"semantic find: model returned non-JSON: {raw[:200]!r}"
                )

        matches = parsed.get("matches", []) if isinstance(parsed, dict) else []
        # Defensive: ensure each match has at least a ref string.
        cleaned: list[dict[str, Any]] = []
        for m in matches:
            if isinstance(m, dict) and isinstance(m.get("ref"), str):
                cleaned.append(
                    {
                        "ref": m["ref"],
                        "reason": str(m.get("reason", ""))[:200],
                    }
                )

        return json.dumps(
            {
                "success": True,
                "query": query,
                "matches": cleaned,
                "count": len(cleaned),
                "via": "semantic",
                "url": page_url,
            }
        )

    @staticmethod
    def _save_screenshot_to_file(result: dict) -> dict:
        """Save base64 screenshot to file, return path instead of data.

        This follows the same pattern as the screenshot tool so the agent
        can use message(media_paths=[path]) to deliver the image to the
        user via relay → S3 → iOS/Desktop.
        """
        data_url = result.get("screenshot", "")
        if not data_url or not data_url.startswith("data:image/"):
            return result

        try:
            _screenshots_dir().mkdir(parents=True, exist_ok=True)

            # Parse data URL: "data:image/jpeg;base64,/9j/..."
            header, b64data = data_url.split(",", 1)
            ext = "jpg" if "jpeg" in header else "png"

            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            suffix = uuid.uuid4().hex[:6]
            filename = f"browser-{timestamp}-{suffix}.{ext}"
            output_path = _screenshots_dir() / filename

            output_path.write_bytes(base64.b64decode(b64data))
            size_kb = output_path.stat().st_size / 1024

            logger.info(f"Browser screenshot saved: {output_path} ({size_kb:.1f}KB)")

            return {
                "success": True,
                "path": str(output_path),
                "sizeKB": round(size_kb),
                "message": (
                    f"Screenshot saved to {output_path}\n"
                    f"To send to user: message(content=\"Here is the screenshot\", "
                    f"media_paths=[\"{output_path}\"])"
                ),
            }
        except Exception as e:
            logger.error(f"Failed to save browser screenshot: {e}")
            return {"error": f"Screenshot save failed: {e}"}
