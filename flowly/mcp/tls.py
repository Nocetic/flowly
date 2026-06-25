"""TLS / mTLS configuration for remote (HTTP/SSE) MCP servers (Faz 2c).

Two config knobs, both optional:

* ``ssl_verify`` — ``True`` (default, verify against system CAs),
  ``False`` (disable verification — discouraged), or a path string to a
  custom CA bundle.
* ``client_cert`` / ``client_key`` — client certificate for mutual TLS.
  Accepts a single combined PEM path, separate cert + key paths, or a
  ``[cert, key]`` / ``[cert, key, password]`` list form.

:func:`make_http_client_factory` returns an ``httpx`` client factory
matching the MCP SDK's ``McpHttpClientFactory`` signature, with the
resolved ``cert`` / ``verify`` baked in. It mirrors the SDK's defaults
(``follow_redirects=True``, 30s/300s timeouts) so behavior only differs
in the TLS material.
"""

from __future__ import annotations

import os
from typing import Any


def _expand(path: Any, label: str, server_name: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            f"MCP server '{server_name}': {label} must be a non-empty path "
            f"(got {type(path).__name__})"
        )
    expanded = os.path.expanduser(path.strip())
    if not os.path.isfile(expanded):
        raise FileNotFoundError(
            f"MCP server '{server_name}': {label} not found at {expanded!r}"
        )
    return expanded


def resolve_client_cert(server_name: str, cfg: dict) -> Any | None:
    """Return the value for ``httpx``'s ``cert=`` param, or ``None``.

    Forms accepted (see module docstring):
      - ``None`` when neither key is set.
      - a single PEM path string (cert+key combined),
      - ``(cert, key)`` tuple,
      - ``(cert, key, password)`` tuple.
    """
    raw_cert = cfg.get("client_cert")
    raw_key = cfg.get("client_key")
    if not raw_cert and not raw_key:
        return None

    if isinstance(raw_cert, (list, tuple)):
        if raw_key:
            raise ValueError(
                f"MCP server '{server_name}': use client_cert=[cert, key] OR "
                f"client_cert + client_key, not both"
            )
        items = list(raw_cert)
        if len(items) == 2:
            return (_expand(items[0], "client_cert[0]", server_name),
                    _expand(items[1], "client_cert[1]", server_name))
        if len(items) == 3:
            cert = _expand(items[0], "client_cert[0]", server_name)
            key = _expand(items[1], "client_cert[1]", server_name)
            password = items[2]
            if not isinstance(password, str):
                raise ValueError(
                    f"MCP server '{server_name}': client_cert[2] (key password) "
                    f"must be a string"
                )
            return (cert, key, password)
        raise ValueError(
            f"MCP server '{server_name}': client_cert list form needs 2 or 3 "
            f"elements (got {len(items)})"
        )

    cert = _expand(raw_cert, "client_cert", server_name)
    if raw_key:
        return (cert, _expand(raw_key, "client_key", server_name))
    return cert


def resolve_verify(server_name: str, cfg: dict) -> Any:
    """Return the value for ``httpx``'s ``verify=`` param.

    ``True`` (default), ``False``, or a CA-bundle path. A path that
    doesn't exist raises so misconfiguration fails loudly.
    """
    raw = cfg.get("ssl_verify", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        lowered = stripped.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        # Treat as a CA bundle path.
        expanded = os.path.expanduser(stripped)
        if not os.path.exists(expanded):
            raise FileNotFoundError(
                f"MCP server '{server_name}': ssl_verify CA bundle not found at "
                f"{expanded!r}"
            )
        return expanded
    return True


def needs_custom_tls(cfg: dict) -> bool:
    """True if the config sets any cert/verify knob away from defaults."""
    if cfg.get("client_cert") or cfg.get("client_key"):
        return True
    verify = cfg.get("ssl_verify", True)
    return verify is not True and verify not in ("true", "True", "1", "yes")


def make_http_client_factory(server_name: str, cfg: dict) -> Any:
    """Build an ``McpHttpClientFactory`` carrying resolved TLS material.

    The returned callable matches the SDK signature
    ``(headers, timeout, auth) -> httpx.AsyncClient`` and mirrors the
    SDK's defaults so only the TLS behavior differs.
    """
    import httpx

    cert = resolve_client_cert(server_name, cfg)
    verify = resolve_verify(server_name, cfg)

    def _factory(headers=None, timeout=None, auth=None):
        kwargs: dict[str, Any] = {"follow_redirects": True, "verify": verify}
        if cert is not None:
            kwargs["cert"] = cert
        if timeout is None:
            kwargs["timeout"] = httpx.Timeout(30.0, read=300.0)
        else:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory
