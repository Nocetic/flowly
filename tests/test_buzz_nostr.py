from __future__ import annotations

import json

import pytest

from flowly.channels._buzz_nostr import (
    build_auth_event,
    decode_private_key,
    hex_to_npub,
    npub_to_hex,
    public_key_hex,
    schnorr_sign,
)

TEST_PRIVATE_KEY = "00" * 31 + "03"
TEST_PUBLIC_KEY = "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"


def test_schnorr_signature_matches_bip340_vector_zero() -> None:
    signature = schnorr_sign(
        bytes(32),
        TEST_PRIVATE_KEY,
        auxiliary_randomness=bytes(32),
    )

    assert public_key_hex(TEST_PRIVATE_KEY).upper() == TEST_PUBLIC_KEY
    assert signature.hex().upper() == (
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
        "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-private-key",
        "00" * 32,
        "ff" * 32,
        "nsec1qqqqqqqq",
    ],
)
def test_decode_private_key_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        decode_private_key(value)


def test_npub_round_trip() -> None:
    public_key = TEST_PUBLIC_KEY.lower()

    encoded = hex_to_npub(public_key)

    assert encoded.startswith("npub1")
    assert npub_to_hex(encoded) == public_key


def test_build_auth_event_has_nip42_shape_and_optional_owner_tag() -> None:
    owner_tag = ["auth", "b" * 64, "", "c" * 128]

    event = build_auth_event(
        private_key=TEST_PRIVATE_KEY,
        challenge="relay-challenge",
        relay_url="wss://relay.example",
        auth_tag_json=json.dumps(owner_tag),
        created_at=1_700_000_000,
        auxiliary_randomness=bytes(32),
    )

    assert event["kind"] == 22242
    assert event["pubkey"].upper() == TEST_PUBLIC_KEY
    assert event["created_at"] == 1_700_000_000
    assert ["relay", "wss://relay.example"] in event["tags"]
    assert ["challenge", "relay-challenge"] in event["tags"]
    assert owner_tag in event["tags"]
    assert len(event["id"]) == 64
    assert len(event["sig"]) == 128


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        json.dumps({"auth": "wrong shape"}),
        json.dumps(["auth", "too-short"]),
        json.dumps(["auth", "too-short", "", "also-too-short"]),
        json.dumps(["not-auth", "b" * 64, "", "c" * 128]),
    ],
)
def test_build_auth_event_rejects_malformed_owner_tag(raw: str) -> None:
    with pytest.raises(ValueError):
        build_auth_event(
            private_key=TEST_PRIVATE_KEY,
            challenge="c",
            relay_url="wss://relay.example",
            auth_tag_json=raw,
        )
