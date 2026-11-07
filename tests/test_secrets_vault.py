"""The secrets vault, its envelope, names and redaction."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import sys
from pathlib import Path

import pytest

from flowly.account import token_store
from flowly.secrets import envelope, redact, requests as secret_requests, service, vault as vault_mod
from flowly.secrets.names import SecretNameError, check_name


class FakeKeyring:
    """A keychain in memory; ``broken`` makes every call raise as a refused keychain does."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.broken = False

    def get_password(self, service_name: str, account: str) -> str | None:
        if self.broken:
            raise RuntimeError("(-25293, 'refused')")
        return self.items.get((service_name, account))

    def set_password(self, service_name: str, account: str, value: str) -> None:
        if self.broken:
            raise RuntimeError("(-25293, 'refused')")
        self.items[(service_name, account)] = value


@pytest.fixture
def keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(token_store, "_KEYRING_DISABLED", None)
    fake = FakeKeyring()
    monkeypatch.setattr(vault_mod, "_keyring", lambda: fake)
    monkeypatch.setattr(token_store, "disable_keyring_for", lambda *a, **k: None)
    vault_mod._reset_for_tests()
    secret_requests._reset_for_tests()
    redact._reset_for_tests()
    yield fake
    vault_mod._reset_for_tests()
    secret_requests._reset_for_tests()
    redact._reset_for_tests()


@pytest.fixture
def no_keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(vault_mod, "_keyring", lambda: None)
    vault_mod._reset_for_tests()
    secret_requests._reset_for_tests()
    redact._reset_for_tests()
    yield
    vault_mod._reset_for_tests()
    secret_requests._reset_for_tests()
    redact._reset_for_tests()


def _seal_for(values: dict[str, str], request_id: str | None = None, description: str | None = None) -> dict:
    key = service.status()["key"]
    public = envelope.unb64url(key["publicKey"])
    return {"names": list(values), "kid": key["kid"], "requestId": request_id,
            "sealed": envelope.seal(values, public, kid=key["kid"], request_id=request_id, description=description)}


# ── names ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "FAL_KEY", "AWS_SECRET_ACCESS_KEY", "X1"])
def test_names_a_secret_may_have(name):
    assert check_name(name) == name


@pytest.mark.parametrize("name", ["", "a_key", "1KEY", "K", "PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
                                  "PYTHONPATH", "NODE_OPTIONS", "FLOWLY_AGENT_SECRET", "LC_ALL", "BASH_ENV",
                                  "KEY-NAME", "A" * 65, None, 5])
def test_names_no_secret_may_have(name):
    with pytest.raises(SecretNameError):
        check_name(name)


# ── vault ──────────────────────────────────────────────────────────────

def test_a_saved_value_comes_back_and_the_file_holds_no_value(keychain):
    vault = vault_mod.get_vault()
    vault.put({"FAL_KEY": "fal-secret-value-123"}, "Images")
    vault_mod._reset_for_tests()
    data = vault_mod.get_vault().load()
    assert data.secrets["FAL_KEY"].value == "fal-secret-value-123"
    assert data.secrets["FAL_KEY"].description == "Images"
    raw = vault.path.read_bytes()
    assert b"fal-secret-value-123" not in raw and b"FAL_KEY" not in raw
    assert json.loads(raw)["keySource"] == "keyring"
    assert not vault.key_path.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600


def test_without_a_keychain_the_key_is_an_owner_only_file(no_keychain):
    vault = vault_mod.get_vault()
    vault.put({"FAL_KEY": "fal-secret-value-123"})
    assert json.loads(vault.path.read_text())["keySource"] == "file"
    assert vault.key_path.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(vault.key_path.stat().st_mode) == 0o600
    vault_mod._reset_for_tests()
    assert vault_mod.get_vault().load().secrets["FAL_KEY"].value == "fal-secret-value-123"


def test_a_keychain_vault_never_falls_back_to_a_file(keychain, monkeypatch):
    vault_mod.get_vault().put({"FAL_KEY": "fal-secret-value-123"})
    vault_mod._reset_for_tests()
    monkeypatch.setattr(vault_mod, "_keyring", lambda: None)
    vault = vault_mod.get_vault()
    with pytest.raises(vault_mod.SecretsLocked):
        vault.load()
    with pytest.raises(vault_mod.SecretsLocked):
        vault.put({"OTHER_KEY": "another-value-456"})
    assert not vault.key_path.exists()


def test_a_refusing_keychain_locks_the_vault(keychain):
    vault_mod.get_vault().put({"FAL_KEY": "fal-secret-value-123"})
    vault_mod._reset_for_tests()
    keychain.broken = True
    with pytest.raises(vault_mod.SecretsLocked):
        vault_mod.get_vault().load()


def test_a_vault_this_key_cannot_open_is_never_overwritten(keychain):
    vault = vault_mod.get_vault()
    vault.put({"FAL_KEY": "fal-secret-value-123"})
    before = vault.path.read_bytes()
    vault_mod._reset_for_tests()
    service_name = next(iter(keychain.items))
    keychain.items[service_name] = base64.b64encode(b"\x01" * 32).decode()
    vault = vault_mod.get_vault()
    with pytest.raises(vault_mod.SecretsUnreadable):
        vault.load()
    with pytest.raises(vault_mod.SecretsUnreadable):
        vault.put({"OTHER_KEY": "another-value-456"})
    assert vault.path.read_bytes() == before


def test_delete_and_the_limit(keychain, monkeypatch):
    vault = vault_mod.get_vault()
    vault.put({"FAL_KEY": "fal-secret-value-123"})
    assert vault.delete("FAL_KEY") is True
    assert vault.delete("FAL_KEY") is False
    monkeypatch.setattr(vault_mod, "MAX_SECRETS", 1)
    vault.put({"ONE_KEY": "value-number-one"})
    with pytest.raises(vault_mod.SecretsError) as error:
        vault.put({"TWO_KEY": "value-number-two"})
    assert error.value.code == "LIMIT_REACHED"
    vault.put({"ONE_KEY": "value-number-one-changed"})  # replacing is not a new one


# ── envelope ───────────────────────────────────────────────────────────

def test_sealed_values_open_only_for_their_key_request_and_names(keychain):
    data = vault_mod.get_vault().load_or_create()
    sealed = envelope.seal({"A_KEY": "value-of-a"}, data.public_key, kid=data.kid, request_id="sr_1")
    opened = envelope.open_sealed(sealed, data.private_key, kid=data.kid, request_id="sr_1", names=["A_KEY"])
    assert opened.values == {"A_KEY": "value-of-a"}
    for kid, request_id, names in [("0" * 16, "sr_1", ["A_KEY"]), (data.kid, "sr_2", ["A_KEY"]),
                                   (data.kid, None, ["A_KEY"]), (data.kid, "sr_1", ["B_KEY"])]:
        with pytest.raises(envelope.EnvelopeError):
            envelope.open_sealed(sealed, data.private_key, kid=kid, request_id=request_id, names=names)
    tampered = envelope.b64url(bytearray(envelope.unb64url(sealed)) + b"x")
    with pytest.raises(envelope.EnvelopeError):
        envelope.open_sealed(tampered, data.private_key, kid=data.kid, request_id="sr_1", names=["A_KEY"])


def test_the_shared_test_vectors_open():
    """The vectors Desktop, iOS and Android seal against (tests/fixtures/secrets_hpke_vectors.json)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    vectors = json.loads((Path(__file__).parent / "fixtures" / "secrets_hpke_vectors.json").read_text())
    assert vectors["vectors"]
    for vector in vectors["vectors"]:
        private_key = X25519PrivateKey.from_private_bytes(envelope.unb64url(vector["privateKey"]))
        assert envelope.key_id(envelope.public_key_bytes(private_key)) == vector["kid"]
        opened = envelope.open_sealed(vector["sealed"], private_key, kid=vector["kid"],
                                      request_id=vector["requestId"], names=vector["names"])
        assert opened.values == vector["values"]
        assert opened.description == vector.get("description")


# ── service ────────────────────────────────────────────────────────────

def test_submit_from_settings_saves_and_lists_without_values(keychain):
    result = asyncio.run(service.submit(_seal_for({"FAL_KEY": "fal-secret-value-123"}, description="Images")))
    assert result == {"saved": ["FAL_KEY"]}
    listed = service.list_secrets()
    assert [s["name"] for s in listed["secrets"]] == ["FAL_KEY"]
    assert "fal-secret-value-123" not in json.dumps(listed)
    assert listed["secrets"][0]["description"] == "Images"


@pytest.mark.parametrize("change, code", [
    ({"kid": "0" * 16}, "KEY_CHANGED"),
    ({"names": ["PATH"]}, "BAD_NAME"),
    ({"names": ["FAL_KEY", "FAL_KEY"]}, "BAD_NAME"),
    ({"requestId": "sr_missing"}, "REQUEST_NOT_FOUND"),
    ({"sealed": "AAAA"}, "DECRYPT_FAILED"),
])
def test_submit_refusals(keychain, change, code):
    params = {**_seal_for({"FAL_KEY": "fal-secret-value-123"}), **change}
    with pytest.raises(vault_mod.SecretsError) as error:
        asyncio.run(service.submit(params))
    assert error.value.code == code
    assert service.saved_names() == []


@pytest.mark.parametrize("value, code", [
    ("4242 4242 4242 4242", "LOOKS_LIKE_CARD"),
    ("   ", "EMPTY_VALUE"),
    ("x" * (17 * 1024), "TOO_LARGE"),
])
def test_values_that_are_not_saved(keychain, value, code):
    with pytest.raises(vault_mod.SecretsError) as error:
        asyncio.run(service.submit(_seal_for({"SOME_KEY": value})))
    assert error.value.code == code


def test_card_check():
    assert service.looks_like_card("4111-1111-1111-1111")
    assert not service.looks_like_card("4111111111111112")
    assert not service.looks_like_card("sk-or-v1-123")


def test_command_env_marks_provider_names_to_pass_the_scrub(keychain):
    from flowly.exec.env_scrub import sanitize_subprocess_env

    vault_mod.get_vault().put({"OPENROUTER_API_KEY": "sk-or-v1-person-own", "FAL_KEY": "fal-secret-value-123"})
    env = sanitize_subprocess_env({"OPENROUTER_API_KEY": "flowly-configured-key"}, service.forced_env())
    assert env["OPENROUTER_API_KEY"] == "sk-or-v1-person-own"
    assert env["FAL_KEY"] == "fal-secret-value-123"
    assert sanitize_subprocess_env({"OPENROUTER_API_KEY": "flowly-configured-key"}, {}).get("OPENROUTER_API_KEY") is None


def test_command_env_is_empty_when_locked(keychain, monkeypatch):
    vault_mod.get_vault().put({"FAL_KEY": "fal-secret-value-123"})
    vault_mod._reset_for_tests()
    monkeypatch.setattr(vault_mod, "_keyring", lambda: None)
    assert service.command_env() == {}


# ── redaction ──────────────────────────────────────────────────────────

def test_redaction_hides_the_value_and_its_encodings(keychain):
    value = "sk-or-v1-abc/def+ghi=="
    vault_mod.get_vault().put({"OPENROUTER_API_KEY": value, "PIN_CODE": "1234"})
    from urllib.parse import quote

    text = "\n".join([f"OPENROUTER_API_KEY={value}", base64.b64encode(value.encode()).decode(),
                      base64.urlsafe_b64encode(value.encode()).decode(), quote(value, safe=""), "pin 1234"])
    hidden = redact.redact(text)
    assert value not in hidden
    assert base64.b64encode(value.encode()).decode().rstrip("=") not in hidden
    assert quote(value, safe="") not in hidden
    assert hidden.count("[secret:OPENROUTER_API_KEY]") == 4
    assert "pin 1234" in hidden  # too short to hide


def test_redaction_follows_the_vault(keychain):
    vault = vault_mod.get_vault()
    assert redact.redact("value-of-the-key") == "value-of-the-key"
    vault.put({"A_KEY": "value-of-the-key"})
    assert redact.redact("value-of-the-key") == "[secret:A_KEY]"
    vault.delete("A_KEY")
    assert redact.redact("value-of-the-key") == "value-of-the-key"


def test_a_cut_never_leaves_part_of_a_value(keychain):
    value = "sk-or-v1-0123456789abcdefghij"
    vault_mod.get_vault().put({"OPENROUTER_API_KEY": value})
    text = f"key is {value} and again {value}"
    for cut in range(len(text)):
        for shown in (text[cut:], text[:cut]):
            hidden = redact.redact(shown)
            for size in range(redact.MIN_EDGE_CHARS, len(value) + 1):
                for start in range(0, len(value) - size + 1):
                    part = value[start:start + size]
                    if part in hidden:
                        raise AssertionError(f"{part!r} left in {hidden!r}")


def test_a_long_value_is_hidden_at_a_cut_quickly(keychain):
    import time as _time

    value = "".join(chr(65 + (i * 7) % 26) for i in range(16_000))
    vault_mod.get_vault().put({"BIG_CERT": value})
    text = "x" * 50_000 + value[:9_000]
    started = _time.monotonic()
    assert redact.redact(text).endswith("[secret:BIG_CERT]")
    assert redact.redact(value[-7_000:] + "tail").startswith("[secret:BIG_CERT]")
    assert _time.monotonic() - started < 1.0


def test_an_approval_says_which_saved_secrets_a_command_names(keychain):
    from flowly.exec.approvals import _secret_reasons

    vault_mod.get_vault().put({"OPENROUTER_API_KEY": "sk-or-v1-person-own", "FAL_KEY": "fal-secret-value-123"})
    assert _secret_reasons('curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai') == [
        "Uses saved secret OPENROUTER_API_KEY"]
    assert _secret_reasons("echo ${FAL_KEY} $env:OPENROUTER_API_KEY") == [
        "Uses saved secret FAL_KEY", "Uses saved secret OPENROUTER_API_KEY"]
    assert _secret_reasons("echo $FAL_KEYS_DIR %FAL_KEY%") == ["Uses saved secret FAL_KEY"]
    assert _secret_reasons("ls -la") == []


def test_a_log_served_to_a_client_hides_saved_values(keychain):
    from flowly.gateway_logs.redact import redact as redact_log

    vault_mod.get_vault().put({"FAL_KEY": "fal-plain-shaped-value"})
    assert "fal-plain-shaped-value" not in redact_log("request failed for key fal-plain-shaped-value", home="/x")
