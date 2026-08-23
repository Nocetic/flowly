"""Apply account changes without disturbing provider hot-reload semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from flowly.integrations.service_control import (
    gateway_is_listening,
    restart_gateway,
)
from flowly.tui.gateway_reload import post_provider_reload

RuntimeApplyStatus = Literal[
    "unchanged",
    "provider_reloaded",
    "gateway_restarted",
    "next_start",
    "manual_restart",
    "provider_reload_failed",
    "gateway_restart_failed",
]


@dataclass(frozen=True)
class RuntimeApplyResult:
    ok: bool
    status: RuntimeApplyStatus
    detail: str = ""


def _gateway_port() -> int:
    try:
        from flowly.config.loader import load_config

        return int(load_config().gateway.port)
    except Exception:  # noqa: BLE001
        return 18790


async def _restart_managed_or_classify_manual() -> RuntimeApplyResult:
    port = _gateway_port()
    result = await restart_gateway(health_check_port=port)
    if result.ok:
        return RuntimeApplyResult(True, "gateway_restarted", result.detail)
    if result.method == "no_service":
        if gateway_is_listening(port=port):
            return RuntimeApplyResult(False, "manual_restart", result.detail)
        return RuntimeApplyResult(True, "next_start", result.detail)
    return RuntimeApplyResult(False, "gateway_restart_failed", result.detail)


async def apply_account_runtime_change(
    *,
    provider_reload_required: bool,
    relay_restart_required: bool,
    security_sensitive: bool = False,
) -> RuntimeApplyResult:
    """Apply config changes using the least disruptive valid mechanism.

    Relay channel credentials are boot-time state and require a service
    restart. Provider/account-key changes use the existing authenticated
    provider hot-reload endpoint. Only a security-sensitive credential removal
    falls back to restart when hot-reload cannot evict the old provider.
    """
    if relay_restart_required:
        return await _restart_managed_or_classify_manual()
    if not provider_reload_required:
        return RuntimeApplyResult(True, "unchanged")

    port = _gateway_port()
    try:
        response = await post_provider_reload(timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        if not gateway_is_listening(port=port):
            return RuntimeApplyResult(True, "next_start", str(exc))
        if security_sensitive:
            return await _restart_managed_or_classify_manual()
        return RuntimeApplyResult(False, "provider_reload_failed", str(exc))

    if response.status_code == 200:
        data = response.json() or {}
        detail = str(data.get("source") or data.get("key") or "provider reloaded")
        return RuntimeApplyResult(True, "provider_reloaded", detail)

    try:
        detail = str((response.json() or {}).get("error") or "")
    except Exception:  # noqa: BLE001
        detail = ""
    detail = detail or f"HTTP {response.status_code}"
    if security_sensitive:
        return await _restart_managed_or_classify_manual()
    return RuntimeApplyResult(False, "provider_reload_failed", detail)
