"""Tests for tool argument coercion.

These tests are the regression safety net for macOS. The Windows fix must
NOT alter the behaviour for well-formed list inputs — every list-in,
list-out case below MUST keep passing byte-for-byte.
"""

from flowly.agent.tools._arg_coercion import coerce_str_list


# ── Happy paths: zero-op for well-formed inputs ──────────────────────────
# These are the macOS regression anchors. If any of these start returning
# something different, the Mac pipeline is broken.

def test_none_passes_through():
    assert coerce_str_list(None) is None


def test_empty_list_passes_through():
    assert coerce_str_list([]) == []


def test_macos_single_path_list_passes_through():
    """macOS happy path — one forward-slash path in a real list."""
    result = coerce_str_list(["/Users/hakan/.flowly/screenshots/s.png"])
    assert result == ["/Users/hakan/.flowly/screenshots/s.png"]


def test_macos_multiple_paths_list_passes_through():
    paths = ["/a/b.png", "/c/d.png"]
    result = coerce_str_list(paths)
    assert result == ["/a/b.png", "/c/d.png"]


def test_windows_path_in_real_list_passes_through():
    """If the model correctly emits a list, we leave it alone even on Windows."""
    paths = ["C:\\Users\\Hakan\\a.png"]
    result = coerce_str_list(paths)
    assert result == ["C:\\Users\\Hakan\\a.png"]


def test_list_with_non_string_items_stringified():
    """Defensive: mixed types get stringified."""
    assert coerce_str_list(["a", 42, None]) == ["a", "42", "None"]


# ── Recovery paths: the Windows Haiku bug scenarios ──────────────────────

def test_stringified_json_array_simple():
    assert coerce_str_list('["a.png", "b.png"]') == ["a.png", "b.png"]


def test_stringified_json_array_properly_escaped_windows():
    """Backslashes already doubled → strict json.loads succeeds."""
    result = coerce_str_list('["C:\\\\Users\\\\Hakan\\\\a.png"]')
    assert result == ["C:\\Users\\Hakan\\a.png"]


def test_stringified_json_array_raw_windows_backslashes():
    """The actual Haiku bug: raw \\U, \\H, \\S break json.loads.

    Uses Python raw-string to reproduce exactly what arrives from the
    tool dispatcher after outer JSON decoding.
    """
    bug = r'["C:\Users\Hakan\.flowly\screenshots\s.png"]'
    result = coerce_str_list(bug)
    assert result == ["C:\\Users\\Hakan\\.flowly\\screenshots\\s.png"]


def test_stringified_array_multiple_windows_paths():
    """Realistic Windows paths (\\Users, \\Downloads) — all segments start
    with letters that aren't JSON escapes (\\b, \\f, \\n, \\r, \\t, \\v),
    so the backslash-repair regex succeeds cleanly.

    Paths whose segment names happen to start with those six letters
    (rare — e.g. C:\\bin\\file.png) are a known residual edge case; downstream
    validation will flag them as missing files and the agent can retry.
    """
    bug = r'["C:\Users\Hakan\a.png", "D:\Downloads\b.png"]'
    result = coerce_str_list(bug)
    assert result == ["C:\\Users\\Hakan\\a.png", "D:\\Downloads\\b.png"]


def test_bare_string_path_wrapped():
    """Agent passes a single path as string instead of list."""
    assert coerce_str_list("/tmp/single.png") == ["/tmp/single.png"]


def test_empty_string_returns_none():
    assert coerce_str_list("") is None
    assert coerce_str_list("   ") is None


def test_malformed_array_manual_split():
    """Commas + quotes but not valid JSON → manual split fallback."""
    malformed = "['a.png', 'b.png']"
    result = coerce_str_list(malformed)
    assert result == ["a.png", "b.png"]


def test_param_name_and_tool_name_do_not_affect_output():
    """Diagnostic labels must be pure annotations."""
    base = coerce_str_list(["/a.png"])
    labelled = coerce_str_list(
        ["/a.png"], param_name="media_paths", tool_name="message"
    )
    assert base == labelled


def test_unparseable_string_wrapped_not_dropped():
    """Garbage input still returns something downstream validators can reject."""
    result = coerce_str_list("not-an-array-not-a-path")
    assert result == ["not-an-array-not-a-path"]


def test_non_string_non_list_wrapped():
    """Integer, dict, etc. → wrap as single-element list rather than crash."""
    assert coerce_str_list(42) == ["42"]
