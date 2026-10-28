"""The arxiv skill's search script builds queries arXiv accepts.

Field syntax (``ti:transformer``) was put under ``all:`` and URL-quoted, which
the API answers with 400, and a plain sentence under one ``all:`` matched its
first word only (510 725 results for "graph neural networks").
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "flowly" / "skills" / "arxiv" / "scripts" / "search_arxiv.py"


@pytest.fixture(scope="module")
def arxiv():
    spec = importlib.util.spec_from_file_location("search_arxiv", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sent(arxiv, monkeypatch, **query) -> str:
    """The search_query arXiv receives, decoded the way arXiv decodes it."""
    sent: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def urlopen(request, timeout=0):
        sent.append(request.full_url)
        return _Response()

    monkeypatch.setattr(arxiv.urllib.request, "urlopen", urlopen)
    arxiv._fetch(arxiv._build_query(query.get("query"), query.get("author"), query.get("category"), None))
    return parse_qs(urlsplit(sent[0]).query)["search_query"][0]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("graph neural networks", "all:graph AND all:neural AND all:networks"),
        ("ti:transformer", "(ti:transformer)"),
        ('ti:"chain of thought"', '(ti:"chain of thought")'),
        ("au:hinton AND ti:capsule", "(au:hinton AND ti:capsule)"),
        ('"chain of thought" prompting', 'all:"chain of thought" AND all:prompting'),
    ],
)
def test_queries_reach_arxiv_in_its_own_syntax(arxiv, monkeypatch, query, expected) -> None:
    assert _sent(arxiv, monkeypatch, query=query) == expected


def test_an_author_with_a_full_name_is_one_phrase(arxiv, monkeypatch) -> None:
    assert _sent(arxiv, monkeypatch, author="Yann LeCun", category="cs.AI") == 'au:"Yann LeCun" AND cat:cs.AI'
