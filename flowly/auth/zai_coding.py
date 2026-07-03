"""Z.AI GLM Coding Plan credential helpers.

The GLM Coding Plan is an API-key subscription surface, not OAuth. Flowly
stores a user-pasted plan key in its own keychain / credentials directory and
also reads OpenCode's auth.json as a fallback so users who already connected
Z.AI there can use the same plan without re-entering the key.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.profile import get_flowly_home

DEFAULT_ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_ZAI_CODING_MODEL = "glm-5.2"
FLOWLY_ZAI_CODING_API_KEY_ENV = "FLOWLY_ZAI_CODING_API_KEY"

_KEYRING_SERVICE = "flowly-tui"
_KEYRING_ACCOUNT = "zai-coding"
_OPENCODE_PROVIDER_IDS = (
    "zai",
    "z.ai",
    "z-ai",
    "zhipu",
    "glm",
    "glm-coding",
    "glm_coding",
    "glm-coding-plan",
    "glm_coding_plan",
    "glm_coding_plan_global",
)


class ZaiCodingAuthError(RuntimeError):
    """Base class for GLM Coding Plan credential failures."""


@dataclass(frozen=True)
class ZaiCodingTokenPayload:
    api_key: str
    base_url: str = DEFAULT_ZAI_CODING_BASE_URL
    source: str = "flowly"
    provider_id: str = ""
    updated_at: int = 0

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any] | None,
        *,
        source: str = "flowly",
        provider_id: str = "",
    ) -> "ZaiCodingTokenPayload | None":
        if not isinstance(raw, dict):
            return None
        credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else raw
        key = str(
            credentials.get("api_key")
            or credentials.get("apiKey")
            or credentials.get("key")
            or credentials.get("token")
            or ""
        ).strip()
        if not key:
            return None
        base = str(
            raw.get("base_url")
            or raw.get("baseUrl")
            or credentials.get("base_url")
            or credentials.get("baseURL")
            or DEFAULT_ZAI_CODING_BASE_URL
        )
        try:
            updated_at = int(raw.get("updated_at") or raw.get("updatedAt") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        return cls(
            api_key=key,
            base_url=validate_zai_coding_base_url(base),
            source=source,
            provider_id=provider_id,
            updated_at=updated_at,
        )

    @classmethod
    def from_opencode_entry(
        cls,
        provider_id: str,
        entry: dict[str, Any] | None,
    ) -> "ZaiCodingTokenPayload | None":
        if not isinstance(entry, dict):
            return None
        if str(entry.get("type") or "api").lower() != "api":
            return None
        key = str(entry.get("key") or "").strip()
        if not key:
            return None
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        base = str(
            entry.get("baseURL")
            or entry.get("base_url")
            or metadata.get("baseURL")
            or metadata.get("base_url")
            or metadata.get("baseUrl")
            or DEFAULT_ZAI_CODING_BASE_URL
        )
        return cls(
            api_key=key,
            base_url=validate_zai_coding_base_url(base),
            source="opencode",
            provider_id=provider_id,
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "provider": "zai_coding",
            "base_url": validate_zai_coding_base_url(self.base_url),
            "updated_at": int(self.updated_at or time.time()),
            "credentials": {
                "api_key": self.api_key,
            },
        }


@dataclass(frozen=True)
class ZaiCodingRuntimeCredentials:
    provider: str
    api_key: str
    base_url: str
    auth_mode: str
    source: str = ""
    provider_id: str = ""


def credentials_path() -> Path:
    return get_flowly_home() / "credentials" / "zai_coding.json"


def opencode_auth_json_path() -> Path:
    override = os.getenv("OPENCODE_AUTH_PATH")
    if override and override.strip():
        return Path(override).expanduser()
    xdg_data = os.getenv("XDG_DATA_HOME")
    if xdg_data and xdg_data.strip():
        return Path(xdg_data).expanduser() / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def redact_secret(text: str, *secrets_to_hide: str) -> str:
    result = str(text)
    for secret in secrets_to_hide:
        if isinstance(secret, str) and len(secret) >= 8:
            result = result.replace(secret, "***")
    return result


def validate_zai_coding_base_url(url: str | None) -> str:
    candidate = (url or DEFAULT_ZAI_CODING_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if parsed.scheme != "https":
        raise ZaiCodingAuthError("GLM Coding Plan keys may only be sent to HTTPS endpoints")
    if host != "api.z.ai":
        raise ZaiCodingAuthError(
            f"Refusing to send GLM Coding Plan key to non-Z.AI host: {host or '<empty>'}"
        )
    if path != "/api/coding/paas/v4":
        raise ZaiCodingAuthError(
            "GLM Coding Plan keys must use https://api.z.ai/api/coding/paas/v4"
        )
    return candidate


def _storage_status() -> str:
    if _try_keyring() is not None:
        return "keyring"
    return f"file:{credentials_path()}"


def _try_keyring():
    marker = get_flowly_home() / "credentials" / ".keychain-broken"
    if marker.exists():
        return None
    try:
        import keyring  # type: ignore[import-not-found]
        backend = keyring.get_keyring()
        module = type(backend).__module__ or ""
        if "fail" in module or "null" in module:
            return None
        return keyring
    except Exception:
        return None


def _write_file(raw: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)
    except OSError:
        pass


def _read_file() -> dict[str, Any] | None:
    try:
        raw = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _load_flowly_token_payload() -> ZaiCodingTokenPayload | None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            raw_blob = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception as exc:
            logger.warning("Z.AI Coding keyring read failed, falling back to file: {}", exc)
            raw_blob = None
        if raw_blob:
            try:
                return ZaiCodingTokenPayload.from_raw(json.loads(raw_blob), source="flowly")
            except json.JSONDecodeError:
                return None
    return ZaiCodingTokenPayload.from_raw(_read_file(), source="flowly")


def _read_opencode_auth_json() -> dict[str, Any] | None:
    env_blob = os.getenv("OPENCODE_AUTH_CONTENT")
    if env_blob and env_blob.strip():
        try:
            raw = json.loads(env_blob)
        except json.JSONDecodeError:
            return None
        return raw if isinstance(raw, dict) else None
    try:
        raw = json.loads(opencode_auth_json_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _normalize_provider_id(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _metadata_mentions_zai(provider_id: str, entry: dict[str, Any]) -> bool:
    normalized_id = _normalize_provider_id(provider_id)
    known = {_normalize_provider_id(v) for v in _OPENCODE_PROVIDER_IDS}
    if normalized_id in known:
        return True
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    haystack = " ".join(
        str(v)
        for v in (
            provider_id,
            entry.get("name", ""),
            entry.get("baseURL", ""),
            entry.get("base_url", ""),
            metadata.get("name", ""),
            metadata.get("baseURL", ""),
            metadata.get("baseUrl", ""),
            metadata.get("base_url", ""),
        )
    ).lower()
    return "api.z.ai" in haystack or "z.ai" in haystack


def _load_opencode_token_payload() -> ZaiCodingTokenPayload | None:
    raw = _read_opencode_auth_json()
    if not isinstance(raw, dict):
        return None
    entries = raw.get("providers") if isinstance(raw.get("providers"), dict) else raw

    for provider_id in _OPENCODE_PROVIDER_IDS:
        try:
            payload = ZaiCodingTokenPayload.from_opencode_entry(provider_id, entries.get(provider_id))
        except ZaiCodingAuthError:
            continue
        if payload is not None:
            return payload

    for provider_id, entry in entries.items():
        if not isinstance(provider_id, str) or not isinstance(entry, dict):
            continue
        if not _metadata_mentions_zai(provider_id, entry):
            continue
        try:
            payload = ZaiCodingTokenPayload.from_opencode_entry(provider_id, entry)
        except ZaiCodingAuthError:
            continue
        if payload is not None:
            return payload
    return None


def _load_env_token_payload() -> ZaiCodingTokenPayload | None:
    key = os.getenv(FLOWLY_ZAI_CODING_API_KEY_ENV, "").strip()
    if not key:
        return None
    return ZaiCodingTokenPayload(
        api_key=key,
        base_url=DEFAULT_ZAI_CODING_BASE_URL,
        source="env",
    )


def load_token_payload(*, include_external: bool = True) -> ZaiCodingTokenPayload | None:
    payload = _load_flowly_token_payload()
    if payload is not None:
        return payload
    if include_external:
        payload = _load_opencode_token_payload()
        if payload is not None:
            return payload
        payload = _load_env_token_payload()
        if payload is not None:
            return payload
    return None


def save_token_payload(payload: ZaiCodingTokenPayload) -> str:
    raw = payload.to_raw()
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, json.dumps(raw, separators=(",", ":")))
            try:
                credentials_path().unlink(missing_ok=True)
            except OSError:
                pass
            return _storage_status()
        except Exception as exc:
            logger.warning("Z.AI Coding keyring write failed, falling back to file: {}", exc)
    _write_file(raw)
    return _storage_status()


def save_api_key(api_key: str, *, base_url: str = DEFAULT_ZAI_CODING_BASE_URL) -> str:
    key = api_key.strip()
    if not key:
        raise ZaiCodingAuthError("GLM Coding Plan API key is empty")
    return save_token_payload(
        ZaiCodingTokenPayload(
            api_key=key,
            base_url=validate_zai_coding_base_url(base_url),
            source="flowly",
            updated_at=int(time.time()),
        )
    )


def clear_token_payload() -> None:
    keyring = _try_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception:
            pass
    try:
        credentials_path().unlink(missing_ok=True)
    except OSError:
        pass


def resolve_runtime_credentials(*, config: Any | None = None) -> ZaiCodingRuntimeCredentials | None:
    cfg = getattr(getattr(config, "providers", None), "zai_coding", None)
    if cfg is not None and not getattr(cfg, "enabled", True):
        return None
    payload = load_token_payload()
    if payload is None or not payload.api_key:
        return None
    configured_base = str(getattr(cfg, "api_base", "") or "") if cfg is not None else ""
    base_url = validate_zai_coding_base_url(configured_base or payload.base_url)
    return ZaiCodingRuntimeCredentials(
        provider="zai_coding",
        api_key=payload.api_key,
        base_url=base_url,
        auth_mode="api_key",
        source=payload.source,
        provider_id=payload.provider_id,
    )
