"""A skill download follows its own site's redirect, and only that one."""

import hashlib

import httpx

from flowly.hub.client import HubClient, SkillInfo

SKILL = b"---\nname: demo\ndescription: A demo.\n---\nHello.\n"


def _client(handler) -> HubClient:
    hub = HubClient("https://useflowlyapp.com")
    hub._client = httpx.Client(transport=httpx.MockTransport(handler))
    return hub


def _skill(url: str) -> SkillInfo:
    return SkillInfo.from_dict({"slug": "demo", "name": "demo", "download_url": url,
                                "hash": hashlib.sha256(SKILL).hexdigest()})


def test_the_www_address_the_registry_gives_redirects_to_its_own_site_and_installs(tmp_path):
    # The registry hands out www.useflowlyapp.com, which answers 308 to the
    # bare domain: every marketplace install failed (2026-11-22).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.useflowlyapp.com":
            return httpx.Response(308, headers={"location": f"https://useflowlyapp.com{request.url.path}"})
        return httpx.Response(200, content=SKILL, headers={"content-type": "text/markdown"})

    path = _client(handler).download_skill(
        _skill("https://www.useflowlyapp.com/api/skills/demo/download"), tmp_path)

    assert path is not None
    assert (path / "SKILL.md").read_bytes() == SKILL


def test_a_redirect_to_another_site_is_refused(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "useflowlyapp.com":
            return httpx.Response(302, headers={"location": "https://evil.example/skill.zip"})
        return httpx.Response(200, content=SKILL)

    path = _client(handler).download_skill(
        _skill("https://useflowlyapp.com/api/skills/demo/download"), tmp_path)

    assert path is None
