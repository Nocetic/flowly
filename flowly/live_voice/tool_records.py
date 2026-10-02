"""Display records for tools the voice service runs itself.

Relay runs Live Voice's web search on Flowly's servers and returns the result
to the voice backend; the client then records the call here so the
conversation shows the same card after a reload. This stores what the user
already saw, on the agent only. Nothing here executes anything.
"""
from __future__ import annotations

from flowly.live_voice.sessions import VoiceError, bounded_text, identity

RECORDED_TOOLS = frozenset({"web_search"})
RECORD_STATUSES = frozenset({"running", "completed", "failed"})
MAX_QUERY_CHARS = 400
MAX_OUTPUT_CHARS = 16_384


def tool_arguments(name: str, value: object) -> dict:
    if name not in RECORDED_TOOLS:
        raise VoiceError("INVALID_PARAMS", "Only tools run by the voice service can be recorded.")
    if not isinstance(value, dict) or "query" not in value or set(value) - {"query", "news"}:
        raise VoiceError("INVALID_PARAMS", "A web search record carries its query only.")
    news = value.get("news")
    if news is not None and not isinstance(news, bool):
        raise VoiceError("INVALID_PARAMS", "Invalid news flag.")
    arguments = {"query": bounded_text(value.get("query"), "query", maximum=MAX_QUERY_CHARS)}
    if news:
        arguments["news"] = True
    return arguments


def record_tool(sessions, params: dict) -> dict:
    """Idempotent per command id: running first, then one settled status."""
    name = params.get("name")
    arguments = tool_arguments(name, params.get("arguments"))
    status = params.get("status")
    if status not in RECORD_STATUSES:
        raise VoiceError("INVALID_PARAMS", "Invalid tool status.")
    output = params.get("output")
    if status == "completed":
        output = bounded_text(output, "output", maximum=MAX_OUTPUT_CHARS)
    elif output is not None:
        raise VoiceError("INVALID_PARAMS", "Only a completed tool carries output.")
    record, _ = sessions.begin_tool(params, name=name, arguments=arguments)
    if status != "running":
        record = sessions.finish_tool(params.get("conversationId"), identity(params.get("commandId"), "commandId"),
                                      status=status, output=output) or record
    return {"record": record}
