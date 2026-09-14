import pytest

from flowly.utils.display_text import bounded_tool_result


@pytest.mark.parametrize("text,expected,truncated", [
    ("a" + "🙂" * 20_000, "a" + "🙂" * 16_383, True),
    ("🙂" * 16_384, "🙂" * 16_384, False),
    ("x" * 32_769, "x" * 32_768, True),
    ("a\ud800b\udc00\x00", "a\ufffdb\ufffd\ufffd", False),
])
def test_tool_result_matches_javascript_wire_limit(text, expected, truncated):
    result = bounded_tool_result(text)
    assert result == (expected, truncated)
    assert len(result[0].encode("utf-16-le")) <= 65_536
    assert bounded_tool_result(result[0]) == (result[0], False)
