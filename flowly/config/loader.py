"""Configuration loading utilities."""

import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from flowly.config.schema import Config


logger = logging.getLogger(__name__)


def get_config_path() -> Path:
    """Get the default configuration file path (profile-aware)."""
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "config.json"


def get_data_dir() -> Path:
    """Get the flowly data directory."""
    from flowly.utils.helpers import get_data_path
    return get_data_path()


def _backup_path(config_path: Path) -> Path:
    """Sibling ``.bak`` file used for self-heal recovery."""
    return config_path.with_suffix(config_path.suffix + ".bak")


def _try_parse_config(path: Path) -> dict | None:
    """Attempt to read+parse JSON. Returns dict on success, None otherwise.

    Empty files and non-dict roots count as "not parseable" — they would
    fail Pydantic validation later anyway, and the recovery path treats
    them the same as a JSON syntax error.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file with self-healing fallback.

    Recovery flow:
      1. Try parsing ``config.json``. If it parses + validates, return it.
         As a side-effect, if no ``.bak`` exists yet, write one — this
         seeds the safety net for existing installs that predate the
         backup convention.
      2. If parse fails, try ``config.json.bak``. If it parses, move the
         broken original aside as ``config.json.broken-<unix-ts>``,
         restore the backup as ``config.json``, log a warning, and
         return the parsed result.
      3. If both fail (or .bak is missing), fall through to in-code
         ``Config()`` defaults. This is the worst case — secrets are
         lost — but at least the agent boots so the user can re-enter
         them.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    bak = _backup_path(path)

    # Happy path: config.json parses cleanly.
    data = _try_parse_config(path)
    if data is not None:
        try:
            cfg = Config.model_validate(convert_keys(data))
        except ValueError as e:
            # Schema mismatch (e.g. wrong type for a known field). Treat
            # the same as a parse failure for recovery purposes.
            logger.warning(
                "config: schema validation failed for %s: %s — attempting recovery",
                path, e,
            )
        else:
            # First-time bootstrap: existing user has a valid config but
            # no .bak yet. Write one so the next corruption is recoverable.
            if not bak.exists():
                try:
                    shutil.copy2(str(path), str(bak))
                    logger.info("config: seeded backup at %s", bak)
                except OSError as exc:
                    logger.warning("config: backup seed failed: %s", exc)
            return cfg

    # Corrupted: try .bak.
    bak_data = _try_parse_config(bak)
    if bak_data is not None:
        try:
            cfg = Config.model_validate(convert_keys(bak_data))
        except ValueError as exc:
            logger.error(
                "config: backup at %s also fails schema validation (%s) — "
                "falling back to defaults", bak, exc,
            )
        else:
            # Move broken original aside for forensics, then restore.
            broken = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
            try:
                if path.exists():
                    os.replace(str(path), str(broken))
                shutil.copy2(str(bak), str(path))
                logger.warning(
                    "config: recovered from backup; broken original kept at %s",
                    broken,
                )
            except OSError as exc:
                # Even if the rename/copy fails, we still have a valid
                # parsed config in memory — return it so the gateway boots.
                logger.error(
                    "config: recovery rename/copy failed (%s) — using "
                    "in-memory backup", exc,
                )
            return cfg

    # Both gone. Last resort.
    if path.exists() or bak.exists():
        logger.error(
            "config: both %s and %s unusable — using defaults", path, bak,
        )
    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file using read-modify-write with backup rotation.

    Reads the existing JSON first and deep-merges the Pydantic-known fields on
    top of it.  Unknown / extra fields that were added manually are preserved.

    Before writing the new content, copies the previous (parseable)
    config.json to ``config.json.bak`` so :func:`load_config` can recover
    if a future write — or any external process — corrupts the active
    file. We refuse to back up an already-corrupt source: corruption
    must not propagate into the safety net.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Load whatever is already on disk (preserves unknown fields)
    existing: dict[str, Any] = {}
    existing_parseable = False
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_parseable = isinstance(existing, dict)
        except (json.JSONDecodeError, OSError):
            existing = {}

    # 2. Dump only the fields Pydantic knows about, convert to camelCase
    known = convert_to_camel(config.model_dump())

    # 3. Deep-merge: existing is the base, known fields win
    merged = _deep_merge(existing, known)

    # 4. Backup the current parseable file BEFORE overwriting. We
    # deliberately don't back up unparseable content — that would
    # propagate corruption into the recovery slot. If the current file
    # was already corrupt, the .bak left over from the prior healthy
    # save is what we want to keep.
    if existing_parseable:
        bak = _backup_path(path)
        try:
            shutil.copy2(str(path), str(bak))
        except OSError as exc:
            logger.warning("config: backup write failed: %s", exc)

    # 5. Atomic write
    tmp_path = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    try:
        tmp_path.write_text(json.dumps(merged, indent=4), encoding="utf-8")
        os.replace(str(tmp_path), str(path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # Restrict permissions: owner read/write only (config contains API keys)
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*. override wins on conflicts,
    EXCEPT a ``None`` in override never clobbers a non-``None`` existing
    value — that case preserves base.

    Why the ``None`` carve-out: ``save_config`` dumps the full Pydantic
    model, and ``Optional`` fields like ``ProviderConfig.api_base`` (whose
    schema default is ``None``) get serialized as ``None`` even when the
    user explicitly set a value on disk a moment earlier (via the setup
    wizard, manual edit, etc.). Without this guard, a partial save —
    e.g. flipping ``providers.active`` — would silently null out
    ``providers.openrouter.apiBase`` because Pydantic re-emits the
    default. The fix is intentionally one-directional: clearing a value
    to ``None`` is not supported via this merge path; callers that want
    a true clear should write ``""`` or delete the key from the raw
    dict before calling.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        elif val is None and key in result and result[key] is not None:
            # Preserve existing non-null disk value against a None override.
            continue
        else:
            result[key] = val
    return result


# Dict fields whose *values* are free-form, case-sensitive maps whose KEYS
# must survive camel/snake conversion verbatim:
#   - env / headers: flat ``{NAME: value}`` maps (env vars, HTTP headers) —
#     keys like ``GITHUB_PERSONAL_ACCESS_TOKEN`` would otherwise be mangled.
#   - mcp_servers / mcpServers: ``{serverName: serverConfig}`` — the server
#     NAMES must stay verbatim, but each serverConfig's fields are still
#     converted (so connectTimeout↔connect_timeout works), and within a
#     serverConfig its own env/headers fall under the opaque rule again.
_OPAQUE_MAP_KEYS = {"env", "headers"}
_NAMED_CONFIG_MAP_KEYS = {"mcp_servers", "mcpServers"}


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic (key-preserving for maps)."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            sk = camel_to_snake(k)
            if k in _OPAQUE_MAP_KEYS or sk in _OPAQUE_MAP_KEYS:
                out[sk] = v  # verbatim — don't touch the map's own keys
            elif (k in _NAMED_CONFIG_MAP_KEYS or sk in _NAMED_CONFIG_MAP_KEYS) and isinstance(v, dict):
                # Preserve server names; convert each server config's fields.
                out[sk] = {name: convert_keys(cfg) for name, cfg in v.items()}
            else:
                out[sk] = convert_keys(v)
        return out
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase (key-preserving for maps)."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            ck = snake_to_camel(k)
            if k in _OPAQUE_MAP_KEYS:
                out[ck] = v  # verbatim
            elif k in _NAMED_CONFIG_MAP_KEYS and isinstance(v, dict):
                out[ck] = {name: convert_to_camel(cfg) for name, cfg in v.items()}
            else:
                out[ck] = convert_to_camel(v)
        return out
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case.

    Handles acronyms correctly:
      apiKey      → api_key
      APIKey      → api_key
      myAPIKey    → my_api_key
      getHTTPSUrl → get_https_url
      IOError     → io_error
    """
    import re
    # OAuth is commonly written as a mixed-case acronym inside otherwise
    # camelCase JSON keys (e.g. xaiOAuth). Normalize it before the generic
    # acronym pass so it becomes xai_oauth, not xai_o_auth.
    name = name.replace("OAuth", "Oauth")
    # Insert _ before: uppercase followed by lowercase (end of acronym)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    # Insert _ before: lowercase/digit followed by uppercase
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
