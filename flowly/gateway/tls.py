"""The gateway's own TLS identity: a key and a self-signed certificate.

A gateway reached from other machines (a VPS, a computer at home) encrypts
its connections without a domain name or a certificate authority. On first
use it makes an ECDSA P-256 key and a ten-year self-signed certificate under
``<FLOWLY_HOME>/gateway/tls/`` (owner-only). The apps learn the key's pin
(``sha256/<base64>`` of the certificate's SubjectPublicKeyInfo, the form
HTTP public-key pinning used) when they pair, from the ``flowly://remote``
code or over a connection they already trust, and from then on connect only
to the gateway holding that key: a certificate authority and the host name
play no part, so the address may change and nothing in between can pose as
the gateway.

The pin is of the key, not the certificate: a certificate renewed on the same
key keeps every app paired. A new key (``rotate``) means pairing again.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
VALID_DAYS = 3650


@dataclass(frozen=True)
class TlsIdentity:
    cert_path: Path
    key_path: Path
    pin: str

    @property
    def fingerprint(self) -> str:
        """The pin's first 16 hex digits in groups of four, to compare by eye
        (the CLI prints it; an app shows it when a pin was typed, not scanned)."""
        return short_fingerprint(self.pin)

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_path, self.key_path)
        return context


def tls_dir() -> Path:
    from flowly.profile import get_flowly_home

    return get_flowly_home() / "gateway" / "tls"


def spki_pin(cert_pem: bytes) -> str:
    """``sha256/<base64>`` of a certificate's SubjectPublicKeyInfo."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def short_fingerprint(pin: str) -> str:
    digest = base64.b64decode(pin.split("/", 1)[1])
    hexed = digest.hex().upper()[:16]
    return " ".join(hexed[i:i + 4] for i in range(0, 16, 4))


def _write_private(path: Path, data: bytes) -> None:
    """Owner-only from its first byte; replaced in one step."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)


def _subject_names(hosts: list[str]) -> list[x509.GeneralName]:
    """The addresses it was made for, as information only (apps check the pin)."""
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for host in hosts:
        host = (host or "").strip().strip("[]")
        if not host or host in ("0.0.0.0", "::"):
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def _make(directory: Path, key: ec.EllipticCurvePrivateKey | None, hosts: list[str]) -> None:
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Flowly gateway")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_subject_names(hosts)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    _write_private(directory / KEY_FILE, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write_private(directory / CERT_FILE, cert.public_bytes(serialization.Encoding.PEM))


def _load_key(directory: Path) -> ec.EllipticCurvePrivateKey | None:
    try:
        key = serialization.load_pem_private_key((directory / KEY_FILE).read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    return key if isinstance(key, ec.EllipticCurvePrivateKey) else None


def ensure_identity(hosts: list[str] | None = None, directory: Path | None = None) -> TlsIdentity:
    """This gateway's identity, made on first use.

    A certificate that expired, or one missing beside its key, is made again
    on the same key: the pin, and so every pairing, stays.
    """
    directory = directory or tls_dir()
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    key = _load_key(directory)
    cert_pem: bytes | None = None
    if key is not None:
        try:
            cert_pem = cert_path.read_bytes()
            cert = x509.load_pem_x509_certificate(cert_pem)
            same_key = cert.public_key().public_numbers() == key.public_key().public_numbers()
            soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
            if not same_key or cert.not_valid_after_utc < soon:
                cert_pem = None
        except (OSError, ValueError):
            cert_pem = None
    if key is None or cert_pem is None:
        _make(directory, key, hosts or [])
        cert_pem = cert_path.read_bytes()
    return TlsIdentity(cert_path=cert_path, key_path=key_path, pin=spki_pin(cert_pem))


def rotate(hosts: list[str] | None = None, directory: Path | None = None) -> TlsIdentity:
    """A new key: every app pairs again (the old pin no longer connects)."""
    directory = directory or tls_dir()
    _make(directory, None, hosts or [])
    return ensure_identity(hosts, directory)
