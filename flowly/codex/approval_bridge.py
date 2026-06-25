"""Bridge from Codex's server-initiated approval requests to Flowly's
central :class:`ApprovalManager`.

The Codex ``app-server`` subprocess pauses any sensitive action
(exec, apply_patch, permission escalation) and sends a JSON-RPC
``...requestApproval`` request to its parent. We answer with
``{"decision": "..."}``. Without this bridge our ``CodexSession``
silently auto-declines, so Codex can never actually do anything risky
— users see "engellendi" messages and the feature looks broken.

This module translates Codex's request shape into a Flowly
:class:`PendingApproval` and routes it through the same
:class:`ApprovalManager` the ``exec`` tool already uses. Approvals
therefore surface in whatever channels the user has wired (desktop
modal, mobile push, Telegram), with no per-tool plumbing.

Decision wire values (verified against codex-rs/app-server-protocol
v2/item.rs on codex 0.130.0+):

  * ``accept``           — allow once
  * ``acceptForSession`` — allow for the rest of the Codex thread
  * ``decline``          — reject

Flowly's :data:`ExecApprovalDecision` values map as:

  ============== ======================
  Flowly         Codex
  ============== ======================
  allow-once     accept
  allow-always   acceptForSession
  deny / None    decline
  ============== ======================

``None`` (timeout) deliberately falls through to ``decline`` — leaving
Codex stuck without a response would wedge the turn until the wedge
watchdog fires.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.types import (
    ExecApprovalDecision,
    ExecRequest,
    PendingApproval,
)

# Codex method names for the three approval surfaces — verified live
# against codex 0.130.0; we accept this set.
_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
_PERMISSION_APPROVAL = "item/permissions/requestApproval"


# How long the user has to answer a Codex approval prompt before we
# fall back to decline. Generous because mobile push round-trips
# can take 30+ seconds — but bounded so a forgotten prompt doesn't
# pin the Codex turn forever (the post-tool wedge watchdog kicks in
# at 90s anyway, so giving the approval flow a similar budget keeps
# the wedge cause clear in logs).
_APPROVAL_TIMEOUT_SECONDS = 120


def _decision_to_codex(decision: ExecApprovalDecision | None) -> str:
    """Translate a Flowly approval decision into Codex's wire string.

    ``None`` (timeout) maps to ``decline`` so Codex always gets a
    decisive answer — otherwise the turn loop on its side waits
    indefinitely.
    """
    if decision == "allow-once":
        return "accept"
    if decision == "allow-always":
        return "acceptForSession"
    return "decline"


def _build_pending_for_command(
    params: dict[str, Any], session_key: str,
) -> PendingApproval:
    """Wrap a Codex commandExecution approval as a Flowly PendingApproval.

    Codex's params shape (v2 item.rs):
      * ``command``: shell line (e.g. ``/bin/zsh -lc 'pytest -q'``)
      * ``cwd``: optional working directory; may be missing on some
        Codex versions, in which case we synthesise ``<codex>`` so the
        approval card doesn't show an empty path.
      * ``reason``: optional human string for *why* approval is needed
        (e.g. "command escapes sandbox"). Not on PendingApproval as a
        first-class field; we prepend it to ``risk_reasons`` so the UI
        modal can surface it.
    """
    command = str(params.get("command") or "")
    cwd = params.get("cwd") or None
    reason = params.get("reason") or "Codex requests approval to run this command"

    request = ExecRequest(
        command=command,
        cwd=cwd,
        session_key=session_key or None,
    )
    now = time.time()
    pending = PendingApproval(
        id=_make_pending_id("codex_exec"),
        request=request,
        created_at=now,
        expires_at=now + _APPROVAL_TIMEOUT_SECONDS,
        session_key=session_key or None,
        resolved_path=None,
        risk_reasons=[reason] if reason else [],
    )
    return pending


def _build_pending_for_file_change(
    params: dict[str, Any], session_key: str,
) -> PendingApproval:
    """Wrap a Codex fileChange (apply_patch) approval as a PendingApproval.

    Codex's ``fileChange`` approval params don't carry the full diff
    by themselves — the diff lives on the corresponding fileChange
    item. By design, we surface a synthesised
    command label so the existing exec-style approval UI has something
    descriptive to show even without the diff inline.

    Params shape:
      * ``reason``: e.g. "writes outside sandbox root"
      * ``grantRoot``: optional path the approval would grant
        write-access to
      * ``itemId``: the fileChange item id (the session layer may
        have cached a change summary keyed by this; we accept the id
        for forward-compatibility but don't dereference it here —
        the bridge stays decoupled from session-level state)
    """
    reason = params.get("reason") or "Codex requests approval to write files"
    grant_root = params.get("grantRoot")
    item_id = params.get("itemId") or ""

    label_parts = ["codex apply_patch"]
    if grant_root:
        label_parts.append(f"(grants write to {grant_root})")
    if item_id:
        label_parts.append(f"[{item_id}]")
    command = " ".join(label_parts)

    risk = [reason]
    if grant_root:
        risk.append(f"grant_root={grant_root}")

    request = ExecRequest(
        command=command,
        cwd=None,
        session_key=session_key or None,
    )
    now = time.time()
    return PendingApproval(
        id=_make_pending_id("codex_patch"),
        request=request,
        created_at=now,
        expires_at=now + _APPROVAL_TIMEOUT_SECONDS,
        session_key=session_key or None,
        resolved_path=grant_root,
        risk_reasons=risk,
    )


def _make_pending_id(prefix: str) -> str:
    """Generate an id distinct from the normal exec-approval id space.

    The id is opaque to the ApprovalManager — it just keys futures
    and notifications by it. Prefixing with ``codex_`` lets us
    distinguish Codex-originated approvals from regular exec ones in
    audit logs / debugging.
    """
    import secrets
    return f"{prefix}_{secrets.token_hex(8)}"


def build_codex_approval_callback(
    *,
    approval_manager: ApprovalManager,
    session_key_getter: Callable[[], str],
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Build the async callback :class:`CodexSession` expects.

    Args:
        approval_manager: The Flowly :class:`ApprovalManager` singleton
            (usually ``flowly.exec.approval_manager.get_approval_manager()``).
            All notify channels (desktop, mobile, Telegram, …) already
            attached to this manager will receive Codex approval
            prompts the same way they receive exec ones.
        session_key_getter: Returns the active Flowly session key for
            the in-flight turn. Used to scope notifications so the
            right user sees the prompt — without it a Codex prompt
            on session A could leak to a user looking at session B.

    Returns:
        Async callback compatible with
        :class:`flowly.codex.session.ApprovalCallback`. Takes the
        Codex JSON-RPC request envelope and returns the
        ``{"decision": "..."}`` reply payload.

    The callback handles the three Codex approval surfaces:

      * ``item/commandExecution/requestApproval``: routed through the
        manager as a synthetic ``ExecRequest`` carrying the command.
      * ``item/fileChange/requestApproval``: routed as a synthetic
        ``apply_patch`` pseudo-command so existing UI keeps working.
      * ``item/permissions/requestApproval``: declined directly.
        Users pick their permission profile up-front in
        ``~/.codex/config.toml``; surprise escalations should never
        auto-accept.

    Unknown methods also decline, so a future Codex schema bump that
    adds a new approval surface defaults to safe behaviour.
    """
    async def _callback(req: dict[str, Any]) -> dict[str, Any]:
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == _PERMISSION_APPROVAL:
            # Don't route permission escalations through the user UI —
            # the user already chose their profile in codex config.
            return {"decision": "decline"}

        if method == _COMMAND_APPROVAL:
            session_key = session_key_getter() or ""
            pending = _build_pending_for_command(params, session_key)
        elif method == _FILE_CHANGE_APPROVAL:
            session_key = session_key_getter() or ""
            pending = _build_pending_for_file_change(params, session_key)
        else:
            # Unknown approval surface — decline so Codex moves on.
            logger.warning(
                "[codex.approval] declining unknown method: %s", method,
            )
            return {"decision": "decline"}

        try:
            decision = await approval_manager.request_and_wait(pending)
        except Exception:
            logger.exception(
                "[codex.approval] manager raised; declining",
            )
            return {"decision": "decline"}

        return {"decision": _decision_to_codex(decision)}

    return _callback


__all__ = [
    "build_codex_approval_callback",
]
