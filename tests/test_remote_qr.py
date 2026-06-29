"""Remote-access QR: the scannable form of the typed connection block.

The QR must carry the *same* four values the user would otherwise type (host,
port, token, TLS) and render as a Rich-markup half-block code that the CLI and
TUI both print verbatim. These tests pin the URI shape, the never-crash
fallbacks, and the structural invariants a scanner depends on (rectangular grid,
only half-block glyphs, a light quiet zone).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from flowly.gateway.remote_qr import connect_uri, qr_markup, remote_qr_markup

_PREFIX = "[black on white]"
_SUFFIX = "[/]"


def _cells(markup: str) -> list[str]:
    """Strip the per-line style span back to the raw glyph grid."""
    out = []
    for line in markup.splitlines():
        assert line.startswith(_PREFIX) and line.endswith(_SUFFIX)
        out.append(line[len(_PREFIX) : -len(_SUFFIX)])
    return out


def test_connect_uri_carries_every_field() -> None:
    uri = connect_uri("192.168.1.5", 18790, "tok-abc", tls=False)
    parts = urlsplit(uri)
    assert parts.scheme == "flowly"
    assert parts.netloc == "remote"
    q = parse_qs(parts.query)
    assert q["host"] == ["192.168.1.5"]
    assert q["port"] == ["18790"]
    assert q["token"] == ["tok-abc"]
    assert q["tls"] == ["0"]


def test_connect_uri_tls_flag_and_token_escaping() -> None:
    # urlsafe-base64 tokens contain '-'/'_' (fine) but the encoder must still
    # percent-escape anything that would break the query (defensive).
    uri = connect_uri("host", 443, "a+b/c=d", tls=True)
    q = parse_qs(urlsplit(uri).query)
    assert q["tls"] == ["1"]
    assert q["token"] == ["a+b/c=d"]
    assert "a+b/c=d" not in uri  # was actually escaped, not passed raw


def test_qr_markup_is_a_rectangular_half_block_grid() -> None:
    markup = qr_markup("flowly://remote?host=10.0.0.2&port=18790&token=x", border=4)
    assert markup is not None
    rows = _cells(markup)
    assert rows, "expected at least one rendered row"
    # Every row the same width → a real rectangle a scanner can read.
    assert len({len(r) for r in rows}) == 1
    # Only the four half-block glyphs ever appear.
    assert set("".join(rows)) <= {"█", "▀", "▄", " "}


def test_qr_has_light_quiet_zone_border() -> None:
    # The first border rows and the leading columns must be blank (light) so the
    # code carries its own white frame even inside a dark bubble.
    rows = _cells(qr_markup("flowly://remote?host=x&token=y", border=4))
    # border=4 modules → 2 half-block rows of pure quiet zone at top.
    assert rows[0].strip() == ""
    assert rows[1].strip() == ""
    # Left margin: first two columns blank on every row.
    assert all(r[:2] == "  " for r in rows)


def test_remote_qr_markup_skips_when_nothing_safe_to_encode() -> None:
    assert remote_qr_markup("", 18790, "tok") is None
    assert remote_qr_markup("192.168.1.5", 18790, "") is None


def test_remote_qr_markup_round_trips_through_connect_uri() -> None:
    # The convenience wrapper must encode the very same URI connect_uri builds.
    direct = qr_markup(connect_uri("192.168.1.5", 18790, "tok-abc"))
    via = remote_qr_markup("192.168.1.5", 18790, "tok-abc")
    assert via == direct


def test_glyph_grid_reconstructs_segno_matrix() -> None:
    # The half-block rendering must be a lossless view of segno's matrix — decode
    # each glyph back to its two stacked modules and compare. This proves the
    # *visual* code is the correct QR for the URI without needing a scanner.
    import segno

    data = connect_uri("192.168.1.5", 18790, "tok-abc")
    expected = [[1 if m else 0 for m in row] for row in segno.make(data, error="m").matrix_iter(border=4)]

    inv = {"█": (1, 1), "▀": (1, 0), "▄": (0, 1), " ": (0, 0)}
    decoded: list[list[int]] = []
    for line in _cells(qr_markup(data, border=4)):
        top, bottom = [], []
        for ch in line:
            t, b = inv[ch]
            top.append(t)
            bottom.append(b)
        decoded.append(top)
        decoded.append(bottom)
    # An odd module count pads the final pair with a blank (light) row; the real
    # last row is bottom-quiet-zone (all light) too, so trimming is exact.
    assert decoded[: len(expected)] == expected
