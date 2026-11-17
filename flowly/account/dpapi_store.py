"""Windows: credentials kept as files encrypted with the user's Windows key (DPAPI).

Windows Credential Manager keeps at most 2560 bytes per credential, and
``keyring`` stores the secret as UTF-16, so 1280 characters. A Flowly account
(an id token and a refresh token) is longer than that. A Windows sign-in on
2026-11-16 failed with error 1783 from ``CredWrite``, turned the keychain off
for every store, and left the tokens in a plain file.

Credential Manager itself encrypts with DPAPI (``CryptProtectData`` for the
signed-in user), which has no size limit. This store calls DPAPI directly and
keeps each secret in its own file, so the protection is the same, without the
limit:

- Only this Windows user, on this computer, can decrypt a file: copied to
  another account or machine it does not open, and is read as missing.
- Each file is bound to its service and account name (DPAPI "entropy"), so a
  file renamed onto another credential does not open either.
- Files sit in ``%LOCALAPPDATA%\\flowly\\credentials``: per user, never roamed
  or synced, outside any Flowly home. A Credential Manager entry belongs to the
  user, not to a home, and service names already carry the home
  (``flowly.profile.credential_scope_suffix``), so this keeps those semantics.

It offers ``keyring``'s three calls, so every credential store goes on calling
``get_password`` / ``set_password`` / ``delete_password`` on whatever
``token_store.available_keyring()`` hands it. A secret this store has not got
yet is read once from Credential Manager, written here and removed there:
nobody signs in again after updating.
"""

from __future__ import annotations

import hashlib
import os
import secrets as _random
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from flowly.utils.file_lock import replace_file

#: What ``storage_status`` and the logs call this store.
DISPLAY_NAME = "Windows data protection (your Windows account)"

_ENTROPY_PREFIX = b"flowly-credential\x00"
# The probe is past Credential Manager's limit, so it proves the reason this exists.
_PROBE = b"x" * 6000

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


# ── DPAPI (ctypes, no third-party dependency) ──────────────────────


def _crypt_functions() -> tuple[Callable[[bytes, bytes], bytes], Callable[[bytes, bytes], bytes]]:
    """``CryptProtectData`` / ``CryptUnprotectData`` for the current user."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signature = [
        ctypes.POINTER(Blob),    # pDataIn
        wintypes.LPCWSTR,        # szDataDescr / ppszDataDescr (unused, NULL)
        ctypes.POINTER(Blob),    # pOptionalEntropy
        ctypes.c_void_p,         # pvReserved
        ctypes.c_void_p,         # pPromptStruct
        wintypes.DWORD,          # dwFlags
        ctypes.POINTER(Blob),    # pDataOut
    ]
    protect_data = crypt32.CryptProtectData
    protect_data.argtypes = signature
    protect_data.restype = wintypes.BOOL
    unprotect_data = crypt32.CryptUnprotectData
    unprotect_data.argtypes = [signature[0], ctypes.c_void_p, *signature[2:]]
    unprotect_data.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    def blob(data: bytes) -> tuple[Any, Any]:
        buffer = ctypes.create_string_buffer(data, len(data) or 1)
        return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def call(function: Any, data: bytes, entropy: bytes) -> bytes:
        # The buffers must outlive the call: the structures only point into them.
        data_in, _data_buffer = blob(data)
        extra, _extra_buffer = blob(entropy)
        data_out = Blob()
        if not function(ctypes.byref(data_in), None, ctypes.byref(extra), None, None,
                        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            local_free(ctypes.cast(data_out.pbData, ctypes.c_void_p))

    return (lambda data, entropy: call(protect_data, data, entropy),
            lambda data, entropy: call(unprotect_data, data, entropy))


class DpapiKeyring:
    """``keyring``'s get/set/delete, as DPAPI-encrypted files in one folder."""

    def __init__(
        self,
        directory: Path,
        protect: Callable[[bytes, bytes], bytes],
        unprotect: Callable[[bytes, bytes], bytes],
        legacy: Any = None,
    ) -> None:
        self.directory = directory
        self._protect = protect
        self._unprotect = unprotect
        #: Credential Manager (``keyring``'s own backend), read once per secret to move it here.
        self._legacy = legacy

    # ``storage_status`` asks the module it gets for its backend, as with ``keyring``.
    def get_keyring(self) -> "DpapiKeyring":
        return self

    def _path(self, service: str, username: str) -> Path:
        name = hashlib.sha256(f"{service}\x00{username}".encode("utf-8")).hexdigest()
        return self.directory / f"{name}.bin"

    @staticmethod
    def _entropy(service: str, username: str) -> bytes:
        return _ENTROPY_PREFIX + f"{service}\x00{username}".encode("utf-8")

    def get_password(self, service: str, username: str) -> str | None:
        path = self._path(service, username)
        try:
            sealed = path.read_bytes()
        except FileNotFoundError:
            return self._move_from_legacy(service, username)
        try:
            return self._unprotect(sealed, self._entropy(service, username)).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Another user's or another computer's file (a restored backup), a damaged one, or
            # DPAPI not ready yet. Read as missing and left in place: the next save replaces it.
            logger.warning("[credentials] {} cannot be opened on this account ({}); reading it as missing",
                           path.name, exc)
            return None

    def set_password(self, service: str, username: str, password: str) -> None:
        sealed = self._protect(password.encode("utf-8"), self._entropy(service, username))
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(service, username)
        tmp = path.with_name(f"{path.name}.{_random.token_hex(4)}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(sealed)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict(tmp)
            replace_file(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete_password(self, service: str, username: str) -> None:
        self._path(service, username).unlink(missing_ok=True)
        self._forget_legacy(service, username)

    # ── one-time move from Credential Manager ──────────────────────

    def _move_from_legacy(self, service: str, username: str) -> str | None:
        if self._legacy is None:
            return None
        try:
            value = self._legacy.get_password(service, username)
        except Exception as exc:  # noqa: BLE001 — a broken old store is just an empty one
            logger.debug("[credentials] Credential Manager read failed: {}", exc)
            return None
        if not value:
            return None
        self.set_password(service, username, value)
        self._forget_legacy(service, username)
        logger.info("[credentials] moved {} from Credential Manager to Windows data protection", service)
        return value

    def _forget_legacy(self, service: str, username: str) -> None:
        if self._legacy is None:
            return
        try:
            self._legacy.delete_password(service, username)
        except Exception:  # noqa: BLE001 — usually "not found"
            pass


def _restrict(path: Path) -> None:
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # owner-only ACL: DPAPI already keeps other users out; this keeps them from deleting
    except OSError:
        pass


def default_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "flowly" / "credentials"


def _legacy_backend() -> Any:
    """``keyring``'s Credential Manager backend, or None."""
    try:
        import keyring  # type: ignore[import-not-found]
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    module = type(backend).__module__ or ""
    if "fail" in module or "null" in module:
        return None
    return backend


def open_store(
    directory: Path | None = None,
    functions: tuple[Callable[[bytes, bytes], bytes], Callable[[bytes, bytes], bytes]] | None = None,
    legacy: Any = None,
    *,
    with_legacy: bool = True,
) -> DpapiKeyring | None:
    """This user's store, once DPAPI has round-tripped a secret Credential Manager could not hold.

    None when DPAPI is not there or does not work: the caller then goes on with
    ``keyring`` as before, so a Windows without it is no worse off than today.
    """
    try:
        protect, unprotect = functions or _crypt_functions()
        entropy = _ENTROPY_PREFIX + b"probe"
        if unprotect(protect(_PROBE, entropy), entropy) != _PROBE:
            raise OSError("DPAPI returned something else than it was given")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[credentials] Windows data protection is not available: {}", exc)
        return None
    if legacy is None and with_legacy:
        legacy = _legacy_backend()
    return DpapiKeyring(directory or default_directory(), protect, unprotect, legacy)


def self_test(directory: Path) -> None:
    """The runtime build's check on a real Windows: a long secret goes in, comes out, and is sealed."""
    store = open_store(directory, with_legacy=False)
    if store is None:
        raise SystemExit("dpapi_store: Windows data protection did not round-trip")
    secret = "t" * 6000  # 12000 bytes as UTF-16: Credential Manager refuses it (1783)
    store.set_password("flowly-self-test", "account", secret)
    sealed = next(directory.glob("*.bin")).read_bytes()
    if secret.encode() in sealed:
        raise SystemExit("dpapi_store: the secret was written in clear")
    if store.get_password("flowly-self-test", "account") != secret:
        raise SystemExit("dpapi_store: the secret did not come back")
    store.delete_password("flowly-self-test", "account")
    if store.get_password("flowly-self-test", "account") is not None:
        raise SystemExit("dpapi_store: the secret was not deleted")
    print("dpapi ok")
