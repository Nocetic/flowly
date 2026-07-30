"""Small Nostr primitives used by the Buzz channel.

The module implements only what the channel needs:

* Bech32 conversion for ``npub`` / ``nsec`` values.
* BIP-340 Schnorr signing over secp256k1.
* A NIP-42 authentication event.

It deliberately has no network or filesystem access. Keeping the cryptographic
surface isolated makes it straightforward to validate against the official
BIP-340 vectors.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

_FIELD_PRIME = 2**256 - 2**32 - 977
_CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GENERATOR = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_Point = tuple[int, int] | None


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        high = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (high >> index) & 1:
                checksum ^= generator
    return checksum


def _expand_hrp(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _convert_bits(
    values: bytes | list[int],
    source_bits: int,
    target_bits: int,
    *,
    pad: bool,
) -> list[int]:
    accumulator = 0
    bit_count = 0
    converted: list[int] = []
    target_mask = (1 << target_bits) - 1

    for value in values:
        if value < 0 or value >> source_bits:
            raise ValueError("value exceeds source bit width")
        accumulator = (accumulator << source_bits) | value
        bit_count += source_bits
        while bit_count >= target_bits:
            bit_count -= target_bits
            converted.append((accumulator >> bit_count) & target_mask)

    if pad:
        if bit_count:
            converted.append((accumulator << (target_bits - bit_count)) & target_mask)
    elif bit_count >= source_bits or ((accumulator << (target_bits - bit_count)) & target_mask):
        raise ValueError("invalid bech32 padding")
    return converted


def _decode_bech32(value: str, expected_hrp: str) -> bytes:
    if not value or (value.lower() != value and value.upper() != value):
        raise ValueError("bech32 value cannot mix case")
    normalized = value.lower()
    separator = normalized.rfind("1")
    if separator < 1 or separator + 7 > len(normalized):
        raise ValueError("invalid bech32 shape")
    if normalized[:separator] != expected_hrp:
        raise ValueError(f"expected {expected_hrp} prefix")
    try:
        data = [_BECH32_ALPHABET.index(char) for char in normalized[separator + 1 :]]
    except ValueError as exc:
        raise ValueError("invalid bech32 character") from exc
    if _bech32_polymod(_expand_hrp(expected_hrp) + data) != 1:
        raise ValueError("invalid bech32 checksum")
    decoded = _convert_bits(data[:-6], 5, 8, pad=False)
    return bytes(decoded)


def _encode_bech32(hrp: str, payload: bytes) -> str:
    data = _convert_bits(payload, 8, 5, pad=True)
    expanded = _expand_hrp(hrp) + data
    checksum_value = _bech32_polymod(expanded + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(checksum_value >> (5 * (5 - index))) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32_ALPHABET[item] for item in data + checksum)


def hex_to_npub(public_key: str) -> str:
    """Return an ``npub`` for a 32-byte hexadecimal public key."""
    try:
        raw = bytes.fromhex(public_key)
    except ValueError as exc:
        raise ValueError("public key must be hexadecimal") from exc
    if len(raw) != 32:
        raise ValueError("public key must be 32 bytes")
    return _encode_bech32("npub", raw)


def npub_to_hex(npub: str) -> str:
    """Return the hexadecimal public key encoded by ``npub``."""
    raw = _decode_bech32(npub.strip(), "npub")
    if len(raw) != 32:
        raise ValueError("npub must encode 32 bytes")
    return raw.hex()


def decode_private_key(value: str) -> int:
    """Decode an ``nsec`` or 64-character hexadecimal private key."""
    normalized = value.strip()
    if normalized.lower().startswith("nsec1"):
        raw = _decode_bech32(normalized, "nsec")
    else:
        try:
            raw = bytes.fromhex(normalized)
        except ValueError as exc:
            raise ValueError("private key must be nsec or hexadecimal") from exc
    if len(raw) != 32:
        raise ValueError("private key must be 32 bytes")
    secret = int.from_bytes(raw, "big")
    if not 1 <= secret < _CURVE_ORDER:
        raise ValueError("private key is outside the secp256k1 range")
    return secret


def _point_add(left: _Point, right: _Point) -> _Point:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2:
        if (y1 + y2) % _FIELD_PRIME == 0:
            return None
        slope = (3 * x1 * x1) * pow(2 * y1, _FIELD_PRIME - 2, _FIELD_PRIME)
    else:
        slope = (y2 - y1) * pow(x2 - x1, _FIELD_PRIME - 2, _FIELD_PRIME)
    slope %= _FIELD_PRIME
    x3 = (slope * slope - x1 - x2) % _FIELD_PRIME
    return x3, (slope * (x1 - x3) - y1) % _FIELD_PRIME


def _point_multiply(scalar: int, point: _Point = _GENERATOR) -> _Point:
    result: _Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _tagged_hash(tag: str, payload: bytes) -> bytes:
    tag_digest = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_digest + tag_digest + payload).digest()


def public_key_hex(private_key: str) -> str:
    """Derive the x-only BIP-340 public key."""
    point = _point_multiply(decode_private_key(private_key))
    if point is None:
        raise ValueError("private key produced no public point")
    return point[0].to_bytes(32, "big").hex()


def schnorr_sign(
    message: bytes,
    private_key: str,
    *,
    auxiliary_randomness: bytes | None = None,
) -> bytes:
    """Create a BIP-340 Schnorr signature for a 32-byte digest."""
    if len(message) != 32:
        raise ValueError("BIP-340 signs a 32-byte digest")
    secret = decode_private_key(private_key)
    public_point = _point_multiply(secret)
    if public_point is None:
        raise ValueError("private key produced no public point")

    effective_secret = secret if public_point[1] % 2 == 0 else _CURVE_ORDER - secret
    public_x = public_point[0].to_bytes(32, "big")
    randomness = auxiliary_randomness or secrets.token_bytes(32)
    if len(randomness) != 32:
        raise ValueError("auxiliary randomness must be 32 bytes")

    secret_bytes = effective_secret.to_bytes(32, "big")
    aux_hash = _tagged_hash("BIP0340/aux", randomness)
    masked_secret = bytes(a ^ b for a, b in zip(secret_bytes, aux_hash))
    nonce = (
        int.from_bytes(
            _tagged_hash("BIP0340/nonce", masked_secret + public_x + message),
            "big",
        )
        % _CURVE_ORDER
    )
    if nonce == 0:
        raise RuntimeError("BIP-340 nonce is zero")

    nonce_point = _point_multiply(nonce)
    if nonce_point is None:
        raise RuntimeError("BIP-340 nonce has no curve point")
    effective_nonce = nonce if nonce_point[1] % 2 == 0 else _CURVE_ORDER - nonce
    nonce_x = nonce_point[0].to_bytes(32, "big")
    challenge = (
        int.from_bytes(
            _tagged_hash("BIP0340/challenge", nonce_x + public_x + message),
            "big",
        )
        % _CURVE_ORDER
    )
    signature_scalar = (effective_nonce + challenge * effective_secret) % _CURVE_ORDER
    return nonce_x + signature_scalar.to_bytes(32, "big")


def build_auth_event(
    *,
    private_key: str,
    challenge: str,
    relay_url: str,
    auth_tag_json: str = "",
    created_at: int | None = None,
    auxiliary_randomness: bytes | None = None,
) -> dict[str, Any]:
    """Build and sign the NIP-42 kind-22242 authentication event."""
    tags: list[list[str]] = [
        ["relay", relay_url],
        ["challenge", challenge],
    ]
    if auth_tag_json.strip():
        try:
            auth_tag = json.loads(auth_tag_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Buzz auth tag is not valid JSON") from exc
        valid = (
            isinstance(auth_tag, list)
            and len(auth_tag) == 4
            and auth_tag[0] == "auth"
            and all(isinstance(part, str) for part in auth_tag)
            and len(auth_tag[1]) == 64
            and len(auth_tag[3]) == 128
        )
        if not valid:
            raise ValueError("Buzz auth tag must contain four strings and start with 'auth'")
        tags.append(auth_tag)

    public_key = public_key_hex(private_key)
    timestamp = int(time.time()) if created_at is None else int(created_at)
    serialized = json.dumps(
        [0, public_key, timestamp, 22242, tags, ""],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    event_id = hashlib.sha256(serialized).digest()
    return {
        "id": event_id.hex(),
        "pubkey": public_key,
        "created_at": timestamp,
        "kind": 22242,
        "tags": tags,
        "content": "",
        "sig": schnorr_sign(
            event_id,
            private_key,
            auxiliary_randomness=auxiliary_randomness,
        ).hex(),
    }
