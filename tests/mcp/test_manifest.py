"""Persistent discovery hints: identity, bounds, private storage and races."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from mcp.types import Tool, ToolAnnotations

from flowly.mcp import manifest
from flowly.mcp.manifest import ManifestStore, configuration_fingerprint


def tool(name="read", **changes):
    return Tool(name=name, description="Read", inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=True), **changes)


@pytest.fixture
def store(tmp_path):
    return ManifestStore(tmp_path, "example", "identity", 60)


def path(store):
    return store.home / "cache" / "mcp-manifests" / store.filename


def save(store, tools=None, **kwargs):
    return store.save(tools or [tool()], SimpleNamespace(resources=object(), prompts=None),
                      kwargs.get("observed_at", time.time()))


def test_complete_manifest_roundtrip_preserves_schema_and_metadata(store):
    remote = tool(outputSchema={"type": "object"}, _meta={"category": "read"})
    assert save(store, [remote])
    result = store.load()
    assert result.tools[0].model_dump() == remote.model_dump()
    assert result.capabilities.resources is not None
    assert result.capabilities.prompts is None
    assert path(store).stat().st_mode & 0o777 == 0o600
    assert path(store).parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("change", [
    {"version": 2}, {"identity": "different"}, {"credentials": "different"},
    {"observedAt": 0}, {"observedAt": float("nan")}, {"observedAt": True},
    {"observedAt": time.time() + 86400}, {"tools": [{}]}, {"tools": "wrong"},
    {"capabilities": {"resources": True}},
    {"capabilities": {"resources": "yes", "prompts": False}},
])
def test_invalid_or_stale_manifest_is_a_cache_miss(store, change):
    save(store)
    document = json.loads(path(store).read_text())
    document.update(change)
    path(store).write_text(json.dumps(document))
    assert store.load() is None


@pytest.mark.parametrize("value", [-1, 0, 604_801, float("nan"), float("inf")])
def test_ttl_must_be_finite_positive_and_bounded(tmp_path, value):
    with pytest.raises(ValueError):
        ManifestStore(tmp_path, "test", "id", value)


def test_names_do_not_alias_and_do_not_escape_the_profile(tmp_path):
    stores = [ManifestStore(tmp_path, name, "id", 60) for name in ("a-b", "a_b", "../a", "/a")]
    assert len({item.filename for item in stores}) == 4
    for item in stores:
        assert save(item)
        assert path(item).is_relative_to(tmp_path)


def test_cache_does_not_persist_configuration_or_credentials(tmp_path):
    config = {"url": "https://example.test/mcp?secret=opaque-secret-value",
              "headers": {"Authorization": "Bearer example-credential-value"}}
    identity = configuration_fingerprint("private-server", config, tmp_path)
    store = ManifestStore(tmp_path, "private-server", identity, 60)
    assert save(store)
    data = path(store).read_text()
    for value in ("example.test", "opaque-secret-value", "example-credential-value", "private-server"):
        assert value not in data


@pytest.mark.parametrize("change", [
    {"url": "https://other.test/mcp"}, {"args": ["other"]},
    {"headers": {"Authorization": "Bearer changed"}}, {"env": {"TOKEN": "changed"}},
    {"tools": {"exclude": ["read"]}}, {"trust": "untrusted"},
    {"protocol": "legacy"}, {"scope": "write"},
])
def test_identity_binds_all_connection_and_policy_inputs(tmp_path, change):
    config = {"command": "python", "args": ["server.py"]}
    assert configuration_fingerprint("one", config, tmp_path) != configuration_fingerprint(
        "one", config | change, tmp_path,
    )


def test_identity_binds_profile_name_sdk_and_ambient_subprocess_environment(tmp_path, monkeypatch):
    config = {"command": "python"}
    initial = configuration_fingerprint("one", config, tmp_path)
    assert initial != configuration_fingerprint("two", config, tmp_path)
    assert initial != configuration_fingerprint("one", config, tmp_path / "other")
    monkeypatch.setenv("PATH", "/fixture-changed-path")
    assert initial != configuration_fingerprint("one", config, tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(manifest, "version", lambda _: "different-sdk")
    assert initial != configuration_fingerprint("one", config, tmp_path)


def test_explicit_and_implicit_defaults_have_same_identity(tmp_path):
    from flowly.config.schema import MCPServerConfig

    config = {"command": "python"}
    assert configuration_fingerprint("one", config, tmp_path) == configuration_fingerprint(
        "one", MCPServerConfig(**config).model_dump(), tmp_path,
    )


def test_oauth_login_refresh_or_logout_invalidates_old_hint(store):
    store.oauth = True
    assert save(store)
    assert store.load() is not None
    directory = store.home / "mcp-tokens"
    directory.mkdir()
    token_file = directory / "example.json"
    token_file.write_text('{"revision": "login", "tokens": {"access_token": "secret"}}')
    assert store.load() is None
    assert save(store)
    assert "secret" not in path(store).read_text()
    assert store.load() is not None
    token_file.write_text('{"revision": "refresh"}')
    assert store.load() is None
    assert save(store)
    token_file.unlink()
    assert store.load() is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "world-readable", "fifo", "directory"])
def test_unsafe_existing_file_is_not_read_or_overwritten(store, tmp_path, kind):
    save(store)
    target = path(store)
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_text("unrelated")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "world-readable":
        target.write_text("{}")
        target.chmod(0o644)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.mkdir()
    assert store.load() is None
    with pytest.raises(OSError):
        save(store)
    assert outside.read_text() == "unrelated"


def test_symlink_cache_directory_is_not_followed(store, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (cache / "mcp-manifests").symlink_to(outside, target_is_directory=True)
    assert store.load() is None
    with pytest.raises(OSError):
        save(store)
    assert not list(outside.iterdir())


def test_oversize_and_duplicate_tools_are_never_used(store, monkeypatch):
    assert not save(store, [tool(_meta={"large": "x" * manifest.MAX_MANIFEST_BYTES})])
    assert not path(store).exists()
    assert save(store, [tool(), tool()])
    assert store.load() is None
    monkeypatch.setattr(manifest, "MAX_TOOLS", 1)
    assert not save(store, [tool(), tool("other")])


def test_slow_older_publication_cannot_overwrite_newer_catalog(store):
    newer = time.time()
    assert save(store, [tool("new")], observed_at=newer)
    assert not save(store, [tool("old")], observed_at=newer - 1)
    assert store.load().tools[0].name == "new"


def test_atomic_writers_enforce_count_and_byte_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "MAX_CACHE_ENTRIES", 3)
    monkeypatch.setattr(manifest, "MAX_CACHE_BYTES", 1800)
    stores = [ManifestStore(tmp_path, str(i), "id", 60) for i in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(save, stores))
    files = list(path(stores[0]).parent.glob("*.json"))
    assert len(files) <= 3
    assert sum(file.stat().st_size for file in files) <= 1800
    for file in files:
        assert json.loads(file.read_text())["version"] == 1
    assert not list(path(stores[0]).parent.glob(".tmp-*"))


def test_packaged_runtime_without_distribution_metadata_can_still_form_identity(tmp_path, monkeypatch):
    def unavailable(_):
        raise manifest.PackageNotFoundError("mcp")

    monkeypatch.setattr(manifest, "version", unavailable)
    first = configuration_fingerprint("one", {"command": "python"}, tmp_path)
    assert first == configuration_fingerprint("one", {"command": "python"}, tmp_path)
    monkeypatch.setattr(manifest, "_RUNTIME_ID", "different-runtime")
    assert first != configuration_fingerprint("one", {"command": "python"}, tmp_path)


def test_cross_process_newer_catalog_wins_even_when_older_writer_finishes_late(store):
    script = '''
import json, sys, time
from pathlib import Path
from mcp.types import Tool
from flowly.mcp.manifest import ManifestStore
store = ManifestStore(Path(sys.argv[1]), "example", "identity", 60)
observed = time.time()
print("ready", flush=True)
sys.stdin.readline()
print(store.save([Tool(name="old", inputSchema={"type": "object"})], None, observed), flush=True)
'''
    with subprocess.Popen([sys.executable, "-c", script, str(store.home)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process:
        assert process.stdout.readline().strip() == "ready"
        assert save(store, [tool("new")])
        stdout, stderr = process.communicate("continue\n", timeout=5)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "False"
    assert store.load().tools[0].name == "new"


def test_orphan_temporary_files_are_evicted_but_unrelated_files_survive(store):
    save(store)
    directory = path(store).parent
    orphan = directory / (".tmp-" + "a" * 32)
    unrelated = directory / "user-notes.txt"
    orphan.write_text("incomplete")
    unrelated.write_text("preserve")
    assert save(store)
    assert not orphan.exists()
    assert unrelated.read_text() == "preserve"


def test_changed_credentials_during_discovery_are_not_published(store):
    assert not store.save([tool()], None, time.time(), expected_credentials="old")
    assert not path(store).exists()


def test_parent_directory_swap_does_not_redirect_atomic_publication(store, tmp_path, monkeypatch):
    save(store)
    directory = path(store).parent
    captured = directory.with_name("original-cache")
    outside = tmp_path / "outside-cache"
    outside.mkdir(mode=0o700)
    original = os.replace

    def swap(source, target, **kwargs):
        directory.rename(captured)
        directory.symlink_to(outside, target_is_directory=True)
        return original(source, target, **kwargs)

    monkeypatch.setattr(manifest.os, "replace", swap)
    assert save(store, [tool("new")])
    assert not list(outside.iterdir())
    assert json.loads((captured / store.filename).read_text())["tools"][0]["name"] == "new"


def test_byte_quota_evicts_entries_even_below_the_count_limit(store, monkeypatch):
    assert save(store)
    monkeypatch.setattr(manifest, "MAX_CACHE_BYTES", path(store).stat().st_size * 2 - 20)
    for name in ("next", "last"):
        assert save(ManifestStore(store.home, name, "identity", 60))
    files = list(path(store).parent.glob("*.json"))
    assert len(files) == 1
    assert sum(file.stat().st_size for file in files) <= manifest.MAX_CACHE_BYTES


def test_process_death_releases_cache_lock_and_next_writer_reclaims_partial_file(store):
    script = '''
import os, sys
from pathlib import Path
from flowly.mcp.manifest import ManifestStore
store = ManifestStore(Path(sys.argv[1]), "example", "identity", 60)
with store._directory(create=True) as directory, store._locked(directory):
    descriptor = os.open(".tmp-" + "a" * 32, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
    os.write(descriptor, b"incomplete")
    os.close(descriptor)
    print("ready", flush=True)
    sys.stdin.readline()
'''
    with subprocess.Popen([sys.executable, "-c", script, str(store.home)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process:
        try:
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(TimeoutError, match="busy"):
                save(store)
        finally:
            process.terminate()
            process.communicate(timeout=5)
    assert save(store)
    assert store.load() is not None
    assert not list(path(store).parent.glob(".tmp-*"))
