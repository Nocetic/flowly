"""A computer's provider keys reach its named agents (provider-switching.md).

A key is entered once, on the main agent, and every agent on the computer has
it: before the profile host starts a named agent it copies the main agent's
API keys (``providers.<key>.apiKey``) into that agent's own ``config.json``.

* A key the agent lacks is copied.
* A key this copied before follows the main agent: replaced when it changes,
  removed when it is removed there.
* A key the person gave the agent itself (different from what was copied) is
  left alone. One equal to the main agent's counts as copied (an agent made by
  copying the main one, before this existed).

What was copied is remembered as a SHA-256 per provider in the agent's
``provider-keys.json``, never the key. Subscription sign-ins (ChatGPT, Grok)
are not copied: their refresh tokens rotate, and two agents holding one would
log each other out. Every agent reads them from the main agent's store
instead (flowly/auth/shared_signin.py).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loguru import logger

from flowly.profile import _atomic_write_json, _load_config_object, describe_profile

#: Providers whose credential is an API key in ``providers.<key>.apiKey``.
SYNCED_KEY_PROVIDERS: tuple[str, ...] = (
    "anthropic", "openai", "openrouter", "gemini", "groq", "xai", "zhipu", "sakana",
)
RECORD_FILE = "provider-keys.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _api_key(config: dict, provider: str) -> str:
    providers = config.get("providers")
    slot = providers.get(provider) if isinstance(providers, dict) else None
    value = slot.get("apiKey") if isinstance(slot, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _set_api_key(config: dict, provider: str, value: str) -> None:
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = config["providers"] = {}
    slot = providers.get(provider)
    if not isinstance(slot, dict):
        slot = providers[provider] = {}
    slot["apiKey"] = value


def _read_record(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def sync_provider_keys(name: str) -> list[str]:
    """Bring the main agent's API keys into named agent ``name``; the providers
    whose key changed there. Call while the agent is stopped."""
    if name == "default":
        return []
    primary = _load_config_object(describe_profile("default").path / "config.json")
    profile_dir = describe_profile(name).path
    config_path = profile_dir / "config.json"
    record_path = profile_dir / RECORD_FILE
    config = _load_config_object(config_path)
    record = _read_record(record_path)
    recorded = dict(record)
    changed: list[str] = []
    for provider in SYNCED_KEY_PROVIDERS:
        main_key, own_key = _api_key(primary, provider), _api_key(config, provider)
        if main_key == own_key:
            # Already the computer's (an agent made by copying the main one has it too).
            if main_key:
                record[provider] = _digest(main_key)
            continue
        if own_key and record.get(provider) != _digest(own_key):
            record.pop(provider, None)  # the person's own key for this agent
            continue
        _set_api_key(config, provider, main_key)
        if main_key:
            record[provider] = _digest(main_key)
        else:
            record.pop(provider, None)
        changed.append(provider)
    if changed:
        _atomic_write_json(config_path, config)
        logger.info("[Profiles] {} has the computer's keys for {}", name, ", ".join(changed))
    if record != recorded:
        _atomic_write_json(record_path, record)
    return changed
