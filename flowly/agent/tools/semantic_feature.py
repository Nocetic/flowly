"""Consent and installation lifecycle for local semantic tool routing.

The routing model is an optional optimization, never a correctness dependency.
This module owns the user-facing state machine and the verified model install;
the agent loop owns inference.  Keeping those responsibilities separate means
Desktop, relay, gateway, and CLI all observe one durable decision without
coupling the feature to conversation memory or to a particular UI process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

from flowly.config.loader import convert_to_camel, get_config_path, load_config
from flowly.integrations.config_io import (
    _assert_config_valid,
    _atomic_write_json,
    _load_raw_or_empty,
)
from flowly.profile import default_home, get_flowly_home

RECOMMENDATION_VERSION = 1

_STATE_LOCK = threading.RLock()
_INSTALL_STATE = "idle"
_INSTALL_ERROR = ""
_INSTALL_STARTED_AT = 0.0
_INSTALL_FINISHED_AT = 0.0


def semantic_model_cache_dir(state_dir: Path | None = None) -> Path:
    """Return the cache shared by Desktop and CLI for the active profile."""

    active_home = get_flowly_home().resolve()
    canonical_home = default_home().resolve()
    managed_profile = (
        active_home == canonical_home
        or active_home.is_relative_to(canonical_home / "profiles")
    )
    if managed_profile:
        return canonical_home / "cache" / "tool-routing" / "models"
    return Path(state_dir or active_home) / "tool-routing" / "models"


def semantic_runtime_available() -> bool:
    """Whether this build can run the optional local encoder."""

    required_modules = (
        "fastembed",
        "huggingface_hub",
        "onnxruntime",
        "tokenizers",
    )
    if any(importlib.util.find_spec(name) is None for name in required_modules):
        return False
    try:
        # FastEmbed imports its own distribution version at module import.
        # Nuitka builds therefore need both package code and metadata.
        version("fastembed")
    except PackageNotFoundError:
        return False
    return True


def _model_marker(cache_dir: Path) -> Path:
    return Path(cache_dir) / "flowly-tool-routing-model.json"


def _manifest_bytes() -> int:
    from flowly.agent.tools.semantic_routing import DEFAULT_MODEL_MANIFEST

    return sum(size for size, _digest in DEFAULT_MODEL_MANIFEST.values())


def _valid_sized_model_dir(path: Path) -> bool:
    from flowly.agent.tools.semantic_routing import DEFAULT_MODEL_MANIFEST

    try:
        return all(
            (path / relative).is_file()
            and (path / relative).stat().st_size == expected_size
            for relative, (expected_size, _digest) in DEFAULT_MODEL_MANIFEST.items()
        )
    except OSError:
        return False


def installed_model_dir(cache_dir: Path | None = None) -> Path | None:
    """Cheap installation check; startup performs the authoritative hashes."""

    from flowly.agent.tools.semantic_routing import (
        DEFAULT_ENCODER_ID,
        _packaged_model_dir,
    )

    packaged = _packaged_model_dir()
    if _valid_sized_model_dir(packaged):
        return packaged

    cache = Path(cache_dir or semantic_model_cache_dir())
    marker = _model_marker(cache)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("encoderId") != DEFAULT_ENCODER_ID:
            return None
        candidate = Path(str(payload.get("modelDir") or ""))
    except (OSError, ValueError, TypeError):
        return None
    return candidate if _valid_sized_model_dir(candidate) else None


def _write_model_marker(cache_dir: Path, model_dir: Path) -> None:
    from flowly.agent.tools.semantic_routing import DEFAULT_ENCODER_ID

    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = _model_marker(cache_dir)
    temp = marker.with_name(f"{marker.name}.tmp.{secrets.token_hex(4)}")
    payload = {
        "schemaVersion": 1,
        "encoderId": DEFAULT_ENCODER_ID,
        "modelDir": str(model_dir.resolve()),
        "verifiedAt": int(time.time()),
    }
    try:
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, marker)
    finally:
        temp.unlink(missing_ok=True)


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def persist_semantic_preference(
    *,
    consent: str,
    enabled: bool,
    dismissed_tool_count: int | None = None,
    dismissed_schema_tokens: int | None = None,
) -> None:
    """Atomically persist the owner decision without rewriting sibling config."""

    if consent not in {"undecided", "enabled", "dismissed"}:
        raise ValueError("invalid semantic routing consent")
    path = get_config_path()
    discovery_patch: dict[str, Any] = {
        "semantic_routing_consent": consent,
        "semantic_routing_enabled": bool(enabled),
        # Downloads happen only inside the explicit enable RPC.
        "semantic_model_auto_download": False,
    }
    if dismissed_tool_count is not None:
        discovery_patch["semantic_recommendation_dismissed_tool_count"] = max(
            0, int(dismissed_tool_count)
        )
    if dismissed_schema_tokens is not None:
        discovery_patch["semantic_recommendation_dismissed_schema_tokens"] = max(
            0, int(dismissed_schema_tokens)
        )
    patch = convert_to_camel({
        "tools": {
            "routing": {
                "discovery": discovery_patch
            }
        }
    })
    merged = _deep_merge(_load_raw_or_empty(path), patch)
    _assert_config_valid(merged)
    _atomic_write_json(path, merged)


def install_semantic_model(cache_dir: Path | None = None) -> Path:
    """Download, checksum, initialize, and smoke-test the pinned local model."""

    if not semantic_runtime_available():
        raise RuntimeError("this Flowly build does not include the local routing runtime")

    from flowly.agent.tools.semantic_routing import FastEmbedLocalEncoder

    cache = Path(cache_dir or semantic_model_cache_dir())
    encoder = FastEmbedLocalEncoder(cache_dir=cache, allow_download=True)
    # Construction validates every pinned asset and loads ONNX. One inference
    # catches architecture/runtime incompatibility before the config is enabled.
    vector = encoder.embed_query("verify local semantic tool routing")
    if len(vector) == 0:
        raise RuntimeError("local routing model returned no verification vector")
    model_dir = FastEmbedLocalEncoder._resolve_model(cache, allow_download=False)
    _write_model_marker(cache, model_dir)
    return model_dir


def begin_install() -> bool:
    """Claim the single process-wide install slot."""

    global _INSTALL_ERROR, _INSTALL_STARTED_AT, _INSTALL_STATE
    with _STATE_LOCK:
        if _INSTALL_STATE == "downloading":
            return False
        _INSTALL_STATE = "downloading"
        _INSTALL_ERROR = ""
        _INSTALL_STARTED_AT = time.time()
        return True


def finish_install(error: str = "") -> None:
    global _INSTALL_ERROR, _INSTALL_FINISHED_AT, _INSTALL_STATE
    with _STATE_LOCK:
        _INSTALL_STATE = "failed" if error else "installed"
        _INSTALL_ERROR = str(error or "")[:500]
        _INSTALL_FINISHED_AT = time.time()


def reset_install_state_for_tests() -> None:
    global _INSTALL_ERROR, _INSTALL_FINISHED_AT, _INSTALL_STARTED_AT, _INSTALL_STATE
    with _STATE_LOCK:
        _INSTALL_STATE = "idle"
        _INSTALL_ERROR = ""
        _INSTALL_STARTED_AT = 0.0
        _INSTALL_FINISHED_AT = 0.0


def feature_status(metrics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the stable RPC DTO used by every product surface."""

    metrics = dict(metrics or {})
    config = load_config()
    discovery = config.tools.routing.discovery
    configured_enabled = bool(discovery.semantic_routing_enabled)
    consent = str(discovery.semantic_routing_consent)
    # A manual pre-consent config edit is itself an explicit operator choice.
    if configured_enabled and consent == "undecided":
        consent = "enabled"

    runtime_available = semantic_runtime_available()
    model_dir = installed_model_dir()
    model_installed = model_dir is not None
    effective_enabled = configured_enabled and runtime_available and model_installed
    deferred_count = max(0, int(metrics.get("deferredToolCount") or 0))
    schema_tokens = max(0, int(metrics.get("deferredSchemaTokens") or 0))
    eligible = bool(metrics.get("eligible")) and (
        deferred_count >= int(discovery.semantic_recommendation_min_tools)
        and schema_tokens >= int(discovery.semantic_recommendation_min_schema_tokens)
    )
    dismissed_growth = (
        deferred_count
        >= int(discovery.semantic_recommendation_dismissed_tool_count)
        + int(discovery.semantic_recommendation_growth_tools)
        and schema_tokens
        >= int(discovery.semantic_recommendation_dismissed_schema_tokens)
        + int(discovery.semantic_recommendation_growth_schema_tokens)
    )

    with _STATE_LOCK:
        install_state = _INSTALL_STATE
        install_error = _INSTALL_ERROR
        install_started_at = _INSTALL_STARTED_AT
        install_finished_at = _INSTALL_FINISHED_AT

    router_state = str(metrics.get("routerState") or "disabled")
    if install_state == "downloading":
        state = "downloading"
    elif consent == "enabled" and install_state == "failed":
        state = "failed"
    elif effective_enabled:
        state = "enabled"
    elif consent == "dismissed" and eligible and dismissed_growth and runtime_available:
        state = "recommended"
    elif consent == "dismissed":
        state = "dismissed"
    elif consent == "enabled":
        state = "needs_download" if runtime_available else "unsupported"
    elif eligible and runtime_available:
        state = "recommended"
    elif eligible:
        state = "unsupported"
    else:
        state = "idle"

    original_tokens = max(0, int(metrics.get("originalSchemaTokens") or 0))
    disclosed_tokens = max(0, int(metrics.get("disclosedSchemaTokens") or 0))
    saved_per_round = max(0, original_tokens - disclosed_tokens)
    return {
        "schemaVersion": 1,
        "recommendationVersion": RECOMMENDATION_VERSION,
        "state": state,
        "consent": consent,
        "enabled": effective_enabled,
        "configuredEnabled": configured_enabled,
        "recommended": state == "recommended",
        "recommendationResurfaced": state == "recommended" and consent == "dismissed",
        "runtimeAvailable": runtime_available,
        "modelInstalled": model_installed,
        "modelBytes": _manifest_bytes(),
        "catalogReady": bool(metrics.get("catalogReady", True)),
        "toolCount": max(0, int(metrics.get("toolCount") or 0)),
        "deferredToolCount": deferred_count,
        "deferredSchemaTokens": schema_tokens,
        # These tokens are already avoided by progressive disclosure itself.
        # Keep the telemetry explicit so clients do not misrepresent it as an
        # incremental saving created by semantic routing.
        "schemaTokensAvoidedByDisclosurePerRound": saved_per_round,
        "schemaTokensAvoidedByDisclosurePerTypicalTurn": saved_per_round * 2,
        "routerState": router_state,
        "installState": install_state,
        "installError": install_error,
        "installStartedAt": install_started_at,
        "installFinishedAt": install_finished_at,
    }
