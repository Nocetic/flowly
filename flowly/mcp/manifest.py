"""Private, bounded discovery hints. A manifest never authorizes execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flowly.mcp.content import mcp_wire_value
from flowly.mcp.security import build_safe_env

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_ENTRIES = 128
MAX_TOOLS = 10_000
_ENTRY = re.compile(r"[0-9a-f]{64}\.json\Z")
_TEMPORARY = re.compile(r"\.tmp-[0-9a-f]{32}\Z")
_RUNTIME_ID = uuid.uuid4().hex


def configuration_fingerprint(name: str, config: dict, home: Path) -> str:
    """Hash all effective connection/policy inputs; never persist raw config."""
    from flowly.config.schema import MCPServerConfig

    normalized = MCPServerConfig.model_validate(config).model_dump()
    try:
        sdk = version("mcp")
    except PackageNotFoundError:
        # Packaged builds without distribution metadata still connect; their
        # hints cannot survive process restart without an authoritative SDK ID.
        sdk = "runtime:" + _RUNTIME_ID
    identity = {
        "name": name, "config": normalized, "profile": str(home.resolve()),
        "sdk": sdk, "python": sys.executable, "cwd": os.getcwd(),
        "environment": build_safe_env(config.get("env")) if not config.get("url") else {},
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MCPManifest:
    tools: tuple[Any, ...]
    capabilities: Any
    observed_at: float


class ManifestStore:
    """One file per exact server name; cross-process locking bounds the cache.

    Directory-relative no-follow operations pin the profile/cache directories.
    Unsupported platforms fail closed and callers perform ordinary discovery.
    """

    def __init__(self, home: Path, name: str, identity: str, ttl: float, *, oauth: bool = False, credential_id: str = ""):
        self.home = home.expanduser().resolve()
        self.name = name
        self.identity = identity
        self.ttl = ttl
        self.oauth = oauth
        if credential_id and not re.fullmatch(r"[a-f0-9]{32}", credential_id):
            raise ValueError("Invalid MCP OAuth credential slot")
        self.credential_id = credential_id
        self.filename = hashlib.sha256(name.encode()).hexdigest() + ".json"
        if not math.isfinite(ttl) or not 0 < ttl <= 604_800:
            raise ValueError("Manifest TTL must be positive and at most seven days")

    def _credential_revision(self) -> str:
        if not self.oauth:
            return "none"
        from flowly.agent.media_files import read_media_file
        from flowly.mcp.schema import sanitize_mcp_name_component

        suffix = "." + self.credential_id if self.credential_id else ""
        path = self.home / "mcp-tokens" / f"{sanitize_mcp_name_component(self.name) or 'server'}{suffix}.json"
        try:
            data = read_media_file(path, (self.home,), MAX_MANIFEST_BYTES)
        except FileNotFoundError:
            return "absent"
        return hashlib.sha256(data).hexdigest()

    @contextmanager
    def _directory(self, *, create: bool):
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("Secure manifest storage is unavailable on this platform")
        if create:
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.home.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in self.home.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            for component in ("cache", "mcp-manifests"):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError("Manifest directories must be private and owned by this user")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _check_file(descriptor: int, limit: int) -> os.stat_result:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > limit
        ):
            raise OSError("Invalid private manifest file")
        return info

    @contextmanager
    def _locked(self, directory: int):
        import fcntl

        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        # Exclusive creation avoids an APFS concurrent O_CREAT/NOFOLLOW race
        # that can return ENOENT even though another writer just created it.
        try:
            descriptor = os.open(".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open(".lock", flags, dir_fd=directory)
        try:
            self._check_file(descriptor, 1)
            deadline = time.monotonic() + 0.5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Manifest cache is busy") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    def _read(self, directory: int) -> dict:
        descriptor = os.open(
            self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as handle:
            self._check_file(handle.fileno(), MAX_MANIFEST_BYTES)
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds the byte limit")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("Manifest must be an object")
        return document

    def load(self) -> MCPManifest | None:
        from mcp.types import Tool

        try:
            with self._directory(create=False) as directory:
                document = self._read(directory)
            observed = document.get("observedAt")
            tools = document.get("tools")
            capabilities = document.get("capabilities")
            if (
                document.get("version") != 1 or document.get("identity") != self.identity
                or document.get("credentials") != self._credential_revision()
                or type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= time.time() - observed <= self.ttl
                or not isinstance(tools, list) or len(tools) > MAX_TOOLS
                or not isinstance(capabilities, dict)
                or set(capabilities) != {"resources", "prompts"}
                or any(type(value) is not bool for value in capabilities.values())
            ):
                return None
            parsed = tuple(Tool.model_validate(item) for item in tools)
            if len({tool.name for tool in parsed}) != len(parsed):
                return None
            return MCPManifest(parsed, SimpleNamespace(**{
                key: SimpleNamespace() if value else None for key, value in capabilities.items()
            }), float(observed))
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def _prune(self, directory: int, incoming: int) -> None:
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if _TEMPORARY.fullmatch(entry.name):
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                        # Every writer holds this lock before creating its
                        # temporary file, so these belong to interrupted writes.
                        os.unlink(entry.name, dir_fd=directory)
                    continue
                if not _ENTRY.fullmatch(entry.name) or entry.name == self.filename:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                    entries.append((info.st_mtime_ns, entry.name, info.st_size))
        entries.sort()
        size, count = sum(info[2] for info in entries) + incoming, len(entries) + 1
        for _, name, length in entries:
            if size <= MAX_CACHE_BYTES and count <= MAX_CACHE_ENTRIES:
                break
            os.unlink(name, dir_fd=directory)
            size -= length
            count -= 1

    def save(
        self, tools: list, capabilities: Any, observed_at: float, *, expected_credentials: str | None = None,
    ) -> bool:
        if len(tools) > MAX_TOOLS or not 0 <= time.time() - observed_at <= self.ttl:
            return False
        credentials = self._credential_revision()
        if expected_credentials is not None and expected_credentials != credentials:
            return False
        document = {
            "version": 1, "identity": self.identity, "credentials": credentials,
            "observedAt": observed_at, "tools": [mcp_wire_value(tool) for tool in tools],
            "capabilities": {key: getattr(capabilities, key, None) is not None
                             for key in ("resources", "prompts")},
        }
        # Bound serialization too; don't construct an arbitrarily large JSON string.
        chunks, size = [], 0
        for chunk in json.JSONEncoder(separators=(",", ":")).iterencode(document):
            encoded = chunk.encode()
            size += len(encoded)
            if size > MAX_MANIFEST_BYTES:
                return False
            chunks.append(encoded)
        if size > MAX_CACHE_BYTES:
            return False
        raw = b"".join(chunks)
        with self._directory(create=True) as directory, self._locked(directory):
            try:
                previous = self._read(directory)
            except (FileNotFoundError, ValueError):
                previous = {}
            timestamp = previous.get("observedAt")
            if type(timestamp) in (int, float) and observed_at < timestamp <= time.time():
                return False  # A slower, older discovery cannot overwrite newer evidence.
            self._prune(directory, size)
            temporary = ".tmp-" + uuid.uuid4().hex
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.filename, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return True
