"""OSV malware gate for stdio MCP servers (Faz 3, S6).

Before launching an MCP server via ``npx`` / ``uvx`` / ``pipx``, query the
OSV (Open Source Vulnerabilities) API for **malware** advisories (``MAL-*``
IDs) on the package being fetched. Regular CVEs are ignored — only confirmed
malware blocks the spawn.

The API is free, public (Google-maintained), ~300ms typical latency.
**Fail-open**: any network error, timeout, parse failure, or unrecognized
command lets the package proceed — a flaky network must never block the agent.

Gated per-server by ``osv_check`` (default True). Only ``npx``/``uvx``/``pipx``
commands (which fetch and execute remote code) are checked; absolute-path or
local commands skip.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Optional, Tuple


logger = logging.getLogger(__name__)

_OSV_ENDPOINT = os.getenv("OSV_ENDPOINT", "https://api.osv.dev/v1/query")
_TIMEOUT = 10  # seconds


def check_package_for_malware(command: str, args: list) -> Optional[str]:
    """Return a block message if the MCP package has malware advisories, else None.

    Fail-open: returns None (allow) on any error or unrecognized command.
    """
    ecosystem = _infer_ecosystem(command)
    if not ecosystem:
        return None  # not a remote-fetch package manager — skip

    package, version = _parse_package_from_args(args, ecosystem)
    if not package:
        return None

    try:
        malware = _query_osv(package, ecosystem, version)
    except Exception as exc:
        logger.debug(
            "OSV check failed for %s/%s (allowing): %s", ecosystem, package, exc,
        )
        return None

    if malware:
        ids = ", ".join(m["id"] for m in malware[:3])
        summaries = "; ".join(m.get("summary", m["id"])[:100] for m in malware[:3])
        return (
            f"BLOCKED: package '{package}' ({ecosystem}) has known malware "
            f"advisories: {ids}. Details: {summaries}"
        )
    return None


def _infer_ecosystem(command: str) -> Optional[str]:
    base = os.path.basename(str(command or "")).lower()
    if base in {"npx", "npx.cmd"}:
        return "npm"
    if base in {"uvx", "uvx.cmd", "pipx"}:
        return "PyPI"
    return None


def _parse_package_from_args(
    args: list, ecosystem: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (package, version) from command args, skipping flags."""
    if not args:
        return None, None

    package_token = None
    for arg in args:
        if not isinstance(arg, str):
            continue
        if arg.startswith("-"):
            continue
        package_token = arg
        break

    if not package_token:
        return None, None

    if ecosystem == "npm":
        return _parse_npm_package(package_token)
    if ecosystem == "PyPI":
        return _parse_pypi_package(package_token)
    return package_token, None


def _parse_npm_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse ``@scope/name@version`` or ``name@version``."""
    if token.startswith("@"):
        match = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if match:
            return match.group(1), match.group(2)
        return token, None
    if "@" in token:
        parts = token.rsplit("@", 1)
        name = parts[0]
        version = parts[1] if len(parts) > 1 and parts[1] != "latest" else None
        return name, version
    return token, None


def _parse_pypi_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse ``name==version`` or ``name[extras]==version``."""
    match = re.match(r"^([a-zA-Z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token)
    if match:
        return match.group(1), match.group(2)
    return token, None


def _query_osv(
    package: str, ecosystem: str, version: Optional[str] = None,
) -> list:
    """Query OSV for ``MAL-*`` advisories. Returns the malware vulns list."""
    payload: dict = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _OSV_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "flowly-mcp-osv-check/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        result = json.loads(resp.read())

    vulns = result.get("vulns", [])
    return [v for v in vulns if str(v.get("id", "")).startswith("MAL-")]
