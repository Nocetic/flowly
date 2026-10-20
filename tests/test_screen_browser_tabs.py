"""Each conversation works in its own tab of the screen's one browser.

The browser stays (logins, cookies); conversations do not share or take over
each other's tabs, old tab numbers never name a new tab, a screenshot shows
what is on the screen, and blank tabs a run opened go when it ends. Observed
on the test VPS (2026-10-20): a new chat reused the previous chat's tab and a
stale "tabId 2" from an earlier chat captured a blank page.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority
from flowly.desktop_runtime.browser_provider import HostBrowserProvider, _tab_base
from flowly.desktop_runtime.execution import ScreenExecution


class _Page:
    viewport_size = {"width": 1360, "height": 700}

    def __init__(self, browser, url="about:blank"):
        self.browser, self.url, self._closed = browser, url, False

    async def title(self):
        return self.url

    async def goto(self, url, **kwargs):
        self.url = url

    async def bring_to_front(self):
        self.browser.front = self

    async def evaluate(self, script, *args):
        if "innerWidth" in script:
            return {"width": 1360, "height": 700}
        if "hasFocus" in script:
            front = self.browser.front is self
            return {"focused": front, "visible": front}
        return ""

    async def screenshot(self, **kwargs):
        # What the person sees: the capture is taken of the tab in front.
        if self.browser.front is not self:
            raise AssertionError("captured a tab the person does not see")
        return f"pixels of {self.url}".encode()

    async def close(self, **kwargs):
        self._closed = True
        self.browser.context.pages.remove(self)
        if self.browser.front is self:
            self.browser.front = self.browser.context.pages[-1] if self.browser.context.pages else None

    def is_closed(self):
        return self._closed


class _Context:
    def __init__(self, browser):
        self.browser, self.pages = browser, []

    async def new_page(self):
        page = _Page(self.browser)
        self.pages.append(page)
        return page


class _Browser:
    """A started browser: one window with its start tab (its New Tab page)."""

    def __init__(self, start_url="chrome://newtab/"):
        self.context = _Context(self)
        self.contexts = [self.context]
        self.front = None
        start = _Page(self, start_url)
        self.context.pages.append(start)
        self.front = start

    def is_connected(self):
        return True


@pytest.fixture
def screen(tmp_path):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation-1")
    provider = HostBrowserProvider(Mock(authority=authority), ScreenExecution(authority))
    browsers = {"generation-1": _Browser()}

    async def connect(generation):
        if provider._generation == generation:
            return
        await provider.disconnect()
        provider._driver = Mock(stop=lambda: _done())
        provider._browser = browsers.setdefault(generation, _Browser())
        provider._generation = generation
        provider._next_tab = _tab_base(generation)

    async def _done():
        return None

    provider._connect = connect
    return provider, browsers


async def call(provider, owner, action, generation="generation-1", run="run-a", **params):
    return await provider.request(run, f"{action}-{owner}", generation, action, params, owner=owner)


def done(provider, run="run-a", generation="generation-1"):
    """One task at a time holds a screen: the run ends before the next starts."""
    provider.runtime.authority.finish_run(run, generation)


async def test_each_conversation_works_in_its_own_tab(screen):
    provider, browsers = screen
    first = await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    done(provider)
    second = await call(provider, "desktop:chat-b", "navigate", url="https://x.com/", run="run-b")
    done(provider, "run-b")
    # The first conversation took the browser's untouched start tab; the
    # second did not take it over but opened its own.
    assert first["tabId"] != second["tabId"]
    assert [page.url for page in browsers["generation-1"].context.pages] == ["https://arxiv.org/", "https://x.com/"]
    again = await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/list/cs.AI/recent", run="run-c")
    done(provider, "run-c")
    assert again["tabId"] == first["tabId"]
    assert browsers["generation-1"].context.pages[0].url == "https://arxiv.org/list/cs.AI/recent"
    listed = await call(provider, "desktop:chat-b", "tabs_list", run="run-b")
    assert {tab["tabId"]: tab["yours"] for tab in listed["tabs"]} == {first["tabId"]: False, second["tabId"]: True}
    assert listed["activeTabId"] == second["tabId"]


async def test_a_conversation_without_a_tab_reads_what_is_in_front_but_does_not_act_on_it(screen):
    provider, browsers = screen
    await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    done(provider)
    result = await call(provider, "desktop:chat-b", "click", run="run-b", selector="#login")
    assert result["error_code"] == "TAB_SELECTION_REQUIRED"
    assert browsers["generation-1"].context.pages[0].url == "https://arxiv.org/"


async def test_naming_another_conversations_tab_works_without_taking_it(screen):
    provider, _ = screen
    first = await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    done(provider)
    await call(provider, "desktop:chat-b", "get_page_text", run="run-b", tabId=first["tabId"])
    listed = await call(provider, "desktop:chat-b", "tabs_list", run="run-b")
    assert listed["tabs"][0]["yours"] is False


async def test_a_screenshot_shows_the_tab_on_the_screen(screen):
    provider, browsers = screen
    mine = await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    done(provider)
    await call(provider, "desktop:chat-b", "navigate", url="https://x.com/", run="run-b")
    done(provider, "run-b")
    assert browsers["generation-1"].front.url == "https://x.com/"
    shot = await call(provider, "desktop:chat-a", "screenshot", run="run-c")
    assert shot.get("success") is True and shot["tabId"] == mine["tabId"]
    assert browsers["generation-1"].front.url == "https://arxiv.org/"


async def test_tab_numbers_from_before_a_restart_name_no_tab(screen):
    provider, _ = screen
    old = await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    done(provider)
    provider.runtime.authority.stop("generation-1")
    provider.runtime.authority.start("generation-2")
    fresh = await call(provider, "desktop:chat-a", "tabs_list", generation="generation-2")
    assert old["tabId"] not in {tab["tabId"] for tab in fresh["tabs"]}
    stale = await call(provider, "desktop:chat-a", "screenshot", generation="generation-2", tabId=old["tabId"])
    assert stale["error_code"] == "TAB_NOT_FOUND"
    assert _tab_base("generation-1") != _tab_base("generation-2")


async def test_blank_tabs_a_run_opened_close_when_it_ends(screen):
    provider, browsers = screen
    await call(provider, "desktop:chat-b", "navigate", url="https://news.ycombinator.com/", run="run-b")
    other = await call(provider, "desktop:chat-b", "tabs_create", run="run-b")
    done(provider, "run-b")
    await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    blank = await call(provider, "desktop:chat-a", "tabs_create")
    used = await call(provider, "desktop:chat-a", "tabs_create", url="https://x.com/")
    await provider.close_blank_tabs("run-a", "generation-1", "desktop:chat-a")
    urls = [page.url for page in browsers["generation-1"].context.pages]
    # Its blank tab went; its used tabs and another run's blank tab stayed.
    assert urls == ["https://news.ycombinator.com/", "about:blank", "https://arxiv.org/", "https://x.com/"]
    listed = await call(provider, "desktop:chat-a", "tabs_list")
    assert blank["tabId"] not in {tab["tabId"] for tab in listed["tabs"]}
    assert {used["tabId"], other["tabId"]} <= {tab["tabId"] for tab in listed["tabs"]}


async def test_cleanup_never_touches_a_page_while_the_person_has_control(screen):
    provider, browsers = screen
    await call(provider, "desktop:chat-a", "tabs_create")
    provider.runtime.authority.acquire("human", "generation-1", 1)
    await provider.close_blank_tabs("run-a", "generation-1", "desktop:chat-a")
    assert len(browsers["generation-1"].context.pages) == 2



@pytest.mark.parametrize("start_url", ["chrome://newtab/", "about:blank"])
async def test_the_first_conversation_takes_the_browsers_untouched_start_page(screen, start_url):
    """A screen's browser starts on its own New Tab page (2026-10-20: it used to
    start on about:blank, which looked broken). Either way the first
    conversation works in that tab instead of opening a second one."""
    provider, browsers = screen
    browsers["generation-1"] = _Browser(start_url)
    first = await call(provider, "desktop:chat-a", "navigate", url="https://x.com/")
    assert [page.url for page in browsers["generation-1"].context.pages] == ["https://x.com/"]
    assert first["tabId"]


async def test_a_new_tab_page_a_run_opened_and_left_closes_when_it_ends(screen):
    provider, browsers = screen
    await call(provider, "desktop:chat-a", "navigate", url="https://arxiv.org/")
    opened = await call(provider, "desktop:chat-a", "tabs_create")
    tab = next(page for page in browsers["generation-1"].context.pages if page.url == "about:blank")
    tab.url = "chrome://newtab/"  # the person pressed + in that window
    await provider.close_blank_tabs("run-a", "generation-1", "desktop:chat-a")
    assert [page.url for page in browsers["generation-1"].context.pages] == ["https://arxiv.org/"]
    assert opened["tabId"] not in {t["tabId"] for t in (await call(provider, "desktop:chat-a", "tabs_list"))["tabs"]}
