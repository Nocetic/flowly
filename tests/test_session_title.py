"""Tests for auto-generated session titles (flowly/session/title.py + manager)."""

import asyncio

import pytest

from flowly.session.title import generate_title


class _FakeResp:
    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason


class _FakeProvider:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self._content)


def _title(content, user="hi", asst="hello"):
    return asyncio.run(generate_title(_FakeProvider(content), "m", user, asst))


def test_plain_title():
    assert _title("Trip planning to Japan") == "Trip planning to Japan"


def test_strips_quotes_and_prefix():
    assert _title('"Trip planning"') == "Trip planning"
    assert _title("Title: Trip planning") == "Trip planning"


def test_keeps_first_nonempty_line():
    assert _title("\n  Budget review  \nignored second line") == "Budget review"


def test_caps_length():
    long = "x " * 60
    out = _title(long)
    assert out is not None and len(out) <= 80 and out.endswith("...")


def test_empty_user_message_skips():
    assert asyncio.run(generate_title(_FakeProvider("x"), "m", "", "a")) is None


def test_empty_model_output_returns_none():
    assert _title("   ") is None


class _ErrProvider:
    def __init__(self, content, finish_reason):
        self._content = content
        self._fr = finish_reason

    async def chat(self, **kwargs):
        return _FakeResp(self._content, finish_reason=self._fr)


def test_error_finish_reason_returns_none():
    # Providers surface errors as a normal response (finish_reason="error"),
    # not an exception — that must never become a title.
    prov = _ErrProvider("Error calling LLM: Request timed out.", "error")
    assert asyncio.run(generate_title(prov, "m", "hi", "yo")) is None


def test_error_shaped_content_returns_none():
    # Even if finish_reason is missing, an "Error calling LLM:" body is rejected.
    prov = _ErrProvider("Error calling LLM: boom", "stop")
    assert asyncio.run(generate_title(prov, "m", "hi", "yo")) is None


def test_provider_error_returns_none():
    class _Boom:
        async def chat(self, **kwargs):
            raise RuntimeError("nope")
    assert asyncio.run(generate_title(_Boom(), "m", "hi", "yo")) is None


# ── list_sessions surfaces the title ──────────────────────────────────────

def _mgr(tmp_path):
    from flowly.session.manager import SessionManager
    mgr = SessionManager(workspace=tmp_path)
    mgr.sessions_dir = tmp_path  # isolate from the real ~/.flowly/sessions
    return mgr


def test_list_sessions_returns_title(tmp_path):
    mgr = _mgr(tmp_path)
    s = mgr.get_or_create("desktop:abc")
    s.add_message("user", "plan my trip")
    s.metadata["title"] = "Trip planning"
    mgr.save(s)

    listed = mgr.list_sessions()
    row = next(r for r in listed if r["key"] == "desktop:abc")
    assert row["title"] == "Trip planning"


def test_list_sessions_title_none_when_absent(tmp_path):
    mgr = _mgr(tmp_path)
    s = mgr.get_or_create("desktop:xyz")
    s.add_message("user", "hi")
    mgr.save(s)

    row = next(r for r in mgr.list_sessions() if r["key"] == "desktop:xyz")
    assert row.get("title") is None
