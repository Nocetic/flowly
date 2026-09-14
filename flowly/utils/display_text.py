"""Bound display-only tool output to the JavaScript client's UTF-16 limit."""

MAX_TOOL_RESULT_UNITS = 32_768


def bounded_tool_result(text: str) -> tuple[str, bool]:
    # No need to encode an arbitrarily large result: every code point costs
    # at least one unit. Keep one extra point to detect the boundary.
    encoded = text[:MAX_TOOL_RESULT_UNITS + 1].encode("utf-16-le", errors="surrogatepass")
    truncated = len(text) > MAX_TOOL_RESULT_UNITS or len(encoded) > MAX_TOOL_RESULT_UNITS * 2
    bounded = encoded[:MAX_TOOL_RESULT_UNITS * 2]
    if truncated and bounded and 0xD800 <= int.from_bytes(bounded[-2:], "little") <= 0xDBFF:
        bounded = bounded[:-2]
    # Malformed provider text must not poison JSON persistence or UI updates.
    return bounded.decode("utf-16-le", errors="replace").replace("\x00", "\ufffd"), truncated
