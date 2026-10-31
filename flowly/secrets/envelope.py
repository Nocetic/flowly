"""The sealed form a secret travels in, from the person's app to this agent.

The app seals the values to this agent's public key (HPKE, RFC 9180, base mode:
DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-128-GCM). The server between them
carries bytes it cannot open. ``info`` binds the sealed values to the key, the
request and the names they were typed for, so they cannot be replayed as other
names or onto another request.

The wire form is ``base64url(enc || ct)`` without padding; ``enc`` is the
32-byte X25519 encapsulated key. Inside is UTF-8 JSON:
``{"values": {NAME: value}, "description"?: str}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

INFO_PREFIX = "flowly-secret/v1"
ENC_LENGTH = 32
#: The most a sealed body may be: six values of 16 KiB, a description, JSON and base64 overhead.
MAX_SEALED_CHARS = 160_000

_SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_128_GCM)


class EnvelopeError(ValueError):
    """The sealed values could not be opened as what they claim to be."""


@dataclass(frozen=True)
class Opened:
    values: dict[str, str]
    description: str | None


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    if not isinstance(text, str) or not text or len(text) > MAX_SEALED_CHARS:
        raise EnvelopeError("sealed values are missing or too large")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise EnvelopeError("sealed values are not base64url") from exc


def public_key_bytes(private_key: X25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public_key: bytes) -> str:
    """The key's id: the first 16 hex characters of its SHA-256."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def info(kid: str, request_id: str | None, names: list[str]) -> bytes:
    return "|".join((INFO_PREFIX, kid, request_id or "", ",".join(sorted(names)))).encode("utf-8")


def seal(values: dict[str, str], public_key: bytes, *, kid: str, request_id: str | None,
         description: str | None = None) -> str:
    """Seal values as an app does (tests and the shared test vectors)."""
    body: dict = {"values": values}
    if description is not None:
        body["description"] = description
    plaintext = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sealed = _SUITE.encrypt(plaintext, X25519PublicKey.from_public_bytes(public_key),
                            info(kid, request_id, list(values)))
    return b64url(sealed)


def open_sealed(sealed: str, private_key: X25519PrivateKey, *, kid: str, request_id: str | None,
                names: list[str]) -> Opened:
    """Open what an app sealed for exactly these names (and request); else :class:`EnvelopeError`."""
    raw = unb64url(sealed)
    if len(raw) <= ENC_LENGTH:
        raise EnvelopeError("sealed values are too short")
    try:
        plaintext = _SUITE.decrypt(raw, private_key, info(kid, request_id, names))
    except Exception as exc:  # cryptography raises InvalidTag; never say more.
        raise EnvelopeError("sealed values could not be opened") from exc
    try:
        body = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvelopeError("sealed values are not JSON") from exc
    values = body.get("values") if isinstance(body, dict) else None
    if not isinstance(values, dict) or sorted(values) != sorted(names):
        raise EnvelopeError("sealed values do not name what was asked")
    if not all(isinstance(value, str) for value in values.values()):
        raise EnvelopeError("a sealed value is not text")
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        raise EnvelopeError("the description is not text")
    return Opened(values=dict(values), description=description)
