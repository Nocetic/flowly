"""Transport-agnostic feature RPC handlers.

These implement the desktop/iOS feature RPCs (connections, config, memory, kg,
sessions, audit, persona, provider, skills, assistants, pairing) with **no
coupling to a transport**. Both the relay channel (``flowly.channels.web``) and
the direct gateway (``flowly.gateway.server``) call them and wrap the returned
``dict`` in their own JSON-RPC reply envelope. "Write once, serve over relay +
gateway."

Contract
--------
* Each handler returns a plain JSON-serialisable ``dict`` (the RPC ``result``)
  or raises :class:`FeatureRpcError` for a structured error (``code`` +
  ``message``). Any other exception propagates; the transport maps it to an
  INTERNAL error and logs it.
* Mutations that need a gateway restart to apply (channel/tool config) return
  ``{"willRestart": bool}`` but **do not** restart. The transport ACKs the
  client first, then schedules the restart, because the restart bounces the
  very process the connection lives in — awaiting it would cut the socket
  before the reply flushed.
* Handlers read the same on-disk state under ``~/.flowly`` the local TUI/CLI
  use, so a remote client sees exactly what the bot itself sees. Read paths use
  ``mode=ro`` sqlite and never mutate; write paths are explicit.

Sync vs async
-------------
Only :func:`connections_list` is ``async`` (integration-card probes are
awaitable). Everything else is synchronous; callers invoke them directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flowly.profile import get_flowly_home

# Integration categories a remote client may view/edit. ``provider`` (LLM keys)
# has its own surface; ``system`` is internal. ``media`` (FAL image generation)
# is a generic card — masked key + model picker — so it renders through the same
# field-by-type path as channels/tools with no client change.
CONNECTION_CATEGORIES = ("channel", "tool", "voice", "media", "web_search")
_PAIRING_CHANNELS = ("telegram", "whatsapp", "imessage")


class FeatureRpcError(Exception):
    """Structured RPC error — the transport maps ``code``/``message`` into its
    own error envelope (and does not log it as a crash)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Shared on-disk locations ────────────────────────────────────────────────

def workspace_dir() -> Path:
    """Workspace root on the bot host (``~/.flowly/workspace``)."""
    return get_flowly_home() / "workspace"


def audit_dir() -> Path:
    return get_flowly_home() / "audit"


def state_db(filename: str) -> Path:
    """Resolve a state sqlite file, probing the known locations (newest layout
    first). Returns the first existing candidate, else the canonical path."""
    home = get_flowly_home()
    candidates = [
        home / "workspace" / ".flowly_state" / filename,
        home / filename,
        home / "workspace" / filename,
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


# ── Connections (integration cards: channels / tools / voice / media) ───────

async def connections_list() -> dict:
    """List integration cards with masked PASSWORD fields + live probe status."""
    from flowly.integrations.registry import list_cards
    from flowly.integrations.config_io import read_card_values
    from flowly.integrations.cards import FieldType

    out = []
    for card in (c for c in list_cards() if c.category in CONNECTION_CATEGORIES):
        values = read_card_values(card)
        masked = {
            f.key: (
                "••••••••"
                if (f.type == FieldType.PASSWORD and values.get(f.key))
                else values.get(f.key)
            )
            for f in card.fields
        }
        probe_status: str | None = None
        probe_detail = ""
        if card.probe:
            try:
                probe = await card.probe(values)
                probe_status = probe.status
                probe_detail = probe.detail
            except Exception:
                probe_status = None
        out.append({
            "key": card.key,
            "label": card.label,
            "category": card.category,
            "enabled": bool(values.get("enabled", False)),
            "connected": probe_status == "ok",
            "probeStatus": probe_status or "unknown",
            "probeDetail": probe_detail,
            "values": masked,
            "fields": [{
                "key": f.key,
                "label": f.label,
                "type": f.type.value,
                "required": f.required,
                "placeholder": f.placeholder,
                "help": f.help,
                "choices": f.choices,
            } for f in card.fields],
            "needsRestart": card.needs_gateway_restart,
        })
    return {"connections": out}


def connections_set(params: dict) -> dict:
    """Apply a channel/integration's values (or clear it). Returns
    ``{"ok": True, "willRestart": bool}`` — the caller schedules the restart."""
    from flowly.integrations.registry import get_card
    from flowly.integrations.config_io import (
        CardValidationError, read_card_values, apply_card_values, clear_card,
    )

    key = params.get("key", "")
    card = get_card(key)
    if not card or card.category not in CONNECTION_CATEGORIES:
        raise FeatureRpcError("NOT_FOUND", f"unknown connection: {key}")

    if params.get("clear"):
        clear_card(card)
    else:
        values = params.get("values") or {}
        if not isinstance(values, dict):
            raise FeatureRpcError("INVALID", "values must be an object")
        merged = read_card_values(card)
        merged.update(values)
        try:
            apply_card_values(card, merged)
        except CardValidationError as exc:
            # A bad field value would brick config.json at next boot — reject it
            # with a clear message instead of corrupting the file + losing the save.
            raise FeatureRpcError("INVALID", f"rejected: {exc}") from exc

    will_restart = bool(card.needs_gateway_restart and params.get("restart", True))
    return {"ok": True, "willRestart": will_restart}


# ── MCP servers (manage external Model Context Protocol servers) ─────────────
#
# Thin transport-agnostic wrappers over flowly.integrations.mcp_io (the same
# primitives the TUI /mcp modal uses). Changes to mcpServers take effect at the
# next agent boot, so mutations are restart-aware like connections.set.

def _mcp_entry_dict(e) -> dict:
    """Serialise an ``MCPServerEntry`` for the wire (camelCase)."""
    return {
        "name": e.name,
        "transport": e.transport,
        "enabled": e.enabled,
        "auth": e.auth,
        "toolFilter": e.tool_filter,
        "source": e.source,
        "description": e.description,
        "status": e.status,
        "needsOauth": e.needs_oauth,
        "needsSecrets": e.needs_secrets,
        "secretFields": [
            {"name": f.name, "prompt": f.prompt, "secret": f.secret, "default": f.default}
            for f in (e.secret_fields or [])
        ],
        "error": e.error,
        "authorized": e.authorized,
    }


def mcp_list() -> dict:
    """Configured MCP servers + installable catalog entries (same as TUI /mcp)."""
    from flowly.integrations.mcp_io import list_mcp_servers
    return {"servers": [_mcp_entry_dict(e) for e in list_mcp_servers()]}


def mcp_upsert(params: dict) -> dict:
    """Add or replace a manually-configured server. Restart-aware."""
    from flowly.integrations.mcp_io import upsert_mcp_server

    name = params.get("name") or ""
    config = params.get("config") or {}
    ok, message = upsert_mcp_server(name, config)
    if not ok:
        raise FeatureRpcError("INVALID", message)
    return {"ok": True, "message": message, "willRestart": bool(params.get("restart", True))}


def mcp_set_enabled(params: dict) -> dict:
    """Flip a configured server's ``enabled`` flag. Restart-aware."""
    from flowly.integrations.mcp_io import set_mcp_enabled

    name = params.get("name") or ""
    enabled = bool(params.get("enabled", True))
    try:
        set_mcp_enabled(name, enabled)
    except KeyError:
        raise FeatureRpcError("NOT_FOUND", f"unknown MCP server: {name}")
    return {"ok": True, "enabled": enabled, "willRestart": bool(params.get("restart", True))}


def mcp_remove(params: dict) -> dict:
    """Delete a configured server (+ its cached OAuth tokens). Restart-aware."""
    from flowly.integrations.mcp_io import remove_mcp_server

    name = params.get("name") or ""
    removed = remove_mcp_server(name)
    if not removed:
        raise FeatureRpcError("NOT_FOUND", f"unknown MCP server: {name}")
    return {"ok": True, "willRestart": bool(params.get("restart", True))}


def mcp_install(params: dict) -> dict:
    """Install a curated catalog entry (writing any collected secrets). Restart-aware."""
    from flowly.integrations.mcp_io import install_catalog_server

    name = params.get("name") or ""
    env_values = params.get("envValues") or params.get("env_values") or {}
    if not isinstance(env_values, dict):
        raise FeatureRpcError("INVALID", "envValues must be an object")
    ok, message = install_catalog_server(name, env_values)
    if not ok:
        raise FeatureRpcError("INVALID", message)
    # OAuth catalog entries still need a browser login before they connect.
    needs_oauth = "flowly mcp login" in message
    return {
        "ok": True,
        "message": message,
        "needsOauth": needs_oauth,
        "willRestart": bool(params.get("restart", True)),
    }


def mcp_test(params: dict) -> dict:
    """Connect once and report tools — for a saved server (by ``name``) or an
    unsaved config (``config``, pre-save validation). Never restarts."""
    name = (params.get("name") or "").strip()
    config = params.get("config")
    interactive = bool(params.get("interactive"))

    if config:
        from flowly.config.loader import convert_keys
        from flowly.config.schema import MCPServerConfig
        from flowly.mcp.probe import probe_tool_names
        try:
            dump = MCPServerConfig(**convert_keys(config)).model_dump()
        except Exception as exc:
            raise FeatureRpcError("INVALID", f"invalid server config: {exc}")
        ok, tools, error = probe_tool_names(name or "test", dump, interactive=interactive)
    elif name:
        from flowly.integrations.mcp_io import probe_mcp_server
        ok, tools, error = probe_mcp_server(name, interactive=interactive)
    else:
        raise FeatureRpcError("INVALID", "name or config is required")
    return {"ok": ok, "tools": tools, "error": error}


def mcp_oauth_start(params: dict) -> dict:
    """Run the OAuth browser flow for an OAuth-configured server on the bot host.

    Clears cached tokens first, then connects interactively (opens the browser
    on whichever host the bot runs on). Works cleanly for a **local** bot; for a
    remote/VPS bot the browser opens on the host, so the desktop should only
    offer this for local bots. Restart-aware on success so the agent reconnects
    with the fresh grant.
    """
    from flowly.integrations.mcp_io import _server_config_dump
    name = (params.get("name") or "").strip()
    if not name:
        raise FeatureRpcError("INVALID", "name is required")
    dump = _server_config_dump(name)
    if dump is None:
        raise FeatureRpcError("NOT_FOUND", f"unknown MCP server: {name}")
    if not dump.get("url"):
        raise FeatureRpcError("INVALID", f"{name} is stdio — OAuth applies to http servers")
    if dump.get("auth") != "oauth":
        raise FeatureRpcError("INVALID", f"{name} is not configured for OAuth (auth: oauth)")

    try:
        from flowly.mcp.oauth import clear_tokens, oauth_available
    except Exception as exc:
        raise FeatureRpcError("UNAVAILABLE", f"OAuth runtime not importable: {exc}")
    if not oauth_available():
        raise FeatureRpcError("UNAVAILABLE", "this 'mcp' SDK build lacks OAuth support")

    clear_tokens(name)
    from flowly.mcp.probe import probe_message
    ok, message = probe_message(name, dump, interactive=True)
    if not ok:
        raise FeatureRpcError("AUTH_FAILED", message)
    return {"ok": True, "message": message, "willRestart": bool(params.get("restart", True))}


# ── Config (raw config.json, deep-merge patch) ──────────────────────────────

def config_get() -> dict:
    """Return the raw config.json — same shape the desktop reads locally."""
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import _load_raw_or_empty
    # Tolerate a transiently malformed config.json (the loader self-heals from
    # the same state via .bak) — never crash the remote Settings load.
    return {"config": _load_raw_or_empty(get_config_path())}


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def config_set(params: dict) -> dict:
    """Deep-merge a config patch into config.json. Returns
    ``{"ok": True, "willRestart": bool}`` — caller schedules the restart.

    Writes through the integrations atomic writer: temp-file + ``os.replace``,
    a ``.bak`` of the previous file, and owner-only (0600 / Windows ACL) perms.
    config.json holds provider API keys, so a plain in-place write would both
    drop those perms and risk an unrecoverable half-written file on a crash."""
    from flowly.config.loader import get_config_path
    from flowly.integrations.config_io import _atomic_write_json, _load_raw_or_empty

    patch = params.get("config") or params.get("patch") or {}
    if not isinstance(patch, dict):
        raise FeatureRpcError("INVALID", "config patch must be an object")

    path = get_config_path()
    merged = _deep_merge(_load_raw_or_empty(path), patch)
    _atomic_write_json(path, merged)
    return {"ok": True, "willRestart": bool(params.get("restart"))}


# ── Pet (Petdex floating companion) ─────────────────────────────────────────

def _pet_err(exc: Exception) -> FeatureRpcError:
    return FeatureRpcError(getattr(exc, "code", "PET_ERROR"), getattr(exc, "message", str(exc)))


def pet_info(params: dict) -> dict:
    from flowly.pet import service
    try:
        return service.get_info()
    except service.PetServiceError as exc:
        raise _pet_err(exc) from exc


async def pet_gallery(params: dict) -> dict:
    from flowly.pet import service
    try:
        return await service.get_gallery()
    except service.PetServiceError as exc:
        raise _pet_err(exc) from exc


async def pet_select(params: dict) -> dict:
    from flowly.pet import service
    slug = (params.get("slug") or "").strip()
    if not slug:
        raise FeatureRpcError("INVALID", "slug is required")
    try:
        return await service.select(slug)
    except service.PetServiceError as exc:
        raise _pet_err(exc) from exc


def pet_disable(params: dict) -> dict:
    from flowly.pet import service
    return service.disable()


def pet_scale(params: dict) -> dict:
    from flowly.pet import service
    if "scale" not in params:
        raise FeatureRpcError("INVALID", "scale is required")
    try:
        return service.set_scale(params["scale"])
    except service.PetServiceError as exc:
        raise _pet_err(exc) from exc


async def pet_thumb(params: dict) -> dict:
    from flowly.pet import service
    slug = (params.get("slug") or "").strip()
    if not slug:
        raise FeatureRpcError("INVALID", "slug is required")
    try:
        return await service.get_thumb(slug)
    except service.PetServiceError as exc:
        raise _pet_err(exc) from exc


# ── Memory (entries + USER.md) ──────────────────────────────────────────────

def chat_inflight(params: dict) -> dict:
    """Partial text of a still-streaming run for this session, or null.

    Served over BOTH relay and direct gateway, so a client that left a
    chat mid-stream and came back can restore the live bubble instead of
    staring at a blank screen until the run finishes. ``sessionKey`` is
    the same key the client tags its chat.send with.
    """
    from flowly.agent.inflight import get as _inflight_get
    session_key = (params.get("sessionKey") or "").strip()
    cur = _inflight_get(session_key) if session_key else None
    return {"inflight": cur}


def memory_entries() -> dict:
    """MEMORY.md date-stamped blocks + USER.md — the desktop listMemories()
    shape."""
    ws = workspace_dir()
    memory_path = ws / "memory" / "MEMORY.md"
    user_path = ws / "USER.md"

    entries: list[dict] = []
    if memory_path.exists():
        from flowly.memory.summary import extract_manual_content

        # The governed generated region duplicates what memory.gov_list
        # returns and is regenerated on every refresh — only the freeform
        # (human/agent-appended) blocks are entries.
        raw = extract_manual_content(memory_path.read_text(encoding="utf-8"))
        # Split on each ``<!-- YYYY-MM-DD HH:MM -->`` marker (kept with the
        # block that follows it), mirroring the desktop parser.
        for block in re.split(r"(?=<!--\s*\d{4}-\d{2}-\d{2})", raw):
            if not block.strip():
                continue
            m = re.search(r"<!--\s*(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})\s*-->", block)
            date = m.group(1).strip() if m else ""
            content = re.sub(r"<!--.*?-->\s*", "", block, flags=re.DOTALL).strip()
            if content:
                entries.append({"date": date, "content": content})

    user = user_path.read_text(encoding="utf-8") if user_path.exists() else None
    return {"memory": entries, "user": user}


def memory_update_user(params: dict) -> dict:
    """Overwrite USER.md (the user profile) on the bot host."""
    content = params.get("content")
    if not isinstance(content, str):
        raise FeatureRpcError("INVALID", "content must be a string")
    ws = workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "USER.md").write_text(content, encoding="utf-8")
    return {"ok": True}


# ── Memory governance (review queue / stats / accept / reject / …) ──────────

def _open_memory_gov():
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.coordinator import MemoryGovernance
    from flowly.agent.memory import MemoryStore
    gov = GovernanceStore(state_db("memory_governance.sqlite3"))
    return MemoryGovernance(gov, memory_store=MemoryStore(workspace_dir()))


def _obsidian_cfg():
    """Return the resolved ObsidianConfig from config.json, or None."""
    from flowly.config.loader import load_config
    cfg = load_config()
    return getattr(getattr(cfg, "integrations", None), "obsidian", None)


def obsidian_rpc(action: str, params: dict) -> dict:
    """Obsidian vault RPC: search the vault and report status.

    Settings (enable / vault path / policy) flow through ``connections.list``
    and ``connections.set`` on the Obsidian integration card. Review/accept of
    vault-derived memory candidates flow through the ``memory.*`` methods.
    """
    cfg = _obsidian_cfg()
    if cfg is None or not getattr(cfg, "enabled", False):
        if action == "status":
            return {"configured": False, "enabled": False}
        raise FeatureRpcError("NOT_CONFIGURED", "Obsidian integration is disabled")

    from flowly.obsidian.tools import ObsidianRuntime
    from flowly.obsidian.vault import VaultNotConfigured, VaultPermissionDenied

    state_dir = state_db("obsidian_index.sqlite").parent
    rt = ObsidianRuntime(cfg, state_dir)

    if action == "status":
        try:
            root = rt.root()
        except VaultPermissionDenied as exc:
            return {"configured": False, "enabled": True, "permissionDenied": True, "detail": str(exc)}
        except VaultNotConfigured as exc:
            return {"configured": False, "enabled": True, "detail": str(exc)}
        return {"configured": True, "enabled": True, "vaultPath": str(root)}

    if action == "search":
        query = (params.get("query") or "").strip()
        if not query:
            raise FeatureRpcError("INVALID", "query required")
        try:
            rt.root()
        except VaultPermissionDenied as exc:
            raise FeatureRpcError("PERMISSION_DENIED", str(exc))
        except VaultNotConfigured as exc:
            raise FeatureRpcError("NOT_CONFIGURED", str(exc))
        try:
            n = int(params.get("max_results") or params.get("maxResults") or 6)
        except (TypeError, ValueError):
            n = 6
        n = max(1, min(n, 20))
        return {"results": rt.index().search(query, max_results=n)}

    raise FeatureRpcError("INVALID", f"unknown obsidian action: {action}")


def memory_gov(action: str, params: dict) -> dict:
    """Memory governance — review queue, stats, and the accept/reject/correct/
    feedback mutations. Reuses the same MemoryGovernance the local TUI/CLI use.
    """
    mg = _open_memory_gov()
    if action == "list":
        # Default to ACTIVE: this is the "your memories" list. Without a filter
        # list_items returns every status (incl. rejected/superseded), which made
        # a deleted (rejected) item linger in the panel — "delete didn't work".
        # The review queue (needs_review) is fetched separately via memory.review.
        return {"items": [i.to_dict() for i in mg.list_items(status=params.get("status") or "active")]}
    if action == "review":
        return {"items": [i.to_dict() for i in mg.review_queue()]}
    if action == "stats":
        s = mg.stats()
        if isinstance(s, dict) and "needs_review" not in s and "review_queue" in s:
            s = {**s, "needs_review": s.get("review_queue", 0)}
        return {"stats": s}
    if action in ("accept", "reject"):
        item_id = params.get("id", "")
        if not item_id:
            raise FeatureRpcError("INVALID", "id required")
        item = getattr(mg, action)(item_id)
        return {"item": item.to_dict() if item else None}
    if action == "correct":
        item_id = params.get("id", "")
        text = params.get("text", "")
        if not item_id or not text:
            raise FeatureRpcError("INVALID", "id and text required")
        item = mg.correct(item_id, text, confidence=params.get("confidence"))
        return {"item": item.to_dict() if item else None}
    if action == "feedback":
        item_id = params.get("id", "")
        if not item_id:
            raise FeatureRpcError("INVALID", "id required")
        item = mg.ingest_feedback(
            item_id, bool(params.get("helpful", False)), params.get("note", ""),
        )
        return {"item": item.to_dict() if item else None}
    raise FeatureRpcError("INVALID", f"unknown memory action: {action}")


def _consolidate_run(dry_run: bool) -> dict:
    """Blocking LLM-propose + apply memory cleanup. Mirrors
    ``flowly memory consolidate``: resolve the active provider, stream a
    proposal (streaming dodges the Flowly proxy's 504 on long completions),
    then apply merge/supersede/stale through governance. Runs in a worker
    thread (see :func:`memory_consolidate`) so the inner ``asyncio.run`` has no
    running loop to clash with."""
    import asyncio
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.memory.consolidate import PROMPT, Consolidator, parse_operations
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.coordinator import MemoryGovernance
    from flowly.memory.kg_mirror import SqliteKGMirror
    from flowly.agent.memory import MemoryStore

    config = load_config()
    ap = resolve_active_provider(config)
    if ap is None:
        raise FeatureRpcError("NO_PROVIDER", "No LLM provider configured")
    model = config.agents.defaults.model

    kg_path = state_db("knowledge_graph.sqlite3")

    def _kg_summary() -> str:
        if not kg_path.exists():
            return ""
        try:
            from flowly.memory.knowledge_graph import KnowledgeGraph
            return KnowledgeGraph(str(kg_path)).summary(max_entities=20)
        except Exception:
            return ""

    def _propose(ctx: dict):
        from flowly.providers.factory import build_provider
        provider = build_provider(ap, default_model=model, config=config)
        prompt = PROMPT.replace("{context}", json.dumps(ctx, ensure_ascii=False, indent=2))

        async def _stream() -> str:
            parts: list[str] = []
            async for delta in provider.chat_stream(
                [{"role": "user", "content": prompt}],
                model=model, max_tokens=2048, temperature=0.1,
            ):
                if delta.content:
                    parts.append(delta.content)
            return "".join(parts)

        return parse_operations(asyncio.run(_stream()))

    gov = GovernanceStore(state_db("memory_governance.sqlite3"))
    mirror = SqliteKGMirror(str(kg_path)) if kg_path.exists() else None
    consolidator = Consolidator(
        gov, _propose, kg_mirror=mirror,
        memory_store=MemoryStore(workspace_dir()), kg_summary_fn=_kg_summary,
    )
    ops, res = consolidator.run(dry_run=dry_run)
    if dry_run:
        output = f"{len(ops)} proposed (dry-run — nothing applied)"
    elif ops:
        output = f"merged={res.merged} superseded={res.superseded} staled={res.staled} skipped={res.skipped}"
    else:
        output = "Nothing to consolidate."
    return {
        "ok": True,
        "output": output,
        "dryRun": dry_run,
        "operations": [
            {"op": o.op, "itemId": o.item_id, "intoId": o.into_id, "reason": o.reason}
            for o in ops
        ],
    }


async def memory_consolidate(params: dict) -> dict:
    """``memory.consolidate`` — LLM-propose + apply memory cleanup over either
    transport (the desktop's "Clean now"). Offloaded to a thread so the event
    loop isn't blocked by the LLM round-trip."""
    import asyncio
    dry_run = bool(params.get("dryRun") or params.get("dry_run"))
    return await asyncio.to_thread(_consolidate_run, dry_run)


def _dream_run(max_messages: int) -> dict:
    """One on-demand cross-session dreaming pass — the desktop/iOS "Learn from
    chats" action. Builds the dreamer with the active provider's streaming
    extractor and runs it synchronously on this worker thread.

    Safe to run alongside the live agent's automatic triggers (idle/daily/turn):
    the dreamer holds an advisory lock + a watermark, so a concurrent run returns
    ``locked`` and an already-processed delta returns ``no_delta`` — both cheap
    no-ops. ``loop=None`` tells the extractor to drive its own ``asyncio.run``
    here (there is no live event loop on this thread), mirroring how
    ``memory_consolidate`` streams its proposal."""
    from flowly.agent.memory import MemoryStore
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.memory.coordinator import MemoryGovernance
    from flowly.memory.dreamer import (
        MemoryDreamerService,
        SessionIndexDeltaSource,
        read_user_profile,
    )
    from flowly.memory.extractor import SubagentExtractor
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.kg_mirror import SqliteKGMirror
    from flowly.providers.factory import build_provider

    config = load_config()
    ap = resolve_active_provider(config)
    if ap is None:
        raise FeatureRpcError("NO_PROVIDER", "No LLM provider configured")
    model = config.agents.defaults.model
    provider = build_provider(ap, default_model=model, config=config)

    md = getattr(config.agents.defaults, "memory_dreaming", None)
    auto_floor = float(getattr(md, "auto_floor", 0.80)) if md is not None else 0.80
    review_floor = float(getattr(md, "review_floor", 0.55)) if md is not None else 0.55

    si_path = str(state_db("session_index.sqlite"))
    kg_path = state_db("knowledge_graph.sqlite3")
    ws = workspace_dir()
    gov = GovernanceStore(state_db("memory_governance.sqlite3"))
    coordinator = MemoryGovernance(gov, memory_store=MemoryStore(ws))
    extractor = SubagentExtractor(provider=provider, model=model, loop=None)
    dreamer = MemoryDreamerService(
        gov,
        SessionIndexDeltaSource(si_path),
        extractor,
        auto_floor=auto_floor,
        review_floor=review_floor,
        calibrate=True,
        kg_mirror=SqliteKGMirror(str(kg_path)) if kg_path.exists() else None,
        on_committed=coordinator.refresh,
        profile_fn=lambda: read_user_profile(ws),
    )
    res = dreamer.run(max_messages=max_messages)

    if not res.ran:
        output = (
            "Already learning — try again in a moment."
            if res.reason == "locked"
            else "Nothing new to learn."
        )
    elif res.reason == "no_delta":
        output = "Nothing new to learn since the last pass."
    elif res.candidates == 0:
        output = f"Read {res.processed_messages} messages — nothing durable to remember."
    else:
        parts = [f"{res.activated} added"]
        if res.needs_review:
            parts.append(f"{res.needs_review} for review")
        if res.superseded:
            parts.append(f"{res.superseded} updated")
        output = f"Learned from {res.processed_messages} messages: " + ", ".join(parts) + "."
    return {
        "ok": True,
        "output": output,
        "ran": res.ran,
        "reason": res.reason,
        "processed": res.processed_messages,
        "candidates": res.candidates,
        "activated": res.activated,
        "needsReview": res.needs_review,
        "superseded": res.superseded,
        "watermark": res.watermark,
    }


async def memory_dream(params: dict) -> dict:
    """``memory.dream`` — run one cross-session dreaming pass on demand (the
    desktop/iOS "Learn from chats" button). Offloaded to a thread so the LLM
    round-trip doesn't block the event loop."""
    import asyncio
    try:
        max_messages = int(params.get("maxMessages") or params.get("max_messages") or 500)
    except (TypeError, ValueError):
        max_messages = 500
    max_messages = max(1, min(max_messages, 5000))
    return await asyncio.to_thread(_dream_run, max_messages)


def memory_import_prompt(params: dict) -> dict:
    """Return the prompt a client can show for ChatGPT/Gemini export."""
    from flowly.memory.importer import memory_export_prompt, normalize_source

    source = params.get("source") or "chatgpt"
    try:
        normalized = normalize_source(str(source))
        prompt = memory_export_prompt(normalized)
    except ValueError as exc:
        raise FeatureRpcError("INVALID", str(exc))
    return {"source": normalized, "prompt": prompt}


def _import_run(params: dict) -> dict:
    """Blocking external memory import. Mirrors ``flowly memory import`` and
    runs in a worker thread because it makes an LLM round-trip."""
    from flowly.agent.memory import MemoryStore
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.memory.coordinator import MemoryGovernance
    from flowly.memory.dreamer import read_user_profile
    from flowly.memory.governance import GovernanceStore
    from flowly.memory.importer import normalize_source, run_import
    from flowly.providers.factory import build_provider

    text = params.get("text") or params.get("dump") or ""
    if not isinstance(text, str) or not text.strip():
        raise FeatureRpcError("INVALID", "text required")
    source = params.get("source") or "chatgpt"
    try:
        source = normalize_source(str(source))
    except ValueError as exc:
        raise FeatureRpcError("INVALID", str(exc))

    config = load_config()
    ap = resolve_active_provider(config)
    if ap is None:
        raise FeatureRpcError("NO_PROVIDER", "No LLM provider configured")
    model = config.agents.defaults.model
    provider = build_provider(ap, default_model=model, config=config)

    ws = workspace_dir()
    gov = GovernanceStore(state_db("memory_governance.sqlite3"))
    coordinator = MemoryGovernance(gov, memory_store=MemoryStore(ws))
    res = run_import(
        gov,
        provider=provider,
        model=model,
        text=text,
        source=source,
        force=bool(params.get("force", False)),
        on_committed=coordinator.refresh,
        profile_fn=lambda: read_user_profile(ws),
    )

    if not res.ran and res.reason == "already_imported":
        output = "This exact memory dump was already imported."
    elif not res.ran:
        output = f"Import skipped: {res.reason}."
    elif res.candidates == 0:
        output = "No durable memories were found in the import."
    else:
        parts = [f"{res.needs_review} for review"]
        if res.duplicates:
            parts.append(f"{res.duplicates} duplicates")
        if res.rejected:
            parts.append(f"{res.rejected} rejected")
        output = f"Imported {res.candidates} candidates: " + ", ".join(parts) + "."
    return {"ok": True, "output": output, **res.to_dict()}


async def memory_import(params: dict) -> dict:
    """``memory.import`` — import a ChatGPT/Gemini memory dump into review."""
    import asyncio

    return await asyncio.to_thread(_import_run, params)


# ── Persona / Provider ──────────────────────────────────────────────────────

def persona_list() -> dict:
    """Workspace personas + the active one (``config.agents.defaults.persona``)."""
    from flowly.config.loader import get_config_path

    active = "default"
    try:
        cfg_path = get_config_path()
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            active = (((cfg.get("agents") or {}).get("defaults") or {}).get("persona")) or "default"
    except Exception:
        pass

    items = []
    personas_dir = workspace_dir() / "personas"
    if personas_dir.exists():
        for md in sorted(personas_dir.glob("*.md")):
            desc = ""
            try:
                for line in md.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        desc = s[:80]
                        break
            except Exception:
                pass
            items.append({"name": md.stem, "active": md.stem == active, "description": desc})
    return {"personas": items, "active": active}


# Coroutine the host (the gateway) registers so a model/provider change can be
# applied LIVE — swapping the running agent's provider + model without a restart.
# Unset (e.g. a transport that doesn't wire it) → model_set falls back to
# signalling a restart instead.
_provider_reload_cb = None


def set_provider_reload_callback(cb) -> None:
    """Register an async ``() -> dict`` the host calls to live-reload the active
    provider after a config change (the gateway points this at on_provider_reload)."""
    global _provider_reload_cb
    _provider_reload_cb = cb


# Coroutine the host registers so a tools.codex_session change (approval policy
# / sandbox) can be applied LIVE — re-reading config, dropping the warm Codex
# subprocesses that captured the old policy at spawn, and re-registering the
# tool (which rewrites ~/.codex/config.toml). Unset → codex_policy_set falls
# back to signalling a restart.
_codex_reload_cb = None


def set_codex_reload_callback(cb) -> None:
    """Register an async ``() -> dict`` the host calls to live-reload the
    codex_session runtime after a policy change (the gateway points this at
    on_codex_reload)."""
    global _codex_reload_cb
    _codex_reload_cb = cb


# Returns ``(board_store, board_orchestrator)`` — the agent's single-writer
# board. Wired at startup so board.snapshot/action work over relay AND gateway
# (the gateway also has its own direct handlers; this lights up the relay).
_board_provider = None


def set_board_provider(provider) -> None:
    """Register a ``() -> (store, orchestrator)`` accessor for the agent board."""
    global _board_provider
    _board_provider = provider


def _board() -> tuple:
    if _board_provider is None:
        return (None, None)
    try:
        return _board_provider()
    except Exception:
        return (None, None)


# Returns the agent's SubagentRegistry — read-only, for the board task-detail
# audit view (a card's run timing + per-tool-call trace). Wired at startup.
_registry_provider = None


def set_registry_provider(provider) -> None:
    """Register a ``() -> SubagentRegistry`` accessor for run/tool-trace audit."""
    global _registry_provider
    _registry_provider = provider


def _registry():
    if _registry_provider is None:
        return None
    try:
        return _registry_provider()
    except Exception:
        return None


# Returns the agent's ``SubagentManager`` — used by ``subagents.spawn`` to
# launch a manual background subagent whose async result is announced back to
# the origin session. Wired at startup (gateway only; the relay has no live
# manager). ``None`` ⇒ manual spawn is unavailable on this transport.
_subagent_manager_provider = None


def set_subagent_manager_provider(provider) -> None:
    """Register a ``() -> SubagentManager`` accessor for manual spawn."""
    global _subagent_manager_provider
    _subagent_manager_provider = provider


def _subagent_manager():
    if _subagent_manager_provider is None:
        return None
    try:
        return _subagent_manager_provider()
    except Exception:
        return None


# Returns the agent's ``CronService``. Wired at startup so cron.* RPCs work over
# relay AND gateway — additive; the relay's existing Firestore/web-channel cron
# path is untouched.
_cron_provider = None


def set_cron_provider(provider) -> None:
    """Register a ``() -> CronService`` accessor for the scheduler."""
    global _cron_provider
    _cron_provider = provider


def _cron():
    if _cron_provider is None:
        return None
    try:
        return _cron_provider()
    except Exception:
        return None


# Flowlet wiring. Two callbacks the host (gateway) registers at startup:
#   * broadcast — push ``flowlet.*`` events to desktop (gateway) + relay (web),
#     so ``flowlets.action`` and ``flowlets.delete`` fan out to every client.
#   * agent runner — run an agent turn for a flowlet ``agent`` action (a
#     "analyze my week" button). ``None`` ⇒ that op reports UNAVAILABLE.
_flowlet_broadcast_cb = None
_flowlet_agent_runner_cb = None
#   * watch hook — evaluate a flowlet's reactive watches right after a tap
#     mutates its state, so a goal celebration / threshold nudge fires
#     instantly instead of waiting for the next heartbeat. ``None`` ⇒ the
#     heartbeat (up to 60s later) is the only trigger.
_flowlet_watch_hook_cb = None


def set_flowlet_broadcast(cb) -> None:
    """Register an async ``(event_name, data) -> None`` used to fan flowlet
    state changes out to all connected clients."""
    global _flowlet_broadcast_cb
    _flowlet_broadcast_cb = cb


def set_flowlet_agent_runner(cb) -> None:
    """Register an async ``(flowlet, message) -> None`` that runs an agent turn
    for a flowlet ``agent`` action."""
    global _flowlet_agent_runner_cb
    _flowlet_agent_runner_cb = cb


def set_flowlet_watch_hook(cb) -> None:
    """Register an async ``(flowlet_id) -> None`` that evaluates a flowlet's
    watches immediately after a client tap changes its state."""
    global _flowlet_watch_hook_cb
    _flowlet_watch_hook_cb = cb


#   * refresh hook — resolve a flowlet's live data sources on demand (a client
#     opened the screen → due sources; tapped refresh → force all).
_flowlet_refresh_cb = None


def set_flowlet_refresh_hook(cb) -> None:
    """Register an async ``(flowlet_id, force: bool) -> int`` that refreshes a
    flowlet's data sources."""
    global _flowlet_refresh_cb
    _flowlet_refresh_cb = cb


_flowlet_vision_runner_cb = None


def set_flowlet_vision_runner(cb) -> None:
    """Register an async ``(flowlet, prompt, image_data_uri) -> str`` — a single
    isolated model turn over a captured photo (see flowlets.capture)."""
    global _flowlet_vision_runner_cb
    _flowlet_vision_runner_cb = cb


def provider_active() -> dict:
    """Resolved active LLM provider (no secrets) + the current default model —
    for the Settings display and the model picker's current selection."""
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    cfg = load_config()
    active = resolve_active_provider(cfg)
    return {
        "provider": None if active is None else {
            "key": active.key,
            "source": active.source,
            "apiBase": getattr(active, "api_base", None),
            # api_key deliberately omitted — never send the secret.
        },
        "model": cfg.agents.defaults.model or "",
    }


async def model_list(params: dict) -> dict:
    """Live model catalogue for the active provider — for the desktop picker.
    Empty ``models`` ⇒ provider has no fetcher / network failed; the client
    falls back to free-text entry."""
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    from flowly.integrations.model_catalog import fetch_models
    active = resolve_active_provider(load_config())
    if active is None:
        return {"provider": None, "models": []}
    models = await fetch_models(active.key, force_refresh=bool(params.get("forceRefresh")))
    return {
        "provider": active.key,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "contextWindow": m.context_window,
                "tags": list(m.tags or []),
            }
            for m in models
        ],
    }


async def model_set(params: dict) -> dict:
    """Set the bot's default model (``agents.defaults.model``) and apply it.

    Live-reloads the running provider when the host wired
    :func:`set_provider_reload_callback` (gateway); otherwise returns
    ``willRestart`` so the transport restarts the bot to pick up the change.
    """
    model = params.get("model")
    if not isinstance(model, str) or not model.strip():
        raise FeatureRpcError("INVALID", "model must be a non-empty string")
    model = model.strip()
    from flowly.config.loader import load_config, save_config
    cfg = load_config()
    cfg.agents.defaults.model = model
    save_config(cfg)
    if _provider_reload_cb is not None:
        try:
            await _provider_reload_cb()
            return {"ok": True, "model": model, "willRestart": False}
        except Exception:
            # Live reload failed — fall back to a restart so the change still lands.
            pass
    return {"ok": True, "model": model, "willRestart": True}


# ── Exec approval policy ─────────────────────────────────────────────────────
# The standing shell/exec approval policy lives in its OWN store
# (~/.flowly/credentials/exec-approvals.json), not config.json — the running
# executor reads that store and picks up edits live via
# ``ExecApprovalStore.refresh_if_changed()``. So these RPCs NEVER need a gateway
# restart, and (unlike a ``config.set`` on ``tools.exec``, which the executor
# ignores) they actually take effect. Served over BOTH transports so
# Desktop-direct and iOS-over-relay share one shape.

_EXEC_SECURITY = ("deny", "allowlist", "full")
_EXEC_ASK = ("off", "on-miss", "always")


def _exec_policy_payload(store) -> dict:
    cfg = store.config
    return {
        "security": cfg.security,
        "ask": cfg.ask,
        "allowlist": [
            {
                "pattern": e.pattern,
                "command": e.last_used_command,
                "lastUsedAt": e.last_used_at,
            }
            for e in cfg.allowlist
        ],
    }


def exec_policy_get() -> dict:
    """Standing exec approval policy (security/ask + allowlist patterns)."""
    from flowly.exec.approvals import ExecApprovalStore
    store = ExecApprovalStore()
    store.load()
    return _exec_policy_payload(store)


def exec_policy_set(params: dict) -> dict:
    """Set the standing exec security/ask policy (and optionally replace the
    allowlist).

    Writes the approvals store; the long-lived executor reloads it on its next
    command (mtime check), so this never returns ``willRestart``. ``allowlist``,
    when given, is the FULL desired list (each item a ``{"pattern": str}`` or a
    bare string) and replaces the stored one — this matches how a settings
    screen manages the list, and (unlike a local file write) works whether the
    bot is local or reached over the relay.
    """
    security = params.get("security")
    ask = params.get("ask")
    allowlist = params.get("allowlist")
    if security is not None and security not in _EXEC_SECURITY:
        raise FeatureRpcError("INVALID", "Invalid security")
    if ask is not None and ask not in _EXEC_ASK:
        raise FeatureRpcError("INVALID", "Invalid ask")
    if allowlist is not None and not isinstance(allowlist, list):
        raise FeatureRpcError("INVALID", "allowlist must be a list")
    if security is None and ask is None and allowlist is None:
        raise FeatureRpcError("INVALID", "Nothing to set")
    from flowly.exec.approvals import ExecApprovalStore
    from flowly.exec.types import AllowlistEntry
    store = ExecApprovalStore()
    cfg = store.load()
    if security is not None:
        cfg.security = security
    if ask is not None:
        cfg.ask = ask
    if allowlist is not None:
        entries = []
        for item in allowlist:
            pattern = item.get("pattern") if isinstance(item, dict) else item
            if not isinstance(pattern, str) or not pattern.strip():
                raise FeatureRpcError("INVALID", "allowlist entries need a pattern")
            entries.append(AllowlistEntry(pattern=pattern.strip()))
        cfg.allowlist = entries
    store.save()
    return _exec_policy_payload(store)


def exec_policy_allowlist_remove(params: dict) -> dict:
    """Drop a pattern from the exec allowlist."""
    pattern = params.get("pattern") or ""
    if not pattern:
        raise FeatureRpcError("INVALID", "Missing pattern")
    from flowly.exec.approvals import ExecApprovalStore
    store = ExecApprovalStore()
    store.load()
    removed = store.remove_from_allowlist(pattern)
    payload = _exec_policy_payload(store)
    payload["removed"] = removed
    return payload


# ── Codex session policy ─────────────────────────────────────────────────────
# tools.codex_session.approvalPolicy / sandbox. Unlike exec policy these live in
# config.json, but a plain config.set is not enough: the warm Codex subprocess
# captured the sandbox at spawn and the approval policy only reaches Codex via
# ~/.codex/config.toml. So codex_policy_set writes config, then live-reloads via
# the host callback (drop warm sessions + re-register the tool → rewrite
# config.toml). Reload success ⇒ no restart; missing/failed callback ⇒ restart.

_CODEX_APPROVAL = ("on-request", "never", "auto-review", "granular")
_CODEX_SANDBOX = ("read-only", "workspace-write", "full-access")


def codex_policy_get() -> dict:
    """Current codex_session approval policy + sandbox (for the settings UI)."""
    from flowly.config.loader import load_config
    cs = load_config().tools.codex_session
    return {
        "enabled": cs.enabled,
        "sandbox": cs.sandbox,
        "approvalPolicy": cs.approval_policy,
        "exposeFlowlyTools": cs.expose_flowly_tools,
    }


async def codex_policy_set(params: dict) -> dict:
    """Set tools.codex_session.approvalPolicy / sandbox and apply it live.

    Live-reloads the running codex_session runtime when the host wired
    :func:`set_codex_reload_callback` (gateway); otherwise (or if the reload
    raises) returns ``willRestart`` so the transport restarts the bot to pick
    up the change.
    """
    approval = params.get("approvalPolicy")
    sandbox = params.get("sandbox")
    if approval is not None and approval not in _CODEX_APPROVAL:
        raise FeatureRpcError("INVALID", "Invalid approvalPolicy")
    if sandbox is not None and sandbox not in _CODEX_SANDBOX:
        raise FeatureRpcError("INVALID", "Invalid sandbox")
    if approval is None and sandbox is None:
        raise FeatureRpcError("INVALID", "Nothing to set")

    from flowly.config.loader import load_config, save_config
    cfg = load_config()
    if approval is not None:
        cfg.tools.codex_session.approval_policy = approval
    if sandbox is not None:
        cfg.tools.codex_session.sandbox = sandbox
    save_config(cfg)

    if _codex_reload_cb is not None:
        try:
            status = await _codex_reload_cb()
            result = {"ok": True, "willRestart": False}
            if isinstance(status, dict):
                result.update(status)
                result["willRestart"] = False
            return result
        except Exception:
            # Live reload failed — fall back to a restart so the change lands.
            pass
    return {"ok": True, "willRestart": True}


# ── Providers (BYOK) ─────────────────────────────────────────────────────────

# Display metadata for the desktop's BYOK panel. ``keyable`` = takes a raw API
# key (the rest are account/OAuth based). Slugs MUST match ProvidersConfig.
# Labels match the corresponding IntegrationCard in integrations/registry.py.
_PROVIDER_SLOTS = (
    ("flowly", "Flowly Account", False),
    ("anthropic", "Anthropic", True),
    ("openai", "OpenAI", True),
    ("openai_codex", "ChatGPT subscription", False),
    ("openrouter", "OpenRouter", True),
    ("gemini", "Google Gemini", True),
    ("groq", "Groq", True),
    ("xai", "xAI (Grok API)", True),
    ("xai_oauth", "xAI Grok OAuth", False),
    ("zai_coding", "Z.AI GLM Coding Plan", True),
    ("zhipu", "Zhipu GLM", True),
    ("sakana", "Sakana Fugu", True),
    ("vllm", "vLLM (self-hosted)", True),
)


def provider_list() -> dict:
    """Every configurable LLM provider with status — **no secrets**. The desktop
    BYOK panel reads this instead of ``config.get`` so raw API keys never cross
    the wire: we report only presence (``hasKey``), the explicit sticky
    ``active`` choice, and the resolved provider (key + human-readable source)."""
    from flowly.config.loader import load_config
    from flowly.integrations.active_provider import resolve_active_provider
    cfg = load_config()
    resolved = resolve_active_provider(cfg)
    explicit = (cfg.providers.active or "").strip()
    providers = []
    for key, name, keyable in _PROVIDER_SLOTS:
        slot = getattr(cfg.providers, key, None)
        if key == "flowly":
            # "Configured" = a Desktop-pushed account credential is present
            # (account_key, or the legacy serverId+authToken pair).
            fl = cfg.providers.flowly
            has_key = bool(
                (getattr(fl, "account_key", "") or "").strip()
                or ((getattr(fl, "server_id", "") or "").strip() and (getattr(fl, "auth_token", "") or "").strip())
            )
        elif key == "zai_coding":
            try:
                from flowly.auth.zai_coding import resolve_runtime_credentials
                has_key = resolve_runtime_credentials(config=cfg) is not None
            except Exception:
                has_key = False
        elif key == "xai_oauth":
            try:
                from flowly.auth.xai_oauth import resolve_runtime_credentials
                has_key = resolve_runtime_credentials(config=cfg) is not None
            except Exception:
                has_key = False
        elif key == "openai_codex":
            try:
                from flowly.auth.openai_codex import resolve_runtime_credentials
                has_key = resolve_runtime_credentials(config=cfg) is not None
            except Exception:
                has_key = False
        else:
            has_key = bool(getattr(slot, "api_key", "")) if keyable else True
        providers.append({
            "key": key,
            "name": name,
            "keyable": keyable,
            "hasKey": has_key,
            "apiBase": getattr(slot, "api_base", None),
            "isActive": resolved is not None and resolved.key == key,
        })
    return {
        "providers": providers,
        "active": explicit or None,
        "resolved": None if resolved is None else {"key": resolved.key, "source": resolved.source},
    }


async def provider_set(params: dict) -> dict:
    """Switch the explicit active provider (``providers.active``). Empty string
    clears it (back to the cascade). Live-reloads the running provider when the
    host wired :func:`set_provider_reload_callback`; otherwise ``willRestart``."""
    key = params.get("key")
    if not isinstance(key, str):
        raise FeatureRpcError("INVALID", "key must be a string")
    key = key.strip()
    from flowly.integrations.active_provider import set_active_provider
    try:
        # Also auto-fixes agents.defaults.model when the new provider can't
        # serve the current one (returns the applied model, else None).
        model_changed = set_active_provider(key)
    except ValueError as exc:
        raise FeatureRpcError("INVALID", str(exc))
    from flowly.integrations import model_catalog
    model_catalog.flush_cache()
    if _provider_reload_cb is not None:
        try:
            await _provider_reload_cb()
            return {"ok": True, "key": key, "model": model_changed, "willRestart": False}
        except Exception:
            pass
    return {"ok": True, "key": key, "model": model_changed, "willRestart": True}


async def provider_set_key(params: dict) -> dict:
    """Set or clear a provider's BYOK API key (``value=""`` clears it). Never
    echoes the key back — returns presence only. Flushes the model catalogue
    cache (so the picker refetches with the new credential) and live-reloads."""
    key = params.get("key")
    value = params.get("value", "")
    if not isinstance(key, str) or not key.strip():
        raise FeatureRpcError("INVALID", "key must be a non-empty string")
    if not isinstance(value, str):
        raise FeatureRpcError("INVALID", "value must be a string")
    key = key.strip()
    value = value.strip()
    if key == "zai_coding":
        from flowly.auth import zai_coding
        from flowly.config.loader import load_config, save_config
        cfg = load_config()
        cfg.providers.zai_coding.enabled = True
        cfg.providers.zai_coding.api_base = zai_coding.DEFAULT_ZAI_CODING_BASE_URL
        save_config(cfg)
        if value:
            zai_coding.save_api_key(value)
        else:
            zai_coding.clear_token_payload()
        from flowly.integrations import model_catalog
        model_catalog.flush_cache()
        has_key = zai_coding.resolve_runtime_credentials(config=load_config()) is not None
        if _provider_reload_cb is not None:
            try:
                await _provider_reload_cb()
                return {"ok": True, "key": key, "hasKey": has_key, "willRestart": False}
            except Exception:
                pass
        return {"ok": True, "key": key, "hasKey": has_key, "willRestart": True}
    from flowly.config.loader import load_config, save_config
    cfg = load_config()
    slot = getattr(cfg.providers, key, None)
    if slot is None or not hasattr(slot, "api_key"):
        raise FeatureRpcError("INVALID", f"provider '{key}' has no API key slot")
    slot.api_key = value
    save_config(cfg)
    from flowly.integrations import model_catalog
    model_catalog.flush_cache()
    if _provider_reload_cb is not None:
        try:
            await _provider_reload_cb()
            return {"ok": True, "key": key, "hasKey": bool(value), "willRestart": False}
        except Exception:
            pass
    return {"ok": True, "key": key, "hasKey": bool(value), "willRestart": True}


async def provider_set_flowly_account(params: dict) -> dict:
    """Persist a Flowly account credential pushed by the Desktop app — the ONLY
    minter. Enables the ``flowly`` hosted provider (billed to the account) as
    the LLM-proxy bearer, WITHOUT joining the relay (``channels.web`` is left
    untouched, so a gateway bot stays a pure gateway).

    Accepts an ``accountKey`` (an ``flw_…`` account API key — the canonical,
    server-record-free path) and/or a legacy ``serverId`` + ``authToken`` pair.
    Pass empty strings to clear. Never echoes the credential back."""
    account_key = params.get("accountKey", "")
    server_id = params.get("serverId", "")
    auth_token = params.get("authToken", "")
    if not all(isinstance(v, str) for v in (account_key, server_id, auth_token)):
        raise FeatureRpcError("INVALID", "credentials must be strings")
    account_key = account_key.strip()
    server_id = server_id.strip()
    auth_token = auth_token.strip()
    from flowly.config.loader import load_config, save_config
    cfg = load_config()
    cfg.providers.flowly.account_key = account_key
    cfg.providers.flowly.server_id = server_id
    cfg.providers.flowly.auth_token = auth_token
    save_config(cfg)
    from flowly.integrations import model_catalog
    model_catalog.flush_cache()
    has = bool(account_key or (server_id and auth_token))
    if _provider_reload_cb is not None:
        try:
            await _provider_reload_cb()
            return {"ok": True, "hasCredential": has, "willRestart": False}
        except Exception:
            pass
    return {"ok": True, "hasCredential": has, "willRestart": True}


# ── Artifacts ────────────────────────────────────────────────────────────────
# Same store + same filtering the gateway's WS handlers use, exposed through
# feature_rpc so RELAY-connected desktops get the Artifacts tab too (the direct
# gateway already serves artifacts.* on its own WS dispatch — these handlers
# make the surface transport-agnostic).

def _artifact_store():
    from flowly.artifacts.store import get_store
    return get_store()


def artifacts_list(params: dict) -> dict:
    """Artifact summaries (no content). Mirrors the gateway WS handler:
    over-fetch then drop internal/context artifacts so the visible page
    doesn't shrink below the caller's limit."""
    from flowly.artifacts.context import is_internal_context_artifact
    from flowly.artifacts.summary import artifact_summary
    store = _artifact_store()
    limit = max(1, min(int(params.get("limit", 50) or 50), 200))
    include_internal = bool(params.get("includeInternal", False))
    fetch_limit = limit if include_internal else max(limit * 5, 100)
    results = store.list(
        type=params.get("type"),
        pinned=params.get("pinned"),
        search=params.get("search"),
        session_key=params.get("sessionKey"),
        limit=fetch_limit,
        offset=int(params.get("offset", 0) or 0),
    )
    if not include_internal:
        results = [a for a in results if not is_internal_context_artifact(a)]
    return {"artifacts": [artifact_summary(a) for a in results[:limit]]}


def artifacts_get(params: dict) -> dict:
    """One artifact WITH content. Large content is sliced (offset/limit caps at
    200k chars per call) so a multi-MB artifact can't blow the relay frame."""
    artifact_id = str(params.get("id", "") or "")
    if not artifact_id:
        raise FeatureRpcError("INVALID", "id required")
    artifact = _artifact_store().get(artifact_id)
    if not artifact:
        raise FeatureRpcError("NOT_FOUND", "Artifact not found")
    offset = max(0, int(params.get("offset", 0) or 0))
    limit = max(1, min(int(params.get("limit", 200_000) or 200_000), 200_000))
    content = artifact.get("content", "") or ""
    if offset or len(content) > offset + limit:
        artifact = {
            **artifact,
            "content": content[offset:offset + limit],
            "content_range": {
                "offset": offset,
                "limit": limit,
                "total": len(content),
                "has_more": len(content) > offset + limit,
            },
        }
    return {"artifact": artifact}


def artifacts_update(params: dict) -> dict:
    """Edit title/content/tags/size — the desktop modal's save path."""
    artifact_id = str(params.get("id", "") or "")
    if not artifact_id:
        raise FeatureRpcError("INVALID", "id required")
    artifact = _artifact_store().update(
        artifact_id,
        title=params.get("title"),
        content=params.get("content"),
        metadata=params.get("metadata"),
        data_bindings=params.get("dataBindings"),
        pinned=params.get("pinned"),
        dashboard_size=params.get("dashboardSize"),
        tags=params.get("tags"),
    )
    if not artifact:
        raise FeatureRpcError("NOT_FOUND", "Artifact not found")
    return {"artifact": artifact}


def artifacts_delete(params: dict) -> dict:
    artifact_id = str(params.get("id", "") or "")
    if not artifact_id:
        raise FeatureRpcError("INVALID", "id required")
    return {"ok": _artifact_store().delete(artifact_id)}


def artifacts_pin(params: dict) -> dict:
    artifact_id = str(params.get("id", "") or "")
    if not artifact_id:
        raise FeatureRpcError("INVALID", "id required")
    artifact = _artifact_store().pin(artifact_id, bool(params.get("pinned", True)))
    if not artifact:
        raise FeatureRpcError("NOT_FOUND", "Artifact not found")
    return {"artifact": artifact}


def artifacts_versions(params: dict) -> dict:
    artifact_id = str(params.get("id", "") or "")
    if not artifact_id:
        raise FeatureRpcError("INVALID", "id required")
    return {"versions": _artifact_store().get_versions(artifact_id)}


# ── Flowlets ─────────────────────────────────────────────────────────────────
# Agent-generated dynamic mini-screens. Read + interact over BOTH transports.
# Creation/definition edits are agent-only (via the flowlet tool); the client
# surface is list / get / state / action / pin / delete — the same shape the
# artifact surface uses (no client-side authoring). `flowlets.action` is the
# deterministic tap handler: it never calls the LLM.

def _flowlet_store():
    from flowly.flowlets.store import get_store
    return get_store()


def _flowlet_values(flowlet: dict) -> dict:
    from flowly.flowlets import queries
    from flowly.flowlets.store import get_store, now_ms
    store = get_store()
    return queries.resolve_values(
        flowlet["definition"],
        store.get_state(flowlet["id"]),
        store.get_events(flowlet["id"]),
        now_ms(),
        None,
    )


def _flowlet_summary(flowlet: dict, values: dict | None = None) -> dict:
    s = {
        "id": flowlet["id"],
        "name": flowlet.get("name"),
        "icon": flowlet.get("icon"),
        "accent": flowlet.get("accent"),
        "pinned": flowlet.get("pinned"),
        "version": flowlet.get("version"),
        "catalog": flowlet.get("catalog"),
        "updatedAt": flowlet.get("updated_at"),
    }
    if values is not None:
        s["values"] = values
        from flowly.flowlets.queries import flowlet_preview
        preview = flowlet_preview(flowlet.get("definition") or {}, values)
        if preview is not None:
            s["preview"] = preview
    return s


def flowlets_list(params: dict) -> dict:
    """Flowlet cards with their current live values (so a card can show progress
    without opening the screen). No full definition — that comes from get."""
    store = _flowlet_store()
    limit = max(1, min(int(params.get("limit", 50) or 50), 200))
    out = []
    for f in store.list(limit=limit):
        try:
            out.append(_flowlet_summary(f, _flowlet_values(f)))
        except Exception:
            out.append(_flowlet_summary(f))
    return {"flowlets": out}


def flowlets_get(params: dict) -> dict:
    """One flowlet WITH its definition and current values — what a client needs
    to render the full screen."""
    flowlet_id = str(params.get("id", "") or "")
    if not flowlet_id:
        raise FeatureRpcError("INVALID", "id required")
    flowlet = _flowlet_store().get(flowlet_id)
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", "Flowlet not found")
    # Opening the screen refreshes its due data sources in the background — the
    # fresh values arrive as a `flowlet.state` broadcast a moment later.
    if _flowlet_refresh_cb is not None and (flowlet.get("definition") or {}).get("sources"):
        try:
            import asyncio as _asyncio
            _asyncio.get_running_loop().create_task(_flowlet_refresh_cb(flowlet_id, False))
        except Exception:
            pass  # no loop / best-effort
    # Serving-time guarantees (never persisted): every user-owned list row is
    # EDITABLE (a drill screen with edit inputs — synthesized if the agent
    # authored none), and a list with an `image` field always DISPLAYS its
    # photos (row thumbnail + full photo). Editable runs first so a synthesized
    # drill screen also picks up its full photo from the photo pass.
    from flowly.flowlets.composites import expand_composites
    from flowly.flowlets.normalize import ensure_editable_drill, ensure_photo_display
    # Composites (catalog 3) expand to primitives FIRST, so the photo/edit
    # augmentation and the client both see plain v2 nodes; an old client renders
    # the expansion with no changes.
    definition = ensure_photo_display(
        ensure_editable_drill(expand_composites(flowlet["definition"]))
    )
    return {
        "flowlet": {
            "id": flowlet["id"],
            "name": flowlet["name"],
            "icon": flowlet.get("icon"),
            "accent": flowlet.get("accent"),
            "pinned": flowlet.get("pinned"),
            "version": flowlet.get("version"),
            "catalog": flowlet.get("catalog"),
            "definition": definition,
            "updatedAt": flowlet.get("updated_at"),
        },
        "values": _flowlet_values(flowlet),
    }


async def flowlets_refresh(params: dict) -> dict:
    """Force-refresh a flowlet's live data sources (a pull-to-refresh tap).
    Returns the freshly-resolved values; a `flowlet.state` broadcast also fans
    the update to the other clients."""
    flowlet_id = str(params.get("id", "") or "")
    if not flowlet_id:
        raise FeatureRpcError("INVALID", "id required")
    flowlet = _flowlet_store().get(flowlet_id)
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", "Flowlet not found")
    refreshed = 0
    if _flowlet_refresh_cb is not None:
        try:
            refreshed = await _flowlet_refresh_cb(flowlet_id, True)
        except Exception as exc:
            raise FeatureRpcError("UNAVAILABLE", f"couldn't refresh: {exc}")
    fresh = _flowlet_store().get(flowlet_id) or flowlet  # re-read post-refresh
    return {"id": flowlet_id, "refreshed": refreshed, "values": _flowlet_values(fresh)}


def flowlets_state(params: dict) -> dict:
    """Just the live values — the poll fallback when a client can't get the
    pushed ``flowlet.state`` event."""
    flowlet_id = str(params.get("id", "") or "")
    if not flowlet_id:
        raise FeatureRpcError("INVALID", "id required")
    flowlet = _flowlet_store().get(flowlet_id)
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", "Flowlet not found")
    return {"id": flowlet_id, "values": _flowlet_values(flowlet)}


async def flowlets_action(params: dict) -> dict:
    """Apply a user tap deterministically (no LLM). ``{id, componentId, value?}``.
    Returns the new values; the transport also broadcasts ``flowlet.state`` so
    the OTHER connected clients update too (the caller gets it in this reply)."""
    from flowly.flowlets.actions import FlowletActionError, apply_action
    store = _flowlet_store()
    flowlet_id = str(params.get("id", "") or "")
    component_id = str(params.get("componentId", "") or "")
    if not flowlet_id or not component_id:
        raise FeatureRpcError("INVALID", "id and componentId required")
    # A tapped `agent` op runs a paid model turn — throttle it (every other
    # model path already is). Best-effort component lookup; a component in a
    # drill screen isn't found here and simply isn't throttled (rare).
    from flowly.flowlets.actions import _find_component
    _fl = store.get(flowlet_id)
    _comp = _find_component(_fl.get("definition") or {}, component_id) if _fl else None
    if ((_comp or {}).get("action") or {}).get("op") == "agent" and not _agent_action_rate_ok(flowlet_id):
        raise FeatureRpcError("RATE_LIMITED", "too many requests; try again in a moment")
    try:
        result = await apply_action(
            store, flowlet_id, component_id,
            value=params.get("value"),
            agent_runner=_flowlet_agent_runner_cb,
        )
    except FlowletActionError as exc:
        raise FeatureRpcError(exc.code, exc.message)
    # Recompute the card headline so list tiles update live (not just the open
    # screen) — carry it in both the reply and the broadcast.
    from flowly.flowlets.queries import flowlet_preview
    flowlet = store.get(flowlet_id)
    preview = flowlet_preview(flowlet.get("definition") or {}, result["values"]) if flowlet else None
    if preview is not None:
        result["preview"] = preview
    if _flowlet_broadcast_cb is not None:
        try:
            data = {"id": flowlet_id, "values": result["values"]}
            if preview is not None:
                data["preview"] = preview
            await _flowlet_broadcast_cb("flowlet.state", data)
        except Exception:
            pass
    # Evaluate reactive watches immediately (goal celebration / threshold nudge
    # shouldn't wait for the next heartbeat). Best-effort — never fail the tap.
    if _flowlet_watch_hook_cb is not None:
        try:
            await _flowlet_watch_hook_cb(flowlet_id)
        except Exception:
            pass
    return result


def _decode_capture_image(image: Any, max_bytes: int) -> bytes | None:
    """A ``data:image/…;base64,…`` URI or bare base64 → the raw JPEG bytes.

    Rejects (returns None) BEFORE decoding when the base64 is too long (so a
    hostile frame can't force an oversized transient decode), and after decoding
    when the bytes aren't a JPEG (the only format our clients produce, and what
    ``flowlets.attachment`` serves) — no arbitrary blob can be stored/served.
    """
    if not isinstance(image, str) or not image:
        return None
    b64 = image
    if b64.startswith("data:"):
        comma = b64.find(",")
        if comma == -1:
            return None
        b64 = b64[comma + 1:]
    # base64 is 4 chars per 3 bytes; reject before allocating the decode.
    if len(b64) > (max_bytes * 4) // 3 + 4:
        return None
    try:
        import base64 as _b64
        data = _b64.b64decode(b64, validate=False)
    except Exception:
        return None
    if len(data) > max_bytes or not data.startswith(b"\xff\xd8\xff"):  # JPEG magic
        return None
    return data


# Rolling-window capture rate limit (a vision turn is a paid call). In-process,
# monotonic; single-user bot, so a plain per-flowlet + global list is enough.
_capture_hits: dict[str, list[float]] = {}
_capture_hits_global: list[float] = []

# Same idea for tapped `agent` action ops (also paid model turns).
_agent_action_hits: dict[str, list[float]] = {}
_agent_action_hits_global: list[float] = []


def _agent_action_rate_ok(flowlet_id: str) -> bool:
    import time as _time
    from flowly.flowlets import catalog as _fcat
    now = _time.monotonic()
    cutoff = now - _fcat.AGENT_ACTION_WINDOW_S
    g = [t for t in _agent_action_hits_global if t >= cutoff]
    f = [t for t in _agent_action_hits.get(flowlet_id, []) if t >= cutoff]
    if len(g) >= _fcat.MAX_AGENT_ACTIONS_GLOBAL_PER_WINDOW:
        _agent_action_hits_global[:] = g
        return False
    if len(f) >= _fcat.MAX_AGENT_ACTIONS_PER_FLOWLET_PER_WINDOW:
        _agent_action_hits[flowlet_id] = f
        return False
    g.append(now)
    f.append(now)
    _agent_action_hits_global[:] = g
    _agent_action_hits[flowlet_id] = f
    return True


def _capture_rate_ok(flowlet_id: str) -> bool:
    import time as _time
    from flowly.flowlets import catalog as _fcat
    now = _time.monotonic()
    cutoff = now - _fcat.CAPTURE_WINDOW_S
    g = [t for t in _capture_hits_global if t >= cutoff]
    f = [t for t in _capture_hits.get(flowlet_id, []) if t >= cutoff]
    if len(g) >= _fcat.MAX_CAPTURES_GLOBAL_PER_WINDOW:
        _capture_hits_global[:] = g
        return False
    if len(f) >= _fcat.MAX_CAPTURES_PER_FLOWLET_PER_WINDOW:
        _capture_hits[flowlet_id] = f
        return False
    g.append(now)
    f.append(now)
    _capture_hits_global[:] = g
    _capture_hits[flowlet_id] = f
    return True


async def flowlets_capture(params: dict) -> dict:
    """A `photo` tap: the client sends a downscaled image; the bot interprets it
    (one isolated model turn) into a new list item and broadcasts the update.
    ``{id, componentId, image}`` → ``{id, values, preview?}``."""
    from flowly.flowlets.actions import _find_component
    from flowly.flowlets import catalog as _fcat
    from flowly.flowlets.vision import FlowletCaptureError, apply_capture

    store = _flowlet_store()
    flowlet_id = str(params.get("id", "") or "")
    component_id = str(params.get("componentId", "") or "")
    if not flowlet_id or not component_id:
        raise FeatureRpcError("INVALID", "id and componentId required")
    data = _decode_capture_image(params.get("image"), _fcat.MAX_IMAGE_BYTES)
    if data is None:
        raise FeatureRpcError("INVALID", "image must be a base64 JPEG within the size limit")
    if not _capture_rate_ok(flowlet_id):
        raise FeatureRpcError("RATE_LIMITED", "too many photo captures — try again in a moment")
    flowlet = store.get(flowlet_id)
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", f"flowlet '{flowlet_id}' not found")
    component = _find_component(flowlet.get("definition") or {}, component_id)
    if not component:
        raise FeatureRpcError("NOT_FOUND", f"component '{component_id}' not found")
    try:
        values = await apply_capture(
            store, flowlet, component, data, runner=_flowlet_vision_runner_cb
        )
    except FlowletCaptureError as exc:
        raise FeatureRpcError(exc.code, exc.message)

    from flowly.flowlets.queries import flowlet_preview
    preview = flowlet_preview(flowlet.get("definition") or {}, values)
    result = {"id": flowlet_id, "values": values}
    if preview is not None:
        result["preview"] = preview
    if _flowlet_broadcast_cb is not None:
        try:
            msg = {"id": flowlet_id, "values": values}
            if preview is not None:
                msg["preview"] = preview
            await _flowlet_broadcast_cb("flowlet.state", msg)
        except Exception:
            pass
    return result


async def flowlets_item_remove(params: dict) -> dict:
    """Delete one row from a list — the client's swipe-to-delete. ``{id, source,
    itemId}`` → ``{id, values, preview?}``. A list a data source owns is
    read-only, so a client can't swipe-delete it."""
    from flowly.flowlets.actions import remove_list_item

    store = _flowlet_store()
    flowlet_id = str(params.get("id", "") or "")
    source = str(params.get("source", "") or "")
    item_id = str(params.get("itemId", "") or "")
    if not flowlet_id or not source or not item_id:
        raise FeatureRpcError("INVALID", "id, source and itemId required")
    flowlet = store.get(flowlet_id)
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", f"flowlet '{flowlet_id}' not found")
    defn = flowlet.get("definition") or {}
    spec = (defn.get("state") or {}).get(source)
    if not isinstance(spec, dict) or spec.get("type") != "list":
        raise FeatureRpcError("INVALID", f"'{source}' is not a list")
    if spec.get("source"):
        raise FeatureRpcError("INVALID", f"'{source}' is owned by a data source and is read-only")
    try:
        remove_list_item(store, flowlet_id, defn, source, item_id)
    except Exception as exc:  # noqa: BLE001
        raise FeatureRpcError("INVALID", str(exc))

    from flowly.flowlets.queries import flowlet_preview
    values = _flowlet_values(flowlet)
    preview = flowlet_preview(defn, values)
    result = {"id": flowlet_id, "values": values}
    if preview is not None:
        result["preview"] = preview
    if _flowlet_broadcast_cb is not None:
        try:
            msg = {"id": flowlet_id, "values": values}
            if preview is not None:
                msg["preview"] = preview
            await _flowlet_broadcast_cb("flowlet.state", msg)
        except Exception:
            pass
    return result


def flowlets_attachment(params: dict) -> dict:
    """Serve a stored photo (base64) so remote clients can render an `image`
    field. ``{id, attachmentId}`` → ``{id, mime, data}``."""
    store = _flowlet_store()
    flowlet_id = str(params.get("id", "") or "")
    att_id = str(params.get("attachmentId", "") or "")
    if not flowlet_id or not att_id:
        raise FeatureRpcError("INVALID", "id and attachmentId required")
    # Defense in depth: the store guards the path too, but bind serving to a real
    # flowlet so a client can't probe arbitrary ids.
    if store.get(flowlet_id) is None:
        raise FeatureRpcError("NOT_FOUND", "attachment not found")
    data = store.get_attachment(flowlet_id, att_id)
    if data is None:
        raise FeatureRpcError("NOT_FOUND", "attachment not found")
    import base64 as _b64
    return {"id": att_id, "mime": "image/jpeg", "data": _b64.b64encode(data).decode("ascii")}


def flowlets_pin(params: dict) -> dict:
    flowlet_id = str(params.get("id", "") or "")
    if not flowlet_id:
        raise FeatureRpcError("INVALID", "id required")
    flowlet = _flowlet_store().pin(flowlet_id, bool(params.get("pinned", True)))
    if not flowlet:
        raise FeatureRpcError("NOT_FOUND", "Flowlet not found")
    return {"ok": True, "flowlet": _flowlet_summary(flowlet)}


async def flowlets_delete(params: dict) -> dict:
    flowlet_id = str(params.get("id", "") or "")
    if not flowlet_id:
        raise FeatureRpcError("INVALID", "id required")
    ok = _flowlet_store().delete(flowlet_id)
    if ok and _flowlet_broadcast_cb is not None:
        try:
            await _flowlet_broadcast_cb("flowlet.deleted", {"id": flowlet_id})
        except Exception:
            pass
    return {"ok": ok}


# ── Logs ─────────────────────────────────────────────────────────────────────

def _gateway_log_file():
    """The gateway service's stderr log (loguru writes to stderr) — the same
    file launchd/systemd capture to. Falls back to the stdout log. Returns a
    Path or None when neither exists (e.g. a foreground `flowly gateway` run
    whose output went to the terminal)."""
    import platform
    from pathlib import Path
    if platform.system().lower() == "windows":
        log_dir = Path.home() / "AppData" / "Local" / "flowly" / "logs"
    else:
        from flowly.profile import get_flowly_home
        log_dir = get_flowly_home() / "logs"
    for name in ("flowly-gateway.err.log", "flowly-gateway.out.log"):
        p = log_dir / name
        if p.exists():
            return p
    return None


def logs_tail(params: dict) -> dict:
    """Tail the bot's own log file for the desktop Activity feed (pull
    model: the client POLLS this; nothing is streamed/broadcast).

    Params: ``lines`` (default 200, cap 500) and an optional ``cursor`` (byte
    offset from a previous call). With a cursor only the NEW bytes' lines are
    returned, so the poller gets exact increments with no duplicates; a cursor
    beyond the file size (rotation/truncation) resets to a fresh tail. Returns
    ``{lines, cursor, available}``."""
    limit = params.get("lines", 200)
    limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 200, 500))
    cursor = params.get("cursor")
    cursor = int(cursor) if isinstance(cursor, (int, float)) and cursor >= 0 else None

    path = _gateway_log_file()
    if path is None:
        return {"lines": [], "cursor": 0, "available": False}
    try:
        size = path.stat().st_size
        if cursor is not None and cursor > size:
            cursor = None  # rotated/truncated → fresh tail
        if cursor is not None:
            if cursor == size:
                return {"lines": [], "cursor": size, "available": True}
            with path.open("rb") as f:
                f.seek(cursor)
                chunk = f.read(size - cursor)
        else:
            # Fresh tail: read at most ~256 KB from the end — plenty for 500
            # lines without slurping a huge file.
            start = max(0, size - 256 * 1024)
            with path.open("rb") as f:
                f.seek(start)
                chunk = f.read(size - start)
        text = chunk.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return {"lines": lines[-limit:], "cursor": size, "available": True}
    except Exception:
        return {"lines": [], "cursor": 0, "available": False}


# ── Skills ──────────────────────────────────────────────────────────────────

def skills_list() -> dict:
    """Installed skills (workspace + managed + builtin) in the rich shape the
    desktop Skills UI expects."""
    from flowly.agent.skills import SkillsLoader

    ws = workspace_dir()
    _src = {"builtin": "bundled", "managed": "marketplace", "workspace": "local"}

    def _frontmatter(skill_file: Path) -> dict:
        out: dict = {"category": "", "tags": []}
        try:
            text = skill_file.read_text(encoding="utf-8")
        except Exception:
            return out
        if not text.startswith("---"):
            return out
        end = text.find("\n---", 3)
        if end == -1:
            return out
        for line in text[3:end].splitlines():
            line = line.strip()
            if line.startswith("category:"):
                out["category"] = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("tags:"):
                raw = line.split(":", 1)[1].strip().strip("[]")
                out["tags"] = [t.strip().strip("\"'") for t in raw.split(",") if t.strip()]
        return out

    loader = SkillsLoader(ws)
    skills = []
    for s in loader.list_skills(filter_unavailable=False):
        name = s.get("name", "")
        fm = _frontmatter(Path(s["path"])) if s.get("path") else {"category": "", "tags": []}
        desc = ""
        try:
            desc = loader._get_skill_description(name) or ""
        except Exception:
            pass
        source = _src.get(s.get("source", ""), "local")
        skills.append({
            "slug": name,
            "name": name,
            "description": desc,
            "category": fm.get("category") or "General",
            "tags": fm.get("tags") or [],
            "source": source,
            "isLocal": source == "local",
            "installed": True,
        })
    return {"skills": skills}


# ── Knowledge graph ─────────────────────────────────────────────────────────

def kg_graph() -> dict:
    """Dump knowledge-graph entities + triples (read-only)."""
    import sqlite3
    db = state_db("knowledge_graph.sqlite3")
    if not db.exists():
        return {"entities": [], "triples": []}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        entities = [dict(r) for r in conn.execute(
            "SELECT id, name, type, properties FROM entities"
        )]
        triples = []
        for r in conn.execute(
            "SELECT t.id, s.name AS subject, t.predicate, o.name AS object, "
            "t.valid_from, t.valid_to, t.confidence FROM triples t "
            "JOIN entities s ON t.subject = s.id JOIN entities o ON t.object = o.id"
        ):
            d = dict(r)
            d["current"] = d.get("valid_to") is None
            triples.append(d)
    finally:
        conn.close()
    return {"entities": entities, "triples": triples}


def kg_delete_entity(params: dict) -> dict:
    """Delete an entity and its incident triples + aliases."""
    import sqlite3
    entity_id = params.get("id")
    if entity_id is None:
        raise FeatureRpcError("INVALID", "id required")
    db = state_db("knowledge_graph.sqlite3")
    if not db.exists():
        return {"ok": True}
    conn = sqlite3.connect(db)
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM triples WHERE subject = ? OR object = ?", (entity_id, entity_id))
        try:
            conn.execute("DELETE FROM aliases WHERE entity_id = ?", (entity_id,))
        except sqlite3.OperationalError:
            pass
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── Sessions (Activity → Sessions) ──────────────────────────────────────────

def _session_title(path) -> str | None:
    """Auto-generated chat title from a session's metadata line (first jsonl
    line), or None. Cheap: reads only the opening line, not the whole file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline().strip()
        if not first:
            return None
        meta = json.loads(first)
        if meta.get("_type") != "metadata":
            return None
        title = (meta.get("metadata") or {}).get("title")
        return title if isinstance(title, str) and title.strip() else None
    except Exception:
        return None


def sessions_list() -> dict:
    """Conversation sessions — mirrors the desktop's activity:sessions-list
    exactly (key/fileName/sizeBytes/modifiedAt/channel/chatId)."""
    sessions_dir = get_flowly_home() / "sessions"
    # A session is "running" right now iff the in-flight registry still holds its
    # turn (begin() on chat.send, finish() at true run completion). Lets every
    # client — gateway and relay — surface a live "working" indicator straight
    # from the bot's runtime state, no separate flag to drift.
    from flowly.agent.inflight import get as _inflight_get
    out = []
    if sessions_dir.exists():
        from flowly.session.manager import iter_session_files
        for p in iter_session_files(sessions_dir):
            try:
                st = p.stat()
                base = p.stem
                sep = base.find("_")
                if sep == -1:
                    channel, chat_id = "unknown", base
                else:
                    channel, chat_id = base[:sep], base[sep + 1:]
                key = base.replace("_", ":")
                modified_ms = int(st.st_mtime * 1000)
                # Auto-generated descriptive title, so every
                # relay client can show the SAME name the gateway/CLI surfaces
                # instead of a random session-key suffix. The client writes it
                # to its (encrypted) Firestore conversation doc; the relay
                # itself is untouched — this only enriches the RPC payload.
                title = _session_title(p)
                out.append({
                    "key": key,
                    "fileName": p.name,
                    "sizeBytes": st.st_size,
                    "modifiedAt": modified_ms,
                    "channel": channel,
                    "chatId": chat_id,
                    "title": title,
                    # Superset fields for the TUI gateway client (which reads
                    # ``displayName`` / ``updatedAt``). Same content, one shape
                    # serves every client + transport.
                    "displayName": title or (key.split(":", 1)[-1] if ":" in key else key),
                    "updatedAt": modified_ms,
                    # True while a turn for this session is in flight — drives the
                    # client's "running" shimmer. Old clients ignore the field.
                    "running": _inflight_get(key) is not None,
                })
            except Exception:
                continue
    out.sort(key=lambda s: s["modifiedAt"], reverse=True)
    return {"sessions": out}


def sessions_read(params: dict) -> dict:
    """A session's messages (jsonl lines) — key ``:`` → ``_`` filename."""
    key = params.get("key") or params.get("sessionKey") or ""
    file_name = key.replace(":", "_") + ".jsonl"
    path = get_flowly_home() / "sessions" / file_name
    messages = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except Exception:
                continue
    return {"messages": messages}


# ── Audit (Activity → Audit) ────────────────────────────────────────────────

def audit_list(params: dict) -> dict:
    """Audit entries with filter + pagination — ``{success, entries, total,
    has_more, next_offset}`` (the desktop audit hook's shape)."""
    from flowly.audit.reader import read_entries
    try:
        limit = int(params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    result = read_entries(
        audit_dir(),
        date=params.get("date") or None,
        tool=params.get("tool") or None,
        status=params.get("status") or None,
        search=params.get("search") or None,
        limit=limit,
        offset=offset,
    )
    payload: dict[str, Any] = {"success": True}
    payload.update(result if isinstance(result, dict) else {})
    return payload


def audit_stats() -> dict:
    """Folder-level audit stats (+ retention/size/enabled from config)."""
    from flowly.audit.reader import get_stats
    stats = get_stats(audit_dir())
    payload: dict[str, Any] = {"success": True}
    payload.update(stats if isinstance(stats, dict) else {})
    try:
        from flowly.config.loader import load_config
        cfg = load_config()
        payload["retention_days"] = cfg.audit.retention_days
        payload["max_size_mb"] = cfg.audit.max_size_mb
        payload["enabled"] = cfg.audit.enabled
    except Exception:
        pass
    return payload


def audit_clear() -> dict:
    """Delete every audit file (the desktop UI does the confirmation)."""
    d = audit_dir()
    if d.exists():
        for p in d.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass
    return {"success": True}


# ── Assistants (Agents tab) ─────────────────────────────────────────────────

def _assistants_dir() -> Path:
    return get_flowly_home() / "assistants"


def assistants_list() -> dict:
    """User-defined + builtin assistants in the desktop Agents-tab shape."""
    from flowly.agent.assistants import AssistantRegistry
    reg = AssistantRegistry(_assistants_dir())
    try:
        reg.reload()
    except Exception:
        pass
    out = []
    for a in reg.all():
        out.append({
            "name": a.name,
            "description": a.description,
            "model": a.model,
            "allowedTools": sorted(a.allowed_tools) if a.allowed_tools else None,
            "autoSaveArtifact": getattr(a, "auto_save_artifact", False),
            "artifactType": getattr(a, "artifact_type", None),
            "systemPrompt": getattr(a, "system_prompt", ""),
            "builtin": a.builtin,
            "sourcePath": str(a.source_path) if a.source_path else None,
        })
    return {"assistants": out}


def assistants_write(params: dict) -> dict:
    """Create/edit a user assistant by writing ~/.flowly/assistants/{name}.md —
    same YAML-fronted format the desktop writes locally. Returns
    ``{"success": bool, "error"?: str}``."""
    name = str(params.get("name", ""))
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
        return {"success": False, "error": (
            f"Invalid assistant name '{name}'. Use lowercase letters, digits, "
            "- and _ only."
        )}
    lines = ["---", f"name: {name}",
             f"description: {params.get('description', '')}",
             f"model: {params.get('model', '')}"]
    tools = params.get("allowedTools")
    if tools:
        lines.append("allowed_tools: [" + ", ".join(f'"{t}"' for t in tools) + "]")
    if params.get("autoSaveArtifact") is True:
        lines.append("auto_save_artifact: true")
    at = params.get("artifactType")
    if at and at != "markdown":
        lines.append(f"artifact_type: {at}")
    lines += ["---", "", (params.get("systemPrompt") or "").strip(), ""]
    d = _assistants_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    return {"success": True}


def assistants_delete(params: dict) -> dict:
    """Delete a user assistant (.md). Returns ``{"deleted": bool, "ok": bool}``."""
    name = str(params.get("name", ""))
    deleted = False
    target = _assistants_dir() / f"{name}.md"
    if target.exists():
        target.unlink()
        deleted = True
    return {"deleted": deleted, "ok": deleted}


# ── Pairing (Telegram / WhatsApp device approval) ───────────────────────────

def pairing_list(params: dict) -> dict:
    """Pending pairing requests for a channel."""
    channel = params.get("channel", "telegram")
    if channel not in _PAIRING_CHANNELS:
        raise FeatureRpcError("INVALID", f"unknown pairing channel: {channel}")
    from flowly.pairing.store import list_pairing_requests
    reqs = list_pairing_requests(channel)
    return {"requests": [{
        "id": r.id,
        "code": r.code,
        "createdAt": r.created_at,
        "lastSeenAt": getattr(r, "last_seen_at", None),
        "meta": getattr(r, "meta", {}) or {},
    } for r in reqs]}


def gmail_set_credentials(params: dict) -> dict:
    """Write Gmail OAuth credentials pushed by a client over the feature RPC —
    the transport-native alternative to the web app's SSH delivery, so Gmail
    works the same on a relay bot OR a self-hosted gateway (neither of which
    necessarily has SSH/IP on record). The client obtains the tokens from the
    web OAuth flow and pushes them here; the bot never sees the user's password.

    Expects ``credentials`` with at least ``refresh_token`` (plus the usual
    ``access_token`` / ``expiry`` / ``client_id`` / ``client_secret`` /
    ``email``). Returns ``willRestart`` so the email channel picks them up.
    """
    creds = params.get("credentials")
    if not isinstance(creds, dict):
        raise FeatureRpcError("INVALID", "credentials must be an object")
    if not isinstance(creds.get("refresh_token"), str) or not creds["refresh_token"].strip():
        raise FeatureRpcError("INVALID", "credentials.refresh_token is required")
    from flowly.channels.gmail_auth import save_credentials
    save_credentials(creds)
    return {"ok": True, "willRestart": True}


def subagents_list(params: dict) -> dict:
    """Background subagent tasks for the live agent — running + recent.

    Registered in FEATURE_METHODS so a single shape serves BOTH relay and
    gateway (the gateway's legacy ``_ws_rpc_subagents_list`` is shadowed by this
    one). The user-facing ``label`` is always the task-derived ``display_name``
    (never an internal key like ``builtin:researcher``).

    Params: ``{"status": "running"|"completed"|"failed"|<all>}`` (optional).
    Returns ``{"tasks": [{runId, label, task, model, status, duration,
    createdAt, endedAt, error, parentSessionKey}]}`` — newest first.
    """
    import time as _time

    reg = _registry()
    if reg is None:
        return {"tasks": []}
    try:
        reg._load_from_disk()  # pick up runs spawned since the last read
    except Exception:
        pass

    records = reg.all()
    status_filter = (params or {}).get("status")
    if status_filter == "running":
        records = [r for r in records if r.ended_at is None]
    elif status_filter == "completed":
        records = [r for r in records if r.outcome == "ok"]
    elif status_filter == "failed":
        records = [r for r in records if r.outcome in ("error", "timeout")]

    tasks = []
    for r in sorted(records, key=lambda x: x.created_at, reverse=True):
        duration = None
        if r.started_at and r.ended_at:
            duration = round(r.ended_at - r.started_at, 1)
        elif r.started_at:
            duration = round(_time.time() - r.started_at, 1)
        tasks.append({
            "runId": r.run_id,
            "label": getattr(r, "display_name", "") or r.label,
            "task": r.task,
            "model": r.model,
            "status": "running" if r.ended_at is None else (r.outcome or "unknown"),
            "duration": duration,
            "createdAt": r.created_at,
            "endedAt": r.ended_at,
            "error": r.error,
            "parentSessionKey": r.parent_session_key,
        })
    return {"tasks": tasks}


def subagents_assistants(params: dict) -> dict:
    """Specialists the user can run as subagents, with their model settings —
    for the desktop/TUI "subagent models" editor.

    Each entry resolves the three-tier model policy (see ``builtin_agent``):
      * ``override`` — what ``agents.assistant_models[name]`` holds:
        ``""`` (unset → use the specialist's own default), ``"inherit"``
        (run on the bot's selected model), or a concrete model id.
      * ``effectiveModel`` — the model a spawn would actually use right now.
      * ``defaultModel`` — the specialist's built-in default.

    Returns ``{"assistants": [{name, description, defaultModel, override,
    effectiveModel, builtin}], "botModel": <agents.defaults.model>}``.
    """
    from flowly.agent.assistants import AssistantRegistry
    from flowly.config.loader import load_config

    cfg = load_config()
    overrides = cfg.agents.assistant_models or {}
    bot_model = cfg.agents.defaults.model or ""

    reg = AssistantRegistry(_assistants_dir())
    try:
        reg.reload()
    except Exception:
        pass

    out = []
    for a in reg.all():
        override = overrides.get(a.name, "") or ""
        if override == "inherit":
            effective = bot_model
        elif override:
            effective = override
        else:
            effective = a.model
        out.append({
            "name": a.name,
            "description": a.description,
            "defaultModel": a.model,
            "override": override,
            "effectiveModel": effective,
            "builtin": a.builtin,
        })
    return {"assistants": out, "botModel": bot_model}


def subagents_set_model(params: dict) -> dict:
    """Set (or clear) the per-specialist model override.

    Params: ``{"name": <specialist>, "model": <id>|"inherit"|""}``.
      * a model id  — run that specialist on this model
      * ``"inherit"`` — run it on the bot's selected model
      * ``""`` / null — clear the override (use the specialist's default)

    Writes ``agents.assistant_models`` in config. No restart needed: the next
    ``builtin_agent`` dispatch reads the fresh config when it spawns.
    Returns ``{"ok": True, "name", "override", "effectiveModel", "botModel"}``.
    """
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FeatureRpcError("INVALID", "name must be a non-empty string")
    name = name.strip()

    raw = params.get("model")
    model = (raw or "").strip() if isinstance(raw, str) else ""

    from flowly.agent.assistants import AssistantRegistry
    from flowly.config.loader import get_config_path, load_config
    from flowly.integrations.config_io import (
        _atomic_write_json, _load_raw, _set_path,
    )

    reg = AssistantRegistry(_assistants_dir())
    try:
        reg.reload()
    except Exception:
        pass
    asst = reg.get(name)
    if asst is None:
        raise FeatureRpcError(
            "INVALID", f"unknown specialist '{name}'. Available: {reg.names()}"
        )

    cfg = load_config()
    overrides = dict(cfg.agents.assistant_models or {})
    if model:
        overrides[name] = model
    else:
        overrides.pop(name, None)

    # Write the whole map at once via the raw path (merge=False replaces the
    # leaf). save_config()'s deep-merge can ADD/overwrite keys but never DELETE
    # one, so clearing an override (pop) would silently survive on disk — the
    # raw replace makes "reset to default" actually stick. Atomic + backed up,
    # same convention as the /model picker's _set_default_model.
    raw = _load_raw()
    _set_path(raw, "agents.assistant_models", overrides, merge=False)
    _atomic_write_json(get_config_path(), raw)

    bot_model = cfg.agents.defaults.model or ""
    if model == "inherit":
        effective = bot_model
    elif model:
        effective = model
    else:
        effective = asst.model
    return {
        "ok": True,
        "name": name,
        "override": model,
        "effectiveModel": effective,
        "botModel": bot_model,
    }


async def subagents_spawn(params: dict) -> dict:
    """Manually launch a background subagent for an ad-hoc task.

    Unlike ``builtin_agent`` (which the LLM calls and picks a specialist), this
    is a user-initiated spawn from a client UI (TUI ``/subagents <task>``). It
    runs a generic subagent on the bot's model and announces the result back to
    the origin session asynchronously — the same delivery path async specialists
    use — so the chat the user spawned from receives the answer when it's ready.

    Params:
      ``task`` (required) — what the subagent should do.
      ``sessionKey`` — origin session ("channel:chat_id"); the result is
        announced here. Defaults to ``cli:direct``.
      ``assistant`` — optional specialist name to run as (else generic).

    Returns ``{"ok": True, "runId": <id>, "displayName": <name>}``.
    """
    task = params.get("task")
    if not isinstance(task, str) or not task.strip():
        raise FeatureRpcError("INVALID", "task must be a non-empty string")
    task = task.strip()

    manager = _subagent_manager()
    if manager is None:
        raise FeatureRpcError(
            "UNAVAILABLE", "manual spawn needs a live gateway (not available here)"
        )

    # Split "channel:chat_id" so the async result re-enters this exact session
    # (spawn rebuilds parent_session_key as f"{channel}:{chat_id}").
    session_key = str(params.get("sessionKey") or "cli:direct")
    if ":" in session_key:
        origin_channel, origin_chat_id = session_key.split(":", 1)
    else:
        origin_channel, origin_chat_id = "cli", session_key

    # Optional specialist. Unknown name ⇒ generic subagent (don't fail the
    # spawn over a bad assistant; the task still runs).
    assistant = None
    asst_name = params.get("assistant")
    if isinstance(asst_name, str) and asst_name.strip():
        try:
            from flowly.agent.assistants import AssistantRegistry
            reg = AssistantRegistry(_assistants_dir())
            try:
                reg.reload()
            except Exception:
                pass
            assistant = reg.get(asst_name.strip())
        except Exception:
            assistant = None

    raw = await manager.spawn(
        task=task,
        label="manual",
        origin_channel=origin_channel,
        origin_chat_id=origin_chat_id,
        cleanup="keep",
        wait=False,
        assistant=assistant,
    )
    # An async (wait=False) spawn always returns a JSON envelope: on success
    # ``{"status": "dispatched", "run_id": ..., "label": ...}``, on refusal
    # ``{"status": "rejected"|"forbidden"|"error", "error": ...}`` (concurrency
    # cap / recursion guard). Parse the status — DON'T treat every JSON return
    # as a refusal, which would wrongly error out a subagent that did launch.
    payload: dict = {}
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
    status = str(payload.get("status") or "")
    if status in ("rejected", "forbidden", "error"):
        raise FeatureRpcError(
            "REJECTED",
            str(payload.get("error") or payload.get("message") or "subagent spawn refused"),
        )

    return {
        "ok": True,
        "runId": str(payload.get("run_id") or ""),
        "displayName": str(payload.get("label") or task[:60]),
    }


def board_snapshot() -> dict:
    """The cross-channel task board as columns + counts (Todo / In Progress /
    Waiting / Done). Read-only — same payload the gateway's HTTP `/api/board`
    serves, now reachable over relay too.

    Wrapped as ``{"snapshot": <data>}`` to match the legacy gateway WS handler
    (`_ws_rpc_board_snapshot`) this method now shadows in FEATURE_METHODS — the
    TUI client reads ``reply.get("snapshot")``, so the envelope must stay."""
    store, _ = _board()
    if store is None:
        raise FeatureRpcError("UNAVAILABLE", "Board not configured")
    return {"snapshot": store.snapshot()}


async def board_action(params: dict) -> dict:
    """Apply a board action (add / move / update / note / delete / clear / run /
    cancel) over either transport. Shared single-writer store; ``run``/``cancel``
    drive the agent's board orchestrator."""
    from flowly.board.actions import apply_board_action
    store, orchestrator = _board()
    result, status = await apply_board_action(store, orchestrator, params)
    if status >= 400 and not result.get("ok"):
        raise FeatureRpcError("BOARD", result.get("error", "board action failed"))
    return result


def board_card(params: dict) -> dict:
    """Full audit detail for ONE card — for the desktop/iOS task-detail view:

    * ``card``: the card with its notes (full result, error, status, timestamps).
    * ``run``:  the linked subagent run's timing + outcome + the **tool trace**
                (every tool call: name, arg/result sizes, status, duration_ms),
                or null if the card hasn't run / the run record is gone.
    """
    store, _ = _board()
    if store is None:
        raise FeatureRpcError("UNAVAILABLE", "Board not configured")
    card_id = (params.get("cardId") or params.get("id") or "").strip()
    if not card_id:
        raise FeatureRpcError("INVALID", "cardId required")
    card = store.get_card(card_id, with_notes=True)
    if card is None:
        return {"card": None, "run": None}

    run = None
    reg = _registry()
    rec = None
    if reg is not None:
        run_id = getattr(card, "run_id", None)
        rec = reg.get(run_id) if run_id else None
        # Fallback: cards run before run_id linking (or after a re-run) still have
        # a registry record labelled with the card id — surface its trace too.
        if rec is None:
            rec = reg.latest_by_label(card_id)
    if rec is not None:
        run = {
            "runId": rec.run_id,
            "startedAt": rec.started_at,
            "endedAt": rec.ended_at,
            "outcome": rec.outcome,
            "error": rec.error,
            "model": rec.model,
            "toolTrace": list(rec.tool_trace or []),
        }
    return {"card": card.to_dict(), "run": run}


# ── Cron / scheduled jobs ──────────────────────────────────────────────────
# Read/write the SAME CronService the relay's Firestore/web path uses — these
# RPCs are additive (list/add/update/remove/run/output for a desktop+iOS UI over
# relay AND gateway). The relay's cron delivery is untouched.

def _cron_job_to_dict(j) -> dict:
    """Serialize a CronJob to the UI shape (mirrors the jobs.json on-disk form)."""
    return {
        "id": j.id,
        "name": j.name,
        "enabled": j.enabled,
        "schedule": {
            "kind": j.schedule.kind,
            "atMs": j.schedule.at_ms,
            "everyMs": j.schedule.every_ms,
            "expr": j.schedule.expr,
            "tz": j.schedule.tz,
        },
        "payload": {
            "kind": j.payload.kind,
            "message": j.payload.message,
            "deliver": j.payload.deliver,
            "channel": j.payload.channel,
            "to": j.payload.to,
        },
        "state": {
            "nextRunAtMs": j.state.next_run_at_ms,
            "lastRunAtMs": j.state.last_run_at_ms,
            "lastStatus": j.state.last_status,
            "lastError": j.state.last_error,
            "lastDeliveryError": j.state.last_delivery_error,
            "consecutiveFailures": j.state.consecutive_failures,
        },
        "origin": (
            {
                "platform": j.origin.platform,
                "chatId": j.origin.chat_id,
                "chatName": j.origin.chat_name,
                "threadId": j.origin.thread_id,
            }
            if j.origin else None
        ),
        "createdAtMs": j.created_at_ms,
        "updatedAtMs": j.updated_at_ms,
        "repeatTimes": j.repeat_times,
        "repeatCompleted": j.repeat_completed,
        "model": j.model,
        "provider": j.provider,
    }


def cron_list(params: dict) -> dict:
    """All scheduled jobs (enabled + disabled) as ``{jobs: [...]}``."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    include_disabled = bool(params.get("includeDisabled", True))
    jobs = svc.list_jobs(include_disabled=include_disabled)
    return {"jobs": [_cron_job_to_dict(j) for j in jobs]}


def cron_add(params: dict) -> dict:
    """Create a scheduled job. ``schedule`` = ``{kind: at|every|cron, atMs?,
    everyMs?, expr?, tz?}``. ``deliver`` true routes the result to the chat
    (origin), false keeps it on the job (view in the cron UI)."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    from flowly.cron.types import CronSchedule, CronOrigin

    name = (params.get("name") or "").strip()
    message = (params.get("message") or "").strip()
    if not name:
        raise FeatureRpcError("INVALID", "name required")

    sched = params.get("schedule") or {}
    try:
        schedule = CronSchedule(
            kind=sched.get("kind"),
            at_ms=sched.get("atMs"),
            every_ms=sched.get("everyMs"),
            expr=sched.get("expr"),
            tz=sched.get("tz"),
        )
    except Exception as e:
        raise FeatureRpcError("INVALID", f"bad schedule: {e}")

    origin = None
    o = params.get("origin")
    if isinstance(o, dict):
        origin = CronOrigin(
            platform=o.get("platform"),
            chat_id=o.get("chatId"),
            chat_name=o.get("chatName"),
            thread_id=o.get("threadId"),
        )

    try:
        job = svc.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=bool(params.get("deliver", True)),
            channel=params.get("channel"),
            to=params.get("to"),
            origin=origin,
            delete_after_run=(schedule.kind == "at"),
            model=params.get("model"),
            provider=params.get("provider"),
        )
    except ValueError as e:
        raise FeatureRpcError("INVALID", str(e))
    return {"job": _cron_job_to_dict(job)}


def cron_remove(params: dict) -> dict:
    """Delete a job by id (or name)."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    jid = str(params.get("id") or "")
    if not jid:
        raise FeatureRpcError("INVALID", "id required")
    return {"ok": svc.remove_job(jid)}


def cron_update(params: dict) -> dict:
    """Enable/disable (``enabled``) or patch fields (``updates``) of a job."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    jid = str(params.get("id") or "")
    if not jid:
        raise FeatureRpcError("INVALID", "id required")
    if "enabled" in params:
        job = svc.enable_job(jid, bool(params.get("enabled")))
    elif isinstance(params.get("updates"), dict):
        job = svc.update_job(jid, params["updates"])
    else:
        raise FeatureRpcError("INVALID", "provide enabled or updates")
    if job is None:
        raise FeatureRpcError("NOT_FOUND", "job not found")
    return {"job": _cron_job_to_dict(job)}


async def cron_run(params: dict) -> dict:
    """Run a job now (``force`` defaults true so disabled jobs still run)."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    jid = str(params.get("id") or "")
    if not jid:
        raise FeatureRpcError("INVALID", "id required")
    ok = await svc.run_job(jid, force=bool(params.get("force", True)))
    return {"ok": ok}


def cron_output(params: dict) -> dict:
    """Recent run outputs (archived ``.md`` per run) for a job, newest-first."""
    svc = _cron()
    if svc is None:
        raise FeatureRpcError("UNAVAILABLE", "Scheduler not configured")
    jid = str(params.get("id") or "")
    if not jid:
        raise FeatureRpcError("INVALID", "id required")
    try:
        limit = max(1, min(int(params.get("limit", 10) or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    job_dir = (svc.store_path.parent / "output" / jid)
    outputs: list[dict[str, Any]] = []
    if job_dir.exists():
        files = sorted(job_dir.glob("*.md"), reverse=True)[:limit]
        for f in files:
            try:
                outputs.append({"name": f.stem, "content": f.read_text(encoding="utf-8")})
            except OSError:
                continue
    return {"outputs": outputs}


# ── Push notifications (anonymous push-relay) ──────────────────────────────
# A device registers its opaque (pushId, pushSecret) with us so we can forward
# APNs/FCM through the relay for out-of-band deliveries (cron results while the
# app is closed). No Flowly account needed.

def push_register(params: dict) -> dict:
    """Register a device's relay push credentials (from /api/push/register)."""
    from flowly.push.relay_push import get_push_registry
    push_id = (params.get("pushId") or "").strip()
    push_secret = (params.get("pushSecret") or "").strip()
    if not push_id or not push_secret:
        raise FeatureRpcError("INVALID", "pushId and pushSecret required")
    get_push_registry().register(
        push_id=push_id,
        push_secret=push_secret,
        gateway_id=(params.get("gatewayId") or "").strip(),
        platform=(params.get("platform") or "ios").strip(),
        kind=(params.get("kind") or "gateway").strip(),
    )
    return {"ok": True}


def push_unregister(params: dict) -> dict:
    """Drop a device's push registration (logout / disable notifications)."""
    from flowly.push.relay_push import get_push_registry
    pid = (params.get("pushId") or "").strip()
    if pid:
        get_push_registry().unregister(pid)
    return {"ok": True}


def pairing_approve(params: dict) -> dict:
    """Approve a pairing code — same effect as
    ``flowly pairing approve <channel> <code>``."""
    channel = params.get("channel", "telegram")
    code = (params.get("code") or "").strip()
    if channel not in _PAIRING_CHANNELS:
        raise FeatureRpcError("INVALID", f"unknown pairing channel: {channel}")
    if not code:
        raise FeatureRpcError("INVALID", "missing pairing code")
    from flowly.pairing.store import approve_pairing_code
    approved = approve_pairing_code(channel, code)
    return {
        "ok": approved is not None,
        "approved": None if approved is None else {
            "id": approved.id,
            "code": approved.code,
            "meta": getattr(approved, "meta", {}) or {},
        },
    }


# ── Dispatch table (shared by every transport: relay + direct gateway) ──────
#
# A single source of truth for the feature-RPC surface. Each transport just
# wraps :func:`dispatch` in its own reply envelope + restart scheduling, so
# adding an RPC here lights it up over both relay and gateway at once.

from functools import partial as _partial
import inspect as _inspect


def system_capabilities() -> dict:
    """Advertise this bot's version + its full feature-method surface.

    A client calls this ONCE on connect and gates its UI on what's actually
    available, prompting the user to update an older bot/desktop instead of
    firing calls that 404 on the gateway or hang until timeout on the relay's
    silent unknown-method path. Served over BOTH transports via the shared
    dispatch. An OLD bot that predates this method simply returns no result
    (UNKNOWN_METHOD on gateway, silence on relay), which the client reads as
    "legacy bot — hide the newer features." ``featureMethods`` includes this
    method itself, so its mere presence already signals a capable bot.
    """
    from flowly import __version__
    return {
        "version": __version__,
        "featureMethods": sorted(_DISPATCH),
    }


# method → (handler, wants_params, restart_aware)
#   wants_params  — call ``handler(params)`` vs ``handler()``
#   restart_aware — a ``willRestart`` in the result means the transport should
#                   ACK then bounce the gateway
_DISPATCH: dict[str, tuple] = {
    "system.capabilities": (system_capabilities, False, False),
    "connections.list":   (connections_list, False, False),
    "connections.set":    (connections_set, True, True),
    "gmail.set_credentials": (gmail_set_credentials, True, True),
    "board.snapshot":     (board_snapshot, False, False),
    "board.action":       (board_action, True, False),
    "subagents.list":     (subagents_list, True, False),
    "subagents.assistants": (subagents_assistants, True, False),
    "subagents.set_model": (subagents_set_model, True, False),
    "subagents.spawn":    (subagents_spawn, True, False),
    "board.card":         (board_card, True, False),
    "cron.list":          (cron_list, True, False),
    "cron.add":           (cron_add, True, False),
    "cron.update":        (cron_update, True, False),
    "cron.remove":        (cron_remove, True, False),
    "cron.run":           (cron_run, True, False),
    "cron.output":        (cron_output, True, False),
    "push.register":      (push_register, True, False),
    "push.unregister":    (push_unregister, True, False),
    "chat.inflight":      (chat_inflight, True, False),
    "config.get":         (config_get, False, False),
    "config.set":         (config_set, True, True),
    "exec.policy.get":              (exec_policy_get, False, False),
    "exec.policy.set":              (exec_policy_set, True, False),
    "exec.policy.allowlist.remove": (exec_policy_allowlist_remove, True, False),
    "codex.policy.get":             (codex_policy_get, False, False),
    "codex.policy.set":             (codex_policy_set, True, True),
    "pet.info":           (pet_info, True, False),
    "pet.gallery":        (pet_gallery, True, False),
    "pet.select":         (pet_select, True, False),
    "pet.disable":        (pet_disable, True, False),
    "pet.scale":          (pet_scale, True, False),
    "pet.thumb":          (pet_thumb, True, False),
    "mcp.list":           (mcp_list, False, False),
    "mcp.upsert":         (mcp_upsert, True, True),
    "mcp.set_enabled":    (mcp_set_enabled, True, True),
    "mcp.remove":         (mcp_remove, True, True),
    "mcp.install":        (mcp_install, True, True),
    "mcp.test":           (mcp_test, True, False),
    "mcp.oauth_start":    (mcp_oauth_start, True, True),
    "memory.entries":     (memory_entries, False, False),
    "memory.update_user": (memory_update_user, True, False),
    "memory.gov_list":    (_partial(memory_gov, "list"), True, False),
    "memory.review":      (_partial(memory_gov, "review"), True, False),
    "memory.stats":       (_partial(memory_gov, "stats"), True, False),
    "memory.accept":      (_partial(memory_gov, "accept"), True, False),
    "memory.reject":      (_partial(memory_gov, "reject"), True, False),
    "memory.correct":     (_partial(memory_gov, "correct"), True, False),
    "memory.feedback":    (_partial(memory_gov, "feedback"), True, False),
    "memory.consolidate": (memory_consolidate, True, False),
    "memory.dream":       (memory_dream, True, False),
    "memory.import_prompt": (memory_import_prompt, True, False),
    "memory.import":      (memory_import, True, False),
    "obsidian.status":    (_partial(obsidian_rpc, "status"), True, False),
    "obsidian.search":    (_partial(obsidian_rpc, "search"), True, False),
    "persona.list":       (persona_list, False, False),
    "provider.active":    (provider_active, False, False),
    "provider.list":      (provider_list, False, False),
    "provider.set":       (provider_set, True, True),
    "provider.set_key":   (provider_set_key, True, True),
    "provider.set_flowly_account": (provider_set_flowly_account, True, True),
    "logs.tail":          (logs_tail, True, False),
    "artifacts.list":     (artifacts_list, True, False),
    "artifacts.get":      (artifacts_get, True, False),
    "artifacts.update":   (artifacts_update, True, False),
    "artifacts.delete":   (artifacts_delete, True, False),
    "artifacts.pin":      (artifacts_pin, True, False),
    "artifacts.versions": (artifacts_versions, True, False),
    "flowlets.list":      (flowlets_list, True, False),
    "flowlets.get":       (flowlets_get, True, False),
    "flowlets.state":     (flowlets_state, True, False),
    "flowlets.action":    (flowlets_action, True, False),
    "flowlets.refresh":   (flowlets_refresh, True, False),
    "flowlets.capture":   (flowlets_capture, True, False),
    "flowlets.attachment": (flowlets_attachment, True, False),
    "flowlets.itemRemove": (flowlets_item_remove, True, False),
    "flowlets.pin":       (flowlets_pin, True, False),
    "flowlets.delete":    (flowlets_delete, True, False),
    "model.list":         (model_list, True, False),
    "model.set":          (model_set, True, True),
    "assistants.list":    (assistants_list, False, False),
    "assistants.write":   (assistants_write, True, False),
    "assistants.delete":  (assistants_delete, True, False),
    "skills.list":        (skills_list, False, False),
    "kg.graph":           (kg_graph, False, False),
    "kg.delete_entity":   (kg_delete_entity, True, False),
    "sessions.list":      (sessions_list, False, False),
    "sessions.read":      (sessions_read, True, False),
    "audit.list":         (audit_list, True, False),
    "audit.stats":        (audit_stats, False, False),
    "audit.clear":        (audit_clear, False, False),
    "pairing.list":       (pairing_list, True, False),
    "pairing.approve":    (pairing_approve, True, False),
}

#: Every method this module serves. Transports gate on membership.
FEATURE_METHODS = frozenset(_DISPATCH)


async def dispatch(method: str, params: dict) -> tuple[dict, bool]:
    """Run a feature RPC by name.

    Returns ``(result, needs_restart)``. Raises :class:`FeatureRpcError` for an
    unknown method or a structured handler error; any other exception
    propagates so the transport can map it to an INTERNAL error.
    """
    entry = _DISPATCH.get(method)
    if entry is None:
        raise FeatureRpcError("UNKNOWN_METHOD", f"unknown feature method: {method}")
    fn, wants_params, restart = entry
    result = fn(params) if wants_params else fn()
    if _inspect.isawaitable(result):
        result = await result
    needs_restart = bool(restart and isinstance(result, dict) and result.get("willRestart"))
    return result, needs_restart
