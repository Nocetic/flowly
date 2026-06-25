"""Feature flag registry and store.

Two sources of truth (highest priority wins):
  1. Remote overrides — set by flowly-desktop/flowly-app based on user plan
  2. Local overrides  — ~/.flowly/features.json, user can edit manually
  3. Registry default — hardcoded below

Each flag has a tier:
  - "free"  — enabled by default, available in open source
  - "pro"   — disabled by default, requires Pro plan or manual override
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FeatureFlag:
    name: str
    default: bool
    tier: str  # "free" | "pro"
    description: str


# ── Flag Registry ────────────────────────────────────────────────────
# Add new flags here. Default determines behavior when no override exists.

REGISTRY: dict[str, FeatureFlag] = {
    # Free tier — open source
    "sandbox_ssrf": FeatureFlag(
        "sandbox_ssrf", default=True, tier="free",
        description="Block web_fetch/browser_tab requests to private IPs and cloud metadata",
    ),
    "content_guard_external": FeatureFlag(
        "content_guard_external", default=True, tier="free",
        description="Scan web_fetch/browser_tab results for prompt injection",
    ),
    "audit_log": FeatureFlag(
        "audit_log", default=True, tier="free",
        description="Local JSONL audit logging of tool calls and LLM calls",
    ),

    # Pro tier — closed source / paid plan
    "verification_agent": FeatureFlag(
        "verification_agent", default=False, tier="pro",
        description="Independent adversarial verification after multi-file changes",
    ),
    "permission_bridge": FeatureFlag(
        "permission_bridge", default=False, tier="pro",
        description="Role-based tool permissions for subagents and teammates",
    ),
    "remote_telemetry": FeatureFlag(
        "remote_telemetry", default=False, tier="pro",
        description="Stream events to Datadog or custom endpoint",
    ),
    "advanced_team_orchestration": FeatureFlag(
        "advanced_team_orchestration", default=False, tier="pro",
        description="Permission sync, reconnection, and dynamic delegation",
    ),
}


class FeatureFlagStore:
    """Reads flags from local JSON file + optional remote overrides."""

    def __init__(self) -> None:
        self._local: dict[str, bool] = {}
        self._remote: dict[str, bool] = {}
        self._loaded = False

    def _local_path(self) -> Path:
        from flowly.profile import get_flowly_home
        return get_flowly_home() / "features.json"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._local_path()
        if path.exists():
            try:
                self._local = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self._local = {}

    def save_local(self) -> None:
        path = self._local_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._local, indent=2) + "\n", encoding="utf-8")

    def set_local(self, flag: str, value: bool) -> None:
        self._ensure_loaded()
        self._local[flag] = value
        self.save_local()

    def set_remote(self, flags: dict[str, bool]) -> None:
        """Called by desktop app or Pro plugin after plan validation."""
        self._remote = flags

    def is_enabled(self, flag: str) -> bool:
        """Check if a feature is enabled.

        Priority: remote > local > registry default.
        """
        self._ensure_loaded()

        if flag in self._remote:
            return self._remote[flag]
        if flag in self._local:
            return self._local[flag]

        reg = REGISTRY.get(flag)
        return reg.default if reg else False

    def list_all(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        result = []
        for name, reg in REGISTRY.items():
            result.append({
                "name": name,
                "enabled": self.is_enabled(name),
                "default": reg.default,
                "tier": reg.tier,
                "description": reg.description,
                "source": (
                    "remote" if name in self._remote else
                    "local" if name in self._local else
                    "default"
                ),
            })
        return result


# ── Module-level singleton ───────────────────────────────────────────

_store: FeatureFlagStore | None = None


def _get_store() -> FeatureFlagStore:
    global _store
    if _store is None:
        _store = FeatureFlagStore()
    return _store


def is_enabled(flag: str) -> bool:
    """Check if a feature flag is enabled."""
    return _get_store().is_enabled(flag)


def set_flag(flag: str, value: bool) -> None:
    """Set a local feature flag override."""
    _get_store().set_local(flag, value)


def list_flags() -> list[dict[str, Any]]:
    """List all flags with current state."""
    return _get_store().list_all()
