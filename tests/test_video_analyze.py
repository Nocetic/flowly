"""Unit tests for VideoAnalyzeTool.

Covers MIME detection, base64 round-trip, SSRF blocking on private
IPs, size cap rejection, error categorisation, and the local-vs-URL
dispatch in execute().
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowly.agent.tools import video_analyze as va
from flowly.agent.tools.video_analyze import VideoAnalyzeTool

# ---- pure helpers -------------------------------------------------------


def test_detect_mime_known():
    assert va._detect_mime(Path("clip.mp4")) == "video/mp4"
    assert va._detect_mime(Path("/abs/CLIP.MOV")) == "video/quicktime"
    assert va._detect_mime(Path("foo.webm")) == "video/webm"
    assert va._detect_mime(Path("foo.mkv")) == "video/x-matroska"


def test_detect_mime_unknown():
    assert va._detect_mime(Path("foo.txt")) is None
    assert va._detect_mime(Path("foo")) is None


def test_data_url_roundtrip(tmp_path: Path):
    src = tmp_path / "tiny.mp4"
    payload = b"fakevideo" * 1024
    src.write_bytes(payload)
    url = va._to_data_url(src, "video/mp4")
    assert url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == payload


def test_estimated_data_url_bytes_accounts_for_base64_header():
    assert va._estimated_data_url_bytes(3, "video/mp4") == len("data:video/mp4;base64,") + 4
    assert va._estimated_data_url_bytes(4, "video/mp4") == len("data:video/mp4;base64,") + 8


def test_check_url_safe_blocks_private():
    assert va._check_url_safe("http://127.0.0.1/x.mp4") is not None
    assert va._check_url_safe("http://localhost/x.mp4") is not None
    assert va._check_url_safe("http://10.0.0.1/x.mp4") is not None
    assert va._check_url_safe("http://192.168.1.5/x.mp4") is not None


def test_check_url_safe_blocks_unsupported_scheme():
    assert va._check_url_safe("ftp://example.com/x.mp4") is not None
    assert va._check_url_safe("file:///etc/passwd") is not None


def test_categorize_error_payment():
    msg = va._categorize_error(Exception("HTTP 402: insufficient credits"))
    assert "credits" in msg.lower() or "payment" in msg.lower()


def test_categorize_error_video_unsupported():
    msg = va._categorize_error(Exception("model does not support video_url"))
    assert "video-capable" in msg.lower()


def test_categorize_error_too_large():
    msg = va._categorize_error(Exception("HTTP 413 payload too large"))
    assert "compress" in msg.lower() or "trim" in msg.lower()


def test_categorize_error_context_too_large():
    """Proxy returns context_too_large; should map to size hint."""
    msg = va._categorize_error(Exception("context_too_large: estimated 200K"))
    assert "compress" in msg.lower() or "trim" in msg.lower()


# ---- tool integration ---------------------------------------------------


@dataclass
class _FakeResponse:
    content: str | None = None


class _FakeProvider:
    def __init__(self, content: str | None = "OK analysis"):
        self.canned = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(content=self.canned)


def _run(coro):
    # Fresh loop per call so we don't reuse a closed one when pytest's
    # asyncio_mode=auto teardown has already shut the previous one down.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_local_path_inlines_base64(tmp_path: Path):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00\x00\x00 ftyp" + b"x" * 200)

    provider = _FakeProvider(content="It's a cat video.")
    tool = VideoAnalyzeTool(provider=provider)

    out = _run(tool.execute(video_url=str(video), question="What's in it?"))
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert "cat" in parsed["analysis"]

    call = provider.calls[0]
    assert call["model"] == "google/gemini-3.1-flash-lite"
    blocks = call["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    assert "What's in it?" in blocks[0]["text"]
    assert blocks[1]["type"] == "video_url"
    assert blocks[1]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_url_forwarded_without_base64(monkeypatch):
    """Public URLs go to the provider as-is; the model downloads them."""
    monkeypatch.setattr(va, "_check_url_safe", lambda _url: None)
    provider = _FakeProvider(content="describes the URL video")
    tool = VideoAnalyzeTool(provider=provider)

    out = _run(
        tool.execute(
            video_url="https://example.com/path/clip.mp4",
            question="describe",
        )
    )
    parsed = json.loads(out)
    assert parsed["success"] is True

    blocks = provider.calls[0]["messages"][0]["content"]
    assert blocks[1]["video_url"]["url"] == "https://example.com/path/clip.mp4"


def test_unsupported_local_extension(tmp_path: Path):
    weird = tmp_path / "clip.xyz"
    weird.write_bytes(b"junk")
    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)
    out = _run(tool.execute(video_url=str(weird), question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "Unsupported" in parsed["error"]
    assert provider.calls == []


def test_size_cap_rejection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(va, "_MAX_VIDEO_BYTES", 100)
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * 500)
    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)
    out = _run(tool.execute(video_url=str(big), question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "too large" in parsed["error"].lower()
    assert provider.calls == []


def test_local_path_encoded_payload_cap_rejection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(va, "_MAX_VIDEO_BYTES", 1_000)
    monkeypatch.setattr(va, "_MAX_LOCAL_VIDEO_DATA_URL_BYTES", 100)
    big = tmp_path / "encoded-too-big.mp4"
    big.write_bytes(b"x" * 90)
    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)

    out = _run(tool.execute(video_url=str(big), question="?"))
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert "base64" in parsed["error"].lower()
    assert provider.calls == []


def test_url_ssrf_blocked():
    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)
    out = _run(tool.execute(video_url="http://127.0.0.1/secret.mp4", question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert (
        "private" in parsed["error"].lower() or "internal" in parsed["error"].lower()
    )
    assert provider.calls == []


def test_empty_response_triggers_one_retry(tmp_path: Path):
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x" * 64)

    class _Retry:
        def __init__(self):
            self.n = 0
            self.calls = []

        async def chat(self, **kwargs):
            self.n += 1
            self.calls.append(kwargs)
            return _FakeResponse(content="" if self.n == 1 else "second-try")

    provider = _Retry()
    tool = VideoAnalyzeTool(provider=provider)
    out = _run(tool.execute(video_url=str(video), question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["analysis"] == "second-try"
    assert provider.n == 2


def test_provider_error_categorized(tmp_path: Path):
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x" * 64)

    class _Boom:
        async def chat(self, **kwargs):
            raise RuntimeError("HTTP 402: insufficient credits")

    tool = VideoAnalyzeTool(provider=_Boom())
    out = _run(tool.execute(video_url=str(video), question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert (
        "credits" in parsed["analysis"].lower()
        or "payment" in parsed["analysis"].lower()
    )


def test_missing_video_url():
    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)
    out = _run(tool.execute(video_url="", question="?"))
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert provider.calls == []


def test_default_model_env_override(monkeypatch, tmp_path: Path):
    """AUXILIARY_VIDEO_MODEL env var overrides the hardcoded default."""
    monkeypatch.setenv("AUXILIARY_VIDEO_MODEL", "google/gemini-2.5-pro")
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x" * 64)

    provider = _FakeProvider()
    tool = VideoAnalyzeTool(provider=provider)
    _run(tool.execute(video_url=str(video), question="?"))
    assert provider.calls[0]["model"] == "google/gemini-2.5-pro"


def test_model_argument_takes_priority(tmp_path: Path):
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x" * 64)

    provider = _FakeProvider()
    tool = VideoAnalyzeTool(
        provider=provider, default_model="google/gemini-3.1-flash-lite"
    )
    _run(
        tool.execute(
            video_url=str(video),
            question="?",
            model="google/gemini-2.5-pro",
        )
    )
    assert provider.calls[0]["model"] == "google/gemini-2.5-pro"


def test_schema_shape():
    tool = VideoAnalyzeTool(provider=_FakeProvider())
    schema = tool.to_schema()
    fn = schema["function"]
    assert fn["name"] == "video_analyze"
    props = fn["parameters"]["properties"]
    assert {"video_url", "question", "model"} <= set(props.keys())
    assert set(fn["parameters"]["required"]) == {"video_url", "question"}
