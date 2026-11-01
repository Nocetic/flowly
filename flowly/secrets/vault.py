"""This agent's saved secrets, encrypted on disk under a key the OS keeps.

``<FLOWLY_HOME>/secrets/vault.json`` holds the values and this agent's HPKE
private key, encrypted with AES-256-GCM under a random 32-byte vault key. The
vault key lives in the OS keychain (macOS Keychain, Windows Credential
Manager, Secret Service on Linux) through the same probe and latch as the
account token (``flowly.account.token_store``). Where there is no keychain
(a cloud computer, a headless Linux, a sandboxed developer CLI) it is an
owner-only file next to the vault, and the protection is the machine's.

A vault opened with the keychain never falls back: if the keychain cannot be
read later the vault is locked (:class:`SecretsLocked`); it is not re-keyed and
nothing is written to a file. A vault that cannot be read is never overwritten.

Each agent has its own home, so its own vault and key: a named agent runs in
a process of its own and cannot read another's (``flowly.profile_host``).
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

from flowly.secrets import envelope

VAULT_FILE = "vault.json"
KEY_FILE = "vault.key"
KEYRING_SERVICE = "flowly-secrets"
KEYRING_ACCOUNT = "vault-key"
_AAD = b"flowly-vault/v1"

MAX_VALUE_BYTES = 16 * 1024
MAX_DESCRIPTION_CHARS = 500
MAX_SECRETS = 100
#: A value this long or longer shows its last :data:`HINT_CHARS` characters in
#: Settings ("…a1f"): enough to tell which key is saved, never enough to use it.
#: A shorter one shows nothing.
HINT_MIN_CHARS = 12
HINT_CHARS = 3
#: ``lastUsedAt`` is written at most this often per secret: not a write per command.
LAST_USED_RESOLUTION_SECONDS = 3600


class SecretsError(Exception):
    """A structured failure: ``code`` for the wire, ``str()`` for people."""

    code = "SECRETS_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class SecretsLocked(SecretsError):
    code = "SECRETS_LOCKED"


class SecretsUnreadable(SecretsError):
    code = "SECRETS_UNREADABLE"


@dataclass
class Secret:
    value: str
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float | None = None

    def summary(self, name: str) -> dict[str, Any]:
        """What may leave the vault: everything but the value, and the value's last characters as a hint."""
        return {"name": name, "description": self.description, "createdAt": self.created_at,
                "updatedAt": self.updated_at, "lastUsedAt": self.last_used_at, "hint": value_hint(self.value)}


def value_hint(value: str) -> str | None:
    """The last :data:`HINT_CHARS` characters of a long enough value; None for a short one."""
    return value[-HINT_CHARS:] if len(value) >= HINT_MIN_CHARS else None


@dataclass
class VaultData:
    private_key: X25519PrivateKey
    secrets: dict[str, Secret] = field(default_factory=dict)

    @property
    def public_key(self) -> bytes:
        return envelope.public_key_bytes(self.private_key)

    @property
    def kid(self) -> str:
        return envelope.key_id(self.public_key)

    def key_wire(self) -> dict[str, str]:
        return {"kid": self.kid, "publicKey": envelope.b64url(self.public_key)}


def _keyring():
    from flowly.account.token_store import available_keyring

    return available_keyring()


def _keyring_service() -> str:
    from flowly.profile import credential_scope_suffix

    suffix = credential_scope_suffix()
    return f"{KEYRING_SERVICE}:{suffix}" if suffix else KEYRING_SERVICE


class Vault:
    """One home's vault. Thread-safe; one instance per home (:func:`get_vault`)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.RLock()
        self._key: bytes | None = None
        self._cache: tuple[tuple[int, int], VaultData] | None = None

    @property
    def path(self) -> Path:
        return self.directory / VAULT_FILE

    @property
    def key_path(self) -> Path:
        return self.directory / KEY_FILE

    # ── reading ────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return self.path.exists()

    def _header(self) -> dict[str, Any]:
        try:
            header = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretsUnreadable("The secrets file cannot be read.") from exc
        if not isinstance(header, dict) or header.get("v") != 1 or header.get("keySource") not in ("keyring", "file"):
            raise SecretsUnreadable("The secrets file is not one this version knows.")
        return header

    def _read_key(self, source: str) -> bytes:
        if self._key is not None:
            return self._key
        if source == "keyring":
            keyring = _keyring()
            if keyring is None:
                raise SecretsLocked("Saved secrets are locked: this computer's keychain is not available to the agent.")
            try:
                encoded = keyring.get_password(_keyring_service(), KEYRING_ACCOUNT)
            except Exception as exc:
                from flowly.account.token_store import disable_keyring_for

                disable_keyring_for("get_password", exc)
                raise SecretsLocked("Saved secrets are locked: the keychain refused to open them.") from exc
            if not encoded:
                raise SecretsLocked("Saved secrets are locked: their key is missing from the keychain.")
        else:
            try:
                encoded = self.key_path.read_text(encoding="ascii").strip()
            except OSError as exc:
                raise SecretsLocked("Saved secrets are locked: their key file is missing.") from exc
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise SecretsLocked("Saved secrets are locked: their key is damaged.") from exc
        if len(key) != 32:
            raise SecretsLocked("Saved secrets are locked: their key is damaged.")
        self._key = key
        return key

    def load(self) -> VaultData | None:
        """The vault's contents, or ``None`` when nothing was ever saved."""
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                self._cache = None
                return None
            except OSError as exc:
                raise SecretsUnreadable("The secrets file cannot be read.") from exc
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._cache is not None and self._cache[0] == stamp:
                return self._cache[1]
            header = self._header()
            key = self._read_key(header["keySource"])
            try:
                nonce = base64.b64decode(header["nonce"], validate=True)
                ciphertext = base64.b64decode(header["ct"], validate=True)
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
                body = json.loads(plaintext.decode("utf-8"))
                data = _from_json(body)
            except SecretsError:
                raise
            except Exception as exc:
                # A wrong key looks the same as a damaged file: neither is overwritten.
                self._key = None
                raise SecretsUnreadable("The secrets file cannot be opened with this computer's key.") from exc
            self._cache = (stamp, data)
            return data

    # ── writing ────────────────────────────────────────────────────────

    def _new_key(self) -> tuple[bytes, str]:
        """A vault key for a first save: the keychain's if it takes it, else a file's."""
        key = _random.token_bytes(32)
        encoded = base64.b64encode(key).decode("ascii")
        keyring = _keyring()
        if keyring is not None:
            try:
                keyring.set_password(_keyring_service(), KEYRING_ACCOUNT, encoded)
                if keyring.get_password(_keyring_service(), KEYRING_ACCOUNT) == encoded:
                    return key, "keyring"
                logger.warning("[Secrets] the keychain did not keep the vault key; using a key file")
            except Exception as exc:
                from flowly.account.token_store import disable_keyring_for

                disable_keyring_for("set_password", exc)
        from flowly.utils.file_security import secure_file

        _write_atomic(self.key_path, encoded.encode("ascii"))
        secure_file(self.key_path)
        return key, "file"

    def save(self, data: VaultData) -> None:
        with self._lock:
            if self.exists():
                header = self._header()
                source = header["keySource"]
                key = self._read_key(source)
            else:
                self.directory.mkdir(parents=True, exist_ok=True)
                from flowly.utils.file_security import secure_dir

                secure_dir(self.directory)
                key, source = self._new_key()
                self._key = key
            nonce = _random.token_bytes(12)
            plaintext = json.dumps(_to_json(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, _AAD)
            header = {"v": 1, "keySource": source,
                      "nonce": base64.b64encode(nonce).decode("ascii"),
                      "ct": base64.b64encode(ciphertext).decode("ascii")}
            _write_atomic(self.path, json.dumps(header).encode("utf-8"))
            from flowly.utils.file_security import secure_file

            secure_file(self.path)
            stat = self.path.stat()
            self._cache = ((stat.st_mtime_ns, stat.st_size), data)

    def load_or_create(self) -> VaultData:
        """The vault, created (with its key pair) on first use."""
        with self._lock:
            data = self.load()
            if data is None:
                data = VaultData(private_key=X25519PrivateKey.generate())
                self.save(data)
            return data

    def put(self, values: dict[str, str], description: str | None = None) -> list[str]:
        """Save values under their names (checked by the caller); returns the names."""
        with self._lock:
            data = self.load_or_create()
            new = [name for name in values if name not in data.secrets]
            if len(data.secrets) + len(new) > MAX_SECRETS:
                raise SecretsError(f"An agent keeps at most {MAX_SECRETS} secrets.", "LIMIT_REACHED")
            now = time.time()
            secrets = dict(data.secrets)
            for name, value in values.items():
                old = secrets.get(name)
                secrets[name] = Secret(
                    value=value,
                    description=(description if description is not None else (old.description if old else ""))[
                        :MAX_DESCRIPTION_CHARS],
                    created_at=old.created_at if old else now,
                    updated_at=now,
                    last_used_at=old.last_used_at if old else None,
                )
            self.save(VaultData(private_key=data.private_key, secrets=secrets))
            return sorted(values)

    def update(self, name: str, *, description: str | None = None, new_name: str | None = None) -> str:
        """Change a secret's description or name, never its value; returns its name now."""
        with self._lock:
            data = self.load()
            if data is None or name not in data.secrets:
                raise SecretsError("No secret has this name.", "NOT_FOUND")
            target = new_name or name
            if target != name and target in data.secrets:
                raise SecretsError(f"A secret named {target} is already saved.", "NAME_TAKEN")
            old = data.secrets[name]
            secrets = {key: value for key, value in data.secrets.items() if key != name}
            secrets[target] = Secret(
                value=old.value,
                description=(description if description is not None else old.description)[:MAX_DESCRIPTION_CHARS],
                created_at=old.created_at, updated_at=time.time(), last_used_at=old.last_used_at,
            )
            self.save(VaultData(private_key=data.private_key, secrets=secrets))
            return target

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self.load()
            if data is None or name not in data.secrets:
                return False
            secrets = {key: value for key, value in data.secrets.items() if key != name}
            self.save(VaultData(private_key=data.private_key, secrets=secrets))
            return True

    def mark_used(self, names: list[str]) -> None:
        """Note that a command was given these secrets; written at most hourly per secret."""
        with self._lock:
            try:
                data = self.load()
            except SecretsError:
                return
            if data is None:
                return
            now = time.time()
            stale = [name for name in names if name in data.secrets
                     and (data.secrets[name].last_used_at or 0) < now - LAST_USED_RESOLUTION_SECONDS]
            if not stale:
                return
            secrets = dict(data.secrets)
            for name in stale:
                old = secrets[name]
                secrets[name] = Secret(old.value, old.description, old.created_at, old.updated_at, now)
            try:
                self.save(VaultData(private_key=data.private_key, secrets=secrets))
            except (OSError, SecretsError) as exc:
                logger.warning("[Secrets] last use not recorded ({})", type(exc).__name__)


def _to_json(data: VaultData) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization

    raw = data.private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption())
    return {
        "hpke": {"kid": data.kid, "sk": base64.b64encode(raw).decode("ascii")},
        "secrets": {name: {"value": s.value, "description": s.description, "createdAt": s.created_at,
                           "updatedAt": s.updated_at, "lastUsedAt": s.last_used_at}
                    for name, s in data.secrets.items()},
    }


def _from_json(body: Any) -> VaultData:
    if not isinstance(body, dict) or not isinstance(body.get("hpke"), dict) or not isinstance(body.get("secrets"), dict):
        raise ValueError("vault body")
    private_key = X25519PrivateKey.from_private_bytes(base64.b64decode(body["hpke"]["sk"], validate=True))
    secrets: dict[str, Secret] = {}
    for name, entry in body["secrets"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ValueError("vault entry")
        secrets[name] = Secret(
            value=entry["value"],
            description=str(entry.get("description") or ""),
            created_at=float(entry.get("createdAt") or 0),
            updated_at=float(entry.get("updatedAt") or 0),
            last_used_at=float(entry["lastUsedAt"]) if entry.get("lastUsedAt") is not None else None,
        )
    return VaultData(private_key=private_key, secrets=secrets)


def _write_atomic(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_random.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


_vaults: dict[str, Vault] = {}
_vaults_lock = threading.Lock()


def get_vault() -> Vault:
    """The vault of the running agent's home."""
    from flowly.profile import get_flowly_home

    directory = get_flowly_home() / "secrets"
    with _vaults_lock:
        vault = _vaults.get(str(directory))
        if vault is None:
            vault = _vaults[str(directory)] = Vault(directory)
        return vault


def _reset_for_tests() -> None:
    with _vaults_lock:
        _vaults.clear()
