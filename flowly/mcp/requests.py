"""Bounded SDK input-required continuation for public MCP request methods."""

from __future__ import annotations

from typing import Any


async def call_with_input(session: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    # Lightweight test/legacy session implementations keep their original API.
    if not callable(getattr(session, "dispatch_input_request", None)):
        return await getattr(session, method)(*args, **kwargs)
    from mcp import types
    from mcp.client._input_required import run_input_required_driver
    from mcp.client.session import ClientRequestContext
    from mcp.shared.exceptions import MCPError

    async def retry(responses, state):
        result = await getattr(session, method)(
            *args, **kwargs, input_responses=responses,
            request_state=state, allow_input_required=True,
        )
        if isinstance(result, types.InputRequiredResult) and len(result.input_requests or {}) > 16:
            raise MCPError(code=-32600, message="Too many MCP input requests in one round")
        return result

    async def dispatch(key, request):
        context = ClientRequestContext(session=session, request_id=key, meta=request.params.meta)
        return await session.dispatch_input_request(context, request)

    first = await retry(None, None)
    if not isinstance(first, types.InputRequiredResult):
        return first
    return await run_input_required_driver(first, dispatch=dispatch, retry=retry, max_rounds=8)
