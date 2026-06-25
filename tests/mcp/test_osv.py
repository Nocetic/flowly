"""Tests for the OSV malware gate (Faz 3, S6).

We mock the OSV HTTP query so no network is touched, and verify:
- A MAL-* advisory blocks the spawn (returns a message).
- A clean package allows (None).
- Network errors fail-open (None).
- Non-npx/uvx commands skip entirely (None).
- npm scoped/unscoped + PyPI package parsing.
"""

from __future__ import annotations

import pytest

import flowly.mcp.osv as osv


def test_clean_package_allows(monkeypatch):
    monkeypatch.setattr(osv, "_query_osv", lambda p, e, v=None: [])
    assert osv.check_package_for_malware("npx", ["-y", "@upstash/context7-mcp"]) is None


def test_malware_blocks(monkeypatch):
    monkeypatch.setattr(
        osv, "_query_osv",
        lambda p, e, v=None: [{"id": "MAL-2024-1234", "summary": "evil postinstall"}],
    )
    msg = osv.check_package_for_malware("npx", ["-y", "evil-pkg"])
    assert msg is not None
    assert "BLOCKED" in msg
    assert "MAL-2024-1234" in msg


def test_network_error_fails_open(monkeypatch):
    def _boom(p, e, v=None):
        raise OSError("network down")
    monkeypatch.setattr(osv, "_query_osv", _boom)
    assert osv.check_package_for_malware("npx", ["-y", "some-pkg"]) is None


def test_non_package_manager_skips(monkeypatch):
    # A local/absolute command is never checked (and _query_osv must not
    # even be reached).
    called = {"n": 0}
    monkeypatch.setattr(osv, "_query_osv", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    assert osv.check_package_for_malware("/usr/local/bin/myserver", ["--flag"]) is None
    assert osv.check_package_for_malware("python", ["server.py"]) is None
    assert called["n"] == 0


def test_no_package_token_skips(monkeypatch):
    monkeypatch.setattr(osv, "_query_osv", lambda *a, **k: [])
    # Only flags, no package → nothing to check.
    assert osv.check_package_for_malware("npx", ["-y"]) is None


def test_ecosystem_inference():
    assert osv._infer_ecosystem("npx") == "npm"
    assert osv._infer_ecosystem("/opt/homebrew/bin/uvx") == "PyPI"
    assert osv._infer_ecosystem("pipx") == "PyPI"
    assert osv._infer_ecosystem("node") is None


def test_npm_package_parsing():
    assert osv._parse_npm_package("@upstash/context7-mcp") == ("@upstash/context7-mcp", None)
    assert osv._parse_npm_package("@scope/name@1.2.3") == ("@scope/name", "1.2.3")
    assert osv._parse_npm_package("server-everything") == ("server-everything", None)
    assert osv._parse_npm_package("pkg@2.0.0") == ("pkg", "2.0.0")
    assert osv._parse_npm_package("pkg@latest") == ("pkg", None)


def test_pypi_package_parsing():
    assert osv._parse_pypi_package("mcp-server-time") == ("mcp-server-time", None)
    assert osv._parse_pypi_package("pkg==1.0.0") == ("pkg", "1.0.0")
    assert osv._parse_pypi_package("pkg[extra]==2.0") == ("pkg", "2.0")
