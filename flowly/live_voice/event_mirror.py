"""Freeze one original event source before direct and relay delivery diverge."""
import json

from loguru import logger

from flowly.live_voice.events import event_access_scope


async def mirror_event(gateway, web, event_name: str, data: dict) -> None:
    event = gateway._scoped_event({'type': 'event', 'event': event_name, 'data': data})
    try:
        await gateway._broadcast_clients(event)
    finally:
        if web is not None:
            try:
                with event_access_scope(event.access):
                    await web._send_or_queue(json.dumps(event))
            except Exception as error:
                logger.warning('Relay event mirror deferred ({})', type(error).__name__)
