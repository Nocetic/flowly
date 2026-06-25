"""Tests for the leaked tool-call markup sanitizer (flowly.agent.loop).

Some models emit a tool call as inline TEXT (deepseek's DSML markup, or
Anthropic-style ``<invoke>``) instead of the structured tool API. The loop's
guard strips that from the assistant's text answer so it never leaks into chat —
while leaving real prose (and fenced code examples) untouched.
"""

from flowly.agent.loop import _contains_leaked_tool_markup, _strip_leaked_tool_markup

# The exact leak observed in production (deepseek-v4-flash via the gateway).
REAL_LEAK = (
    '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="artifact"> '
    '<｜｜DSML｜｜parameter name="action" string="true">get</｜｜DSML｜｜parameter> '
    '<｜｜DSML｜｜parameter name="artifact_id" string="true">art_6a358cc3_55b17a48</｜｜DSML｜｜parameter> '
    '<｜｜DSML｜｜parameter name="limit" string="false">6000</｜｜DSML｜｜parameter> '
    '<｜｜DSML｜｜parameter name="offset" string="false">0</｜｜DSML｜｜parameter> '
    '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
)


def test_pure_leak_strips_to_empty():
    assert _contains_leaked_tool_markup(REAL_LEAK)
    assert _strip_leaked_tool_markup(REAL_LEAK) == ""


def test_leak_after_real_text_keeps_the_text():
    text = "Here is your summary of the research.\n\n" + REAL_LEAK
    assert _contains_leaked_tool_markup(text)
    assert _strip_leaked_tool_markup(text) == "Here is your summary of the research."


def test_anthropic_style_invoke_block_stripped():
    text = 'ok<invoke name="artifact"><parameter name="action">get</parameter></invoke>'
    assert _contains_leaked_tool_markup(text)
    assert _strip_leaked_tool_markup(text) == "ok"


def test_deepseek_native_markers_stripped():
    text = "answer <｜tool▁calls▁begin｜>stuff<｜tool▁calls▁end｜>"
    assert _contains_leaked_tool_markup(text)
    assert "tool" not in _strip_leaked_tool_markup(text).replace("stuff", "")


# --- must NOT touch legitimate content -------------------------------------

def test_normal_prose_untouched():
    text = "I'll invoke the API and parse the tool_calls field from the response."
    assert not _contains_leaked_tool_markup(text)
    assert _strip_leaked_tool_markup(text) == text


def test_code_fence_with_tool_syntax_preserved():
    text = (
        "Here's how the format looks:\n\n"
        "```xml\n"
        '<invoke name="search"><parameter name="q">cats</parameter></invoke>\n'
        "```\n\n"
        "Use it via the tool API."
    )
    # The only tool markup is inside a fence → not treated as a leak.
    assert not _contains_leaked_tool_markup(text)
    assert _strip_leaked_tool_markup(text) == text


def test_empty_and_none_safe():
    assert _strip_leaked_tool_markup("") == ""
    assert _strip_leaked_tool_markup(None) == ""
    assert not _contains_leaked_tool_markup("")
    assert not _contains_leaked_tool_markup(None)
