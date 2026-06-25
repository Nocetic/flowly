"""Tests for TLS / mTLS resolution (Faz 2c, T11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.mcp.tls import (
    make_http_client_factory,
    needs_custom_tls,
    resolve_client_cert,
    resolve_verify,
)


@pytest.fixture
def cert_files(tmp_path: Path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca.pem"
    for f in (cert, key, ca):
        f.write_text("-----BEGIN CERT-----\n")
    return cert, key, ca


# ── client cert ─────────────────────────────────────────────────────


def test_no_cert_returns_none():
    assert resolve_client_cert("srv", {}) is None


def test_combined_pem_path(cert_files):
    cert, _, _ = cert_files
    assert resolve_client_cert("srv", {"client_cert": str(cert)}) == str(cert)


def test_cert_plus_key(cert_files):
    cert, key, _ = cert_files
    result = resolve_client_cert("srv", {"client_cert": str(cert), "client_key": str(key)})
    assert result == (str(cert), str(key))


def test_cert_list_form(cert_files):
    cert, key, _ = cert_files
    result = resolve_client_cert("srv", {"client_cert": [str(cert), str(key)]})
    assert result == (str(cert), str(key))


def test_cert_list_with_password(cert_files):
    cert, key, _ = cert_files
    result = resolve_client_cert(
        "srv", {"client_cert": [str(cert), str(key), "secret"]},
    )
    assert result == (str(cert), str(key), "secret")


def test_missing_cert_file_raises():
    with pytest.raises(FileNotFoundError):
        resolve_client_cert("srv", {"client_cert": "/nonexistent/cert.pem"})


def test_cert_list_and_key_together_rejected(cert_files):
    cert, key, _ = cert_files
    with pytest.raises(ValueError):
        resolve_client_cert(
            "srv", {"client_cert": [str(cert), str(key)], "client_key": str(key)},
        )


# ── verify ──────────────────────────────────────────────────────────


def test_verify_default_true():
    assert resolve_verify("srv", {}) is True


def test_verify_false():
    assert resolve_verify("srv", {"ssl_verify": False}) is False
    assert resolve_verify("srv", {"ssl_verify": "false"}) is False


def test_verify_ca_path(cert_files):
    _, _, ca = cert_files
    assert resolve_verify("srv", {"ssl_verify": str(ca)}) == str(ca)


def test_verify_missing_ca_raises():
    with pytest.raises(FileNotFoundError):
        resolve_verify("srv", {"ssl_verify": "/nonexistent/ca.pem"})


# ── needs_custom_tls ────────────────────────────────────────────────


def test_needs_custom_tls_default_false():
    assert needs_custom_tls({}) is False
    assert needs_custom_tls({"ssl_verify": True}) is False


def test_needs_custom_tls_when_verify_off():
    assert needs_custom_tls({"ssl_verify": False}) is True


def test_needs_custom_tls_when_cert_set():
    assert needs_custom_tls({"client_cert": "/x.pem"}) is True


# ── factory ─────────────────────────────────────────────────────────


def test_factory_builds_client_with_verify_off():
    import httpx
    factory = make_http_client_factory("srv", {"ssl_verify": False})
    client = factory(headers={"X": "1"}, timeout=None, auth=None)
    assert isinstance(client, httpx.AsyncClient)


def test_factory_with_cert_is_callable(cert_files):
    # We assert the factory is built and the cert path resolves; we do
    # NOT instantiate the live client here because httpx eagerly loads
    # the cert chain (that's httpx's concern, and would need a real
    # signed cert/key pair to succeed).
    cert, key, _ = cert_files
    cfg = {"client_cert": str(cert), "client_key": str(key)}
    factory = make_http_client_factory("srv", cfg)
    assert callable(factory)
    assert resolve_client_cert("srv", cfg) == (str(cert), str(key))
