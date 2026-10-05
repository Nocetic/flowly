"""sessions.list pages by cursor: every row once, newest first, cheaply.

Clients render a long conversation list as it is scrolled. The first page
is polled; the pages behind it are read once. A cursor is a position
(modification time, file name), so a conversation that gets a message while
the owner scrolls moves to the top (the next first page) and the pages after
the cursor neither skip nor repeat a row.
"""

from __future__ import annotations

import os

import pytest

import flowly.channels.feature_rpc as feature_rpc
import flowly.profile as profiles
from flowly.channels.feature_rpc import FeatureRpcError, sessions_list


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "flowly-home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    if hasattr(profiles, "_cached_home"):
        profiles._cached_home = None
    monkeypatch.setattr(feature_rpc, "_pending_inputs", lambda: {})
    return home


def _write(home, name: str, at: int) -> None:
    """A conversation `desktop:<name>` last written `at` seconds into the epoch."""
    path = home / "sessions" / f"desktop_{name}.jsonl"
    path.write_text('{"_type": "metadata", "key": "desktop:%s", "metadata": {"title": "%s"}}\n' % (name, name))
    os.utime(path, ns=(at * 1_000_000_000, at * 1_000_000_000))


def _walk(limit: int) -> list[list[str]]:
    pages, before = [], None
    while True:
        result = sessions_list({"limit": limit, **({"before": before} if before else {})})
        pages.append([row["key"] for row in result["sessions"]])
        before = result["next"]
        if before is None:
            return pages


def test_pages_cover_the_list_newest_first_exactly_once(home):
    for index in range(7):
        _write(home, f"c{index}", 100 + index)
    assert _walk(3) == [["desktop:c6", "desktop:c5", "desktop:c4"],
                        ["desktop:c3", "desktop:c2", "desktop:c1"],
                        ["desktop:c0"]]
    # A page that ends exactly at the last row has no next page.
    assert _walk(7) == [[f"desktop:c{index}" for index in range(6, -1, -1)]]


def test_without_a_limit_the_whole_list_as_before(home):
    for index in range(4):
        _write(home, f"c{index}", 100 + index)
    result = sessions_list()
    assert [row["key"] for row in result["sessions"]] == ["desktop:c3", "desktop:c2", "desktop:c1", "desktop:c0"]
    assert "next" not in result
    assert sessions_list({}) == result


def test_a_conversation_that_moves_to_the_top_is_neither_skipped_nor_repeated(home):
    for index in range(6):
        _write(home, f"c{index}", 100 + index)
    first = sessions_list({"limit": 2})
    assert [row["key"] for row in first["sessions"]] == ["desktop:c5", "desktop:c4"]
    # c1, still behind the cursor, gets a message and becomes the newest.
    _write(home, "c1", 200)
    rest = sessions_list({"limit": 10, "before": first["next"]})
    assert [row["key"] for row in rest["sessions"]] == ["desktop:c3", "desktop:c2", "desktop:c0"]
    assert [row["key"] for row in sessions_list({"limit": 2})["sessions"]] == ["desktop:c1", "desktop:c5"]


def test_through_refreshes_down_to_a_cursor_however_much_arrived(home):
    for index in range(6):
        _write(home, f"c{index}", 100 + index)
    first = sessions_list({"limit": 2})
    boundary = first["next"]  # The owner scrolled past c4.
    # Three new conversations push the first page's rows below its window.
    for index in range(3):
        _write(home, f"new{index}", 300 + index)
    above = sessions_list({"limit": 200, "through": boundary})
    assert [row["key"] for row in above["sessions"]] == [
        "desktop:new2", "desktop:new1", "desktop:new0", "desktop:c5", "desktop:c4"]
    assert above["next"] is None
    # Bounded like any page, and continued between the two cursors.
    top = sessions_list({"limit": 2, "through": boundary})
    rest = sessions_list({"limit": 200, "through": boundary, "before": top["next"]})
    assert [row["key"] for row in top["sessions"] + rest["sessions"]] == [row["key"] for row in above["sessions"]]
    # Below the boundary the pages are unchanged.
    assert [row["key"] for row in sessions_list({"limit": 200, "before": boundary})["sessions"]] == [
        "desktop:c3", "desktop:c2", "desktop:c1", "desktop:c0"]


def test_rows_written_in_the_same_instant_page_by_file_name(home):
    for name in ("a", "b", "c", "d"):
        _write(home, name, 100)
    assert _walk(1) == [["desktop:d"], ["desktop:c"], ["desktop:b"], ["desktop:a"]]


def test_a_page_reads_only_its_own_headers_and_skips_unreadable_rows(home, monkeypatch):
    for index in range(50):
        _write(home, f"c{index:02d}", 100 + index)
    (home / "sessions" / "desktop_c48.jsonl").write_text("not json\n")
    os.utime(home / "sessions" / "desktop_c48.jsonl", ns=(148 * 10**9, 148 * 10**9))
    reads = []
    real = feature_rpc._session_row
    monkeypatch.setattr(feature_rpc, "_session_row", lambda *args: reads.append(args[0].name) or real(*args))
    page = sessions_list({"limit": 5})
    # The broken row does not take a place on the page.
    assert [row["key"] for row in page["sessions"]] == [f"desktop:c{index}" for index in (49, 47, 46, 45, 44)]
    assert len(reads) == 6
    assert page["next"] is not None


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"limit": "5"}, {"limit": True}, {"limit": 2.5},
    {"limit": 5, "before": "garbage"}, {"limit": 5, "before": 12},
    {"limit": 5, "before": "v1:1:../../etc/passwd"}, {"limit": 5, "before": "v1:1:a\\b"},
    {"limit": 5, "before": "v1:" + "9" * 21 + ":a"}, {"limit": 5, "through": "nope"},
])
def test_invalid_paging_is_refused(home, params):
    with pytest.raises(FeatureRpcError) as error:
        sessions_list(params)
    assert error.value.code == "INVALID_PARAMS"


def test_dispatch_passes_the_paging_params(home):
    handler, takes_params, needs_restart = feature_rpc._DISPATCH["sessions.list"]
    assert (handler, takes_params, needs_restart) == (sessions_list, True, False)


def test_the_bot_says_it_pages_so_clients_never_page_an_old_one(home):
    # An old gateway honoured ``limit`` by cutting its list and gave no cursor,
    # so a client that paged it would show the newest page as the whole list.
    assert feature_rpc.system_capabilities()["sessionsListVersion"] == 2
