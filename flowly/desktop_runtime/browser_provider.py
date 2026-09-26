"""Agent browser adapter for the profile's existing headed Chromium session.

Playwright is optional and imported only after screen action admission. This
adapter never launches another browser, copies cookies or accepts a CDP URL
from a tool call. Its connection is separate from the browser's lifetime.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from typing import Any
from urllib.parse import urlsplit

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.execution import ActionOutcomeUnknownError, ScreenExecution
from flowly.desktop_runtime.launcher import ScreenRuntime

PROVIDER_ID = "host-desktop-browser"
CAPABILITIES = frozenset({
    "tabs_list", "tabs_context", "tabs_create", "tabs_close", "navigate",
    "read_page", "get_page_text", "click", "hover", "type", "form_input",
    "key", "scroll", "screenshot", "wait",
})
_ELEMENTS = """() => Array.from(document.querySelectorAll(
    'a[href],button,input:not([type=hidden]),textarea,select,[role],[contenteditable=true]'
)).filter(e => { const r=e.getBoundingClientRect(); return r.width>0 && r.height>0;
}).slice(0,250)"""
_LABEL = """e => {
    const tag=e.tagName.toLowerCase();
    const role=e.getAttribute('role') || ({button:'button', a:'link',
        input:'textbox', textarea:'textbox', select:'combobox'}[tag] || tag);
    // Do not read input values, cookies, storage, or hidden DOM text.
    const label=e.getAttribute('aria-label') ||
        (e.labels && Array.from(e.labels).map(l=>l.innerText).join(' ')) ||
        e.getAttribute('placeholder') || e.innerText || '';
    return {role:role.slice(0,64), name:label.slice(0,160), disabled:!!e.disabled};
}"""


def _text(value: Any, *, maximum: int = 32768) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise ScreenError("INVALID_PARAMS")
    return value


def _url(value: Any) -> str:
    value = _text(value, maximum=8192)
    if value == "about:blank":
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ScreenError("INVALID_PARAMS")
    return value


class HostBrowserProvider:
    MAX_TABS = 16

    def __init__(self, runtime: ScreenRuntime, execution: ScreenExecution | None = None):
        self.runtime = runtime
        self.execution = execution or ScreenExecution(runtime.authority)
        self._driver = None
        self._browser = None
        self._generation: str | None = None
        self._epoch: int | None = None
        self._pages: dict[int, Any] = {}
        self._next_tab = 1
        self._active_tab: int | None = None
        self._refs: dict[str, tuple[Any, Any]] = {}
        self._next_ref = secrets.randbelow(2**48)

    async def _clear_refs(self) -> None:
        references, self._refs = self._refs, {}
        for _, handle in references.values():
            try:
                await handle.dispose()
            except Exception:
                pass  # a navigated/destroyed document already disposed it

    async def disconnect(self) -> None:
        """Only call after outstanding operations have settled."""
        await self._clear_refs()
        if self._driver is not None:
            await self._driver.stop()
        self._driver = self._browser = None
        self._pages.clear()
        self._active_tab = None
        self._generation = self._epoch = None

    async def _connect(self, generation: str) -> None:
        if self._generation == generation and self._browser and self._browser.is_connected():
            return
        await self.disconnect()
        state = await asyncio.to_thread(self.runtime.status)
        if not state or state["phase"] != "running" or state["generation"] != generation:
            raise ScreenError("BROWSER_UNAVAILABLE")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ScreenError("DEPENDENCIES_MISSING") from exc
        self._driver = await async_playwright().start()
        try:
            self._browser = await self._driver.chromium.connect_over_cdp(
                f"http://127.0.0.1:{state['cdpPort']}", timeout=5000,
            )
            self._generation = generation
        except Exception:
            await self.disconnect()
            raise ScreenError("BROWSER_UNAVAILABLE") from None

    def _sync_pages(self) -> dict[int, Any]:
        pages = self._browser.contexts[0].pages
        self._pages = {key: page for key, page in self._pages.items() if page in pages and not page.is_closed()}
        for page in pages:
            if page not in self._pages.values():
                self._pages[self._next_tab] = page
                self._next_tab += 1
        return self._pages

    async def _page(self, params: dict) -> tuple[int, Any]:
        pages = self._sync_pages()
        requested = params.get("tabId")
        if requested is not None:
            if type(requested) is not int or requested not in pages:
                raise ScreenError("TAB_NOT_FOUND")
            self._active_tab = requested
        if self._active_tab not in pages:
            focused, visible = [], []
            for key, page in pages.items():
                flags = await page.evaluate("({focused:document.hasFocus(), visible:document.visibilityState==='visible'})")
                if flags["focused"]:
                    focused.append(key)
                if flags["visible"]:
                    visible.append(key)
            candidates = focused or visible or list(pages)
            if len(candidates) != 1:
                raise ScreenError("TAB_SELECTION_REQUIRED")
            self._active_tab = candidates[0]
        return self._active_tab, pages[self._active_tab]

    async def _target(self, page, params: dict, *, text_target: bool = False):
        ref = params.get("ref")
        if ref is not None:
            pair = self._refs.get(_text(ref, maximum=128))
            if pair is None or pair[0] is not page:
                raise ScreenError("STALE_REFERENCE")
            try:
                if not await pair[1].evaluate("e=>e.isConnected"):
                    raise ScreenError("STALE_REFERENCE")
            except Exception:
                raise ScreenError("STALE_REFERENCE") from None
            return pair[1]
        selector = params.get("selector")
        if selector is not None:
            target = page.locator("css=" + _text(selector, maximum=4096))
        elif text_target and params.get("text") is not None:
            target = page.get_by_text(_text(params["text"], maximum=1024), exact=True)
        else:
            raise ScreenError("TARGET_REQUIRED")
        count = await target.count()
        if count != 1:
            raise ScreenError("TARGET_NOT_FOUND" if count == 0 else "AMBIGUOUS_TARGET")
        return target

    async def request(self, run_id: str, request_id: str, generation: str, action: str, params: dict) -> dict:
        if not isinstance(action, str) or action not in CAPABILITIES:
            return {"error": "This screen browser does not support that action.", "error_code": "UNSUPPORTED_ACTION"}
        if not isinstance(params, dict):
            return {"error": "Invalid browser parameters.", "error_code": "INVALID_PARAMS"}

        async def operate():
            await self._connect(generation)
            state = await asyncio.to_thread(self.runtime.authority.snapshot)
            if self._epoch != state["epoch"]:
                await self._clear_refs()
                self._active_tab = None
                self._epoch = state["epoch"]
            try:
                result = await asyncio.wait_for(self._dispatch(action, params),
                                                timeout=65 if action == "wait" else 20)
            except asyncio.TimeoutError:
                # Cancelling the driver await cannot prove a queued CDP command
                # stopped. Bound the request but leave durable admission closed.
                raise ActionOutcomeUnknownError() from None
            except ScreenError:
                raise
            except Exception:
                if not self._browser or not self._browser.is_connected():
                    raise ActionOutcomeUnknownError() from None
                # Backend errors can contain selectors, typed credentials or
                # page URLs. They are deliberately not logged or returned.
                raise ScreenError("BROWSER_ACTION_FAILED") from None
            return {**result, "_screenObservation": {"generation": generation, "epoch": self._epoch}}

        try:
            return await self.execution.action(run_id, request_id, generation, operate)
        except ScreenError as exc:
            return {"error": "Screen action could not complete. Observe the current state before retrying.",
                    "error_code": exc.code}

    def observation_valid(self, result: dict) -> bool:
        observation = result.get("_screenObservation")
        if observation is None:
            return True  # errors contain no captured data
        state = self.runtime.authority.snapshot()
        return (state["running"] and state["control"] == "agent"
                and observation == {"generation": state["generation"], "epoch": state["epoch"]})

    async def _dispatch(self, action: str, params: dict) -> dict:
        if action in {"tabs_list", "tabs_context"}:
            tabs = [{"id": key, "tabId": key, "url": page.url, "title": (await page.title())[:512]}
                    for key, page in self._sync_pages().items()]
            return dict(success=True, tabs=tabs, activeTabId=self._active_tab, provider=PROVIDER_ID)
        if action == "tabs_create":
            url = _url(params.get("url", "about:blank"))
            if len(self._sync_pages()) >= self.MAX_TABS:
                raise ScreenError("TAB_LIMIT_REACHED")
            page = await self._browser.contexts[0].new_page()
            self._sync_pages()
            self._active_tab = next(key for key, value in self._pages.items() if value is page)
            await page.bring_to_front()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return dict(success=True, tabId=self._active_tab)
        if action == "tabs_close" and params.get("tabId") is None:
            raise ScreenError("INVALID_PARAMS")
        tab_id, page = await self._page(params)
        if action == "tabs_close":
            # Keep the last window alive: closing it would terminate Chromium.
            if len(self._pages) == 1:
                raise ScreenError("LAST_TAB_REQUIRED")
            await page.close(run_before_unload=False)
            await self._clear_refs()
        elif action == "navigate":
            url = _url(params.get("url"))
            await self._clear_refs()
            await page.bring_to_front()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        elif action == "read_page":
            await self._clear_refs()
            array = await page.evaluate_handle(_ELEMENTS)
            try:
                handles = await array.get_properties()
            finally:
                await array.dispose()
            lines = []
            for handle in handles.values():
                element = handle.as_element()
                if element is None:
                    await handle.dispose()
                    continue
                label = await element.evaluate(_LABEL)
                ref = f"ref_{self._next_ref}"
                self._next_ref += 1
                self._refs[ref] = (page, element)
                lines.append(f"{ref} {label['role']} {json.dumps(label['name'], ensure_ascii=False)}"
                             + (" [disabled]" if label["disabled"] else ""))
            return dict(success=True, tabId=tab_id, url=page.url, title=(await page.title())[:512],
                        elements="\n".join(lines), count=len(lines), truncated=len(lines) >= 250)
        elif action == "get_page_text":
            return dict(success=True, tabId=tab_id, url=page.url,
                        text=await page.evaluate("(document.body?.innerText || '').slice(0,30000)"))
        elif action in {"click", "hover", "type", "form_input"}:
            target = await self._target(page, params, text_target=action in {"click", "hover"})
            await page.bring_to_front()
            if action == "click":
                await target.click(timeout=5000)
            elif action == "hover":
                await target.hover(timeout=5000)
            else:
                value = _text(params.get("text") if action == "type" else params.get("value"))
                for key in ("clear", "submit"):
                    if key in params and type(params[key]) is not bool:
                        raise ScreenError("INVALID_PARAMS")
                if action == "form_input" and await target.evaluate("e=>e.tagName==='SELECT'"):
                    await target.select_option(value=value, timeout=5000)
                elif params.get("clear", True):
                    await target.fill(value, timeout=5000)
                else:
                    await target.type(value, timeout=5000)
                if params.get("submit"):
                    await target.press("Enter", timeout=5000)
        elif action == "key":
            key = _text(params.get("key"), maximum=128)
            aliases = {"ctrl": "Control", "cmd": "Control", "control": "Control", "alt": "Alt", "shift": "Shift"}
            key = "+".join(aliases.get(part.lower(), part) for part in key.split("+"))
            await page.bring_to_front()
            await page.keyboard.press(key)
        elif action == "scroll":
            direction, amount = params.get("direction", "down"), params.get("amount", 3)
            vectors = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
            if direction not in vectors or type(amount) is not int or not 1 <= amount <= 100:
                raise ScreenError("INVALID_PARAMS")
            x, y = vectors[direction]
            await page.bring_to_front()
            await page.mouse.wheel(x * amount * 100, y * amount * 100)
        elif action == "screenshot":
            if any(key in params for key in ("bbox", "ref", "selector")):
                raise ScreenError("UNSUPPORTED_ACTION")
            size = await page.evaluate("({width:innerWidth,height:innerHeight})")
            if not 0 < size["width"] <= 3840 or not 0 < size["height"] <= 2160:
                raise ScreenError("OBSERVATION_TOO_LARGE")
            image = await page.screenshot(type="jpeg", quality=75, timeout=10000)
            if len(image) > 8 * 1024 * 1024:
                raise ScreenError("OBSERVATION_TOO_LARGE")
            return dict(success=True, tabId=tab_id,
                        screenshot="data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"))
        elif action == "wait":
            timeout = params.get("timeout_ms", 5000)
            if type(timeout) is not int or not 1 <= timeout <= 60000:
                raise ScreenError("INVALID_PARAMS")
            if "ref" in params or "selector" in params:
                target = await self._target(page, params)
                if hasattr(target, "wait_for"):
                    await target.wait_for(state="visible", timeout=timeout)
                else:
                    await target.wait_for_element_state("visible", timeout=timeout)
            else:
                await page.wait_for_load_state("networkidle" if params.get("network_idle") else "domcontentloaded", timeout=timeout)
        return dict(success=True, tabId=tab_id)
