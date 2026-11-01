"""A document the agent gives shows its first page (flowly/media/previews.py).

2026-11-01: a PDF arrived as a name and an icon; the owner asked for a preview
in every app, as an image has.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import sys

import pytest
from PIL import Image

from flowly.media import previews
from flowly.media.previews import MAX_DIMENSION, TARGET_BYTES, document_preview_b64


def make_pdf(path, text="Quarterly report"):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 120), text, fontsize=36)
    page.draw_rect(pymupdf.Rect(72, 200, 523, 600), color=(0.8, 0.1, 0.1), fill=(0.8, 0.1, 0.1))
    document.save(path)
    document.close()
    return path


def decoded(preview: str) -> Image.Image:
    raw = base64.b64decode(preview)
    assert raw[:3] == b"\xff\xd8\xff", "a JPEG"
    assert len(raw) <= TARGET_BYTES
    return Image.open(io.BytesIO(raw))


def test_a_pdf_shows_its_first_page_small(tmp_path):
    image = decoded(document_preview_b64(make_pdf(tmp_path / "report.pdf")))
    assert max(image.size) <= MAX_DIMENSION and image.size[1] > image.size[0], "portrait, like the page"
    red = image.getpixel((image.size[0] // 2, image.size[1] // 2))
    assert red[0] > 150 and red[1] < 90, "what is on the page is in the preview"


def test_a_broken_or_empty_document_has_no_preview_and_no_error(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert document_preview_b64(broken) is None
    assert document_preview_b64(tmp_path / "gone.pdf") is None
    notes = tmp_path / "notes.csv"
    notes.write_text("a,b\n1,2\n")
    assert document_preview_b64(notes) is None, "a type with no page has none"


def test_a_changed_file_is_drawn_again(tmp_path):
    path = make_pdf(tmp_path / "report.pdf", "First")
    first = document_preview_b64(path)
    make_pdf(path, "Second, and longer than the first")
    assert document_preview_b64(path) != first


def test_an_office_document_uses_quick_look_only_where_there_is_one(tmp_path, monkeypatch):
    doc = tmp_path / "brief.docx"
    doc.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(previews.sys, "platform", "linux")
    assert document_preview_b64(doc) is None


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("qlmanage") or not shutil.which("textutil"),
                    reason="macOS Quick Look")
def test_an_office_document_shows_its_first_page_on_a_mac(tmp_path):
    import subprocess

    text = tmp_path / "brief.txt"
    text.write_text("Brief for the launch\n" * 20)
    doc = tmp_path / "brief.docx"
    subprocess.run(["textutil", "-convert", "docx", str(text), "-output", str(doc)], check=True)
    preview = document_preview_b64(doc)
    if preview is None:
        pytest.skip("Quick Look gave no thumbnail here (sandboxed runner)")
    assert max(decoded(preview).size) <= MAX_DIMENSION


async def test_a_pdf_the_agent_gives_carries_its_first_page_on_the_wire(tmp_path, monkeypatch):
    from flowly.bus.events import OutboundMessage
    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    home = tmp_path / "home"
    (home / "media").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    pdf = make_pdf(home / "media" / "report.pdf")
    await channel.send(OutboundMessage(channel="web", chat_id="c", content="here", media=[str(pdf)], metadata={"run_id": "r"}))
    att = sent[-1]["data"]["attachments"][0]
    assert (att["kind"], att["mimeType"]) == ("file", "application/pdf")
    decoded(att["thumbnail"])
