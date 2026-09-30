import pytest

from flowly.session.history_page import HistoryPageError, history_page


def rows(n=300):
    return [
        {"role": "user" if i % 3 == 0 else "assistant", "content": str(i), "_event_id": f"e{i}"}
        for i in range(n)
    ]


def test_old_clients_keep_complete_history():
    data = rows()
    assert history_page(data, "desktop:old", {}) == (data, {})


def test_pages_are_stable_during_append_and_cover_every_message_once():
    data = rows()
    page, state = history_page(data, "desktop:profile-home", {"limit": 10})
    found = page
    while state["hasOlder"]:
        page, state = history_page(
            data + rows(1), "desktop:profile-home", {"limit": 10, "before": state["before"]}
        )
        found = page + found
    assert [row["content"] for row in found] == [row["content"] for row in data]
    assert len({row["id"] for row in found}) == len(data)


def test_cursors_are_bound_to_session_and_missing_anchors_fail():
    data = rows()
    _, state = history_page(data, "desktop:a", {"limit": 10})
    for source, key in [(data, "desktop:b"), (data[:20], "desktop:a")]:
        with pytest.raises(HistoryPageError):
            history_page(source, key, {"before": state["before"]})


@pytest.mark.parametrize(
    "params", [{"limit": True}, {"limit": 0}, {"limit": 201}, {"before": "!!!"}, {"before": "e30="}]
)
def test_invalid_paging_is_rejected(params):
    with pytest.raises(HistoryPageError):
        history_page(rows(), "desktop:a", params)


def test_legacy_duplicate_rows_get_stable_distinct_ids_and_long_tools_are_bounded():
    data = [{"role": "assistant", "content": "same"} for _ in range(450)]
    page, state = history_page(data, "desktop:a", {"limit": 10})
    assert len(page) <= 200
    assert len({row["id"] for row in page}) == len(page)
    previous, _ = history_page(data, "desktop:a", {"limit": 10, "before": state["before"]})
    assert not {row["id"] for row in page} & {row["id"] for row in previous}
