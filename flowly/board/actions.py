"""Shared board-action core.

The gateway exposes the board over HTTP (`/api/board/action`, desktop) and a
direct WS RPC; the relay can't reach those. This module factors the action
logic so the ``feature_rpc`` ``board.action`` handler can drive the SAME
single-writer ``BoardStore`` over either transport — add / move / run a card
from any client, relay or gateway.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger


async def apply_board_action(store: Any, orchestrator: Any, body: dict) -> tuple[dict, int]:
    """Apply one board action; return ``(result, http_status)``.

    ``store`` is the agent's ``BoardStore`` (single owner); ``orchestrator``
    drives card execution (``run`` / ``cancel``) and may be ``None`` when board
    execution isn't wired. Body: ``{"action": "add|move|update|note|delete|
    clear|assign|unassign|link|run|cancel", ...}``.
    """
    from flowly.board.store import (
        STATUS_CANCELLED,
        STATUS_DONE,
        STATUS_READY,
        STATUS_TODO,
        BoardError,
    )

    if store is None:
        return {"ok": False, "error": "Board not configured"}, 500

    raw_action = body.get("action")
    if not isinstance(raw_action, str):
        return {"ok": False, "error": "action must be a string"}, 400
    action = raw_action.strip()
    try:
        if action == "add":
            idempotency_key = body.get("idempotencyKey", "") or ""
            if not isinstance(idempotency_key, str):
                return {"ok": False, "error": "idempotencyKey must be a string"}, 400
            existing = store.get_card_by_idempotency_key(idempotency_key)
            if existing is not None:
                return {
                    "ok": True,
                    "card": existing.to_dict(),
                    "idempotentReplay": True,
                }, 200
            card = store.add_card(
                body.get("title") or "",
                body=body.get("body", "") or "",
                origin_channel=body.get("originChannel", "app") or "app",
                origin_chat_id=body.get("originChatId", "") or "",
                created_by="user",
                priority=body.get("priority", 0),
                scheduled_at=body.get("scheduledAt"),
                idempotency_key=idempotency_key,
                max_attempts=body.get("maxAttempts", 2),
            )
            assignee = str(body.get("assigneeProfile") or "").strip()
            if assignee:
                if orchestrator is None:
                    store.delete_card(card.id)
                    return {"ok": False, "error": "bot assignment is not available"}, 400
                try:
                    card = orchestrator.assign_card(card.id, assignee)
                except Exception:
                    store.delete_card(card.id)
                    raise
            return {"ok": True, "card": card.to_dict()}, 200

        if action in ("clear_done", "clear"):
            removed = store.delete_by_status(body.get("status") or STATUS_DONE)
            return {"ok": True, "removed": removed}, 200

        card_id = body.get("cardId") or body.get("card_id") or ""
        if not card_id:
            return {"ok": False, "error": "cardId required"}, 400
        expected = body.get("expectedRevision")
        if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
            return {"ok": False, "error": "expectedRevision must be an integer"}, 400

        if action == "move":
            card = store.set_status(
                card_id,
                body.get("status") or "",
                expected_revision=expected,
                actor="user",
            )
            if orchestrator is not None:
                orchestrator.wake_dispatcher()
            return {"ok": True, "card": card.to_dict()}, 200

        if action == "update":
            updates = {}
            if "priority" in body:
                updates["priority"] = body["priority"]
            if "scheduledAt" in body:
                updates["scheduled_at"] = body["scheduledAt"]
            if "maxAttempts" in body:
                updates["max_attempts"] = body["maxAttempts"]
            return {
                "ok": True,
                "card": store.update_card(
                    card_id,
                    title=body.get("title"),
                    body=body.get("body"),
                    expected_revision=expected,
                    actor="user",
                    **updates,
                ).to_dict(),
            }, 200

        if action == "note":
            store.add_note(card_id, author=body.get("author", "user") or "user", text=body.get("text", "") or "")
            card = store.get_card(card_id)
            return {"ok": True, "card": card.to_dict() if card else None}, 200

        if action == "assign":
            if orchestrator is None:
                return {"ok": False, "error": "bot assignment is not available"}, 400
            profile = str(body.get("profile") or body.get("assigneeProfile") or "").strip()
            card = orchestrator.assign_card(
                card_id,
                profile,
                expected_revision=expected,
            )
            return {"ok": True, "card": card.to_dict()}, 200

        if action == "unassign":
            if orchestrator is None:
                return {"ok": False, "error": "bot assignment is not available"}, 400
            card = orchestrator.unassign_card(
                card_id,
                expected_revision=expected,
            )
            return {"ok": True, "card": card.to_dict()}, 200

        if action == "link":
            parent_id = str(body.get("parentId") or "").strip()
            if not parent_id:
                return {"ok": False, "error": "parentId required"}, 400
            store.link_cards(parent_id, card_id)
            card = store.get_card(card_id)
            return {"ok": True, "card": card.to_dict() if card else None}, 200

        if action == "delete":
            return {"ok": store.delete_card(card_id)}, 200

        if action == "run":
            if orchestrator is None:
                return {"ok": False, "error": "board execution not available"}, 400
            card = store.get_card(card_id)
            if card is None:
                return {"ok": False, "error": "card not found"}, 404

            def _log_done(t: "asyncio.Task") -> None:
                if not t.cancelled() and t.exception() is not None:
                    logger.error(f"[board] run_card {card_id} failed: {t.exception()}")

            # deliver=False: a UI-initiated run only updates the card (its result
            # lands on the card itself, shown in the board detail). We do NOT push
            # the result back into the origin conversation — otherwise running a
            # card from the desktop/iOS board would post the answer as a chat
            # message on the relay. Chat-originated background tasks still relay,
            # because the agent runs those through its board_run tool in-turn.
            asyncio.ensure_future(
                orchestrator.run_card(card_id, deliver=False)
            ).add_done_callback(_log_done)
            return {"ok": True, "status": "started", "card": card.to_dict()}, 200

        if action == "cancel":
            if orchestrator is not None:
                await orchestrator.cancel_card(card_id)
            else:
                store.set_status(card_id, STATUS_CANCELLED)
            card = store.get_card(card_id)
            return {"ok": True, "card": card.to_dict() if card else None}, 200

        if action == "retry":
            card = store.get_card(card_id)
            if card is None:
                return {"ok": False, "error": "card not found"}, 404
            status = STATUS_READY if card.assignee_profile else STATUS_TODO
            card = store.set_status(
                card_id,
                status,
                clear_run_id=True,
                clear_schedule=True,
                expected_revision=expected,
                actor="user",
            )
            if orchestrator is not None:
                orchestrator.wake_dispatcher()
            return {"ok": True, "card": card.to_dict()}, 200

        return {"ok": False, "error": f"unknown action: {action}"}, 400
    except BoardError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        logger.error(f"[board] action error {action!r}: {e}")
        return {"ok": False, "error": "Internal server error"}, 500
