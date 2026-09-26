"""Resource admission against synthetic proc/cgroup trees, without host writes."""

from types import SimpleNamespace

import pytest

from flowly.desktop_runtime import resources

GIB = 1024**3


@pytest.fixture
def host(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text(f"MemTotal:       {8 * GIB // 1024} kB\nMemAvailable:   {4 * GIB // 1024} kB\n")
    cgroup = tmp_path / "control group"
    (cgroup / "user/profile").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/user/profile\n")
    escaped = str(cgroup).replace(" ", r"\040")
    (proc / "self/mountinfo").write_text(f"27 20 0:20 / {escaped} rw - cgroup2 cgroup rw\n")
    for level in (cgroup, cgroup / "user", cgroup / "user/profile"):
        (level / "memory.max").write_text("max\n")
        (level / "memory.current").write_text("0\n")
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=4 * GIB))
    return SimpleNamespace(proc=proc, group=cgroup, directory=tmp_path / "new/profile/desktop",
                           report=lambda: resources.resource_report(tmp_path / "new/profile/desktop", proc=proc, temporary=tmp_path))


def test_unlimited_groups_still_obey_host_available_memory_and_do_not_create_profile(host):
    value = host.report()
    assert value["ready"] and value["reason"] is None
    assert value["memory"] == {"availableBytes": 4 * GIB, "requiredBytes": resources.MIN_MEMORY}
    assert not host.directory.exists()
    assert str(host.proc.parent) not in str(value)


def test_parent_cgroup_headroom_includes_sibling_usage(host):
    (host.group / "user/memory.max").write_text(str(3 * GIB))
    (host.group / "user/memory.current").write_text(str(2 * GIB))
    (host.group / "user/profile/memory.max").write_text(str(2 * GIB))
    assert host.report()["memory"]["availableBytes"] == GIB
    assert host.report()["issues"] == ["LOW_MEMORY"]
    assert host.report()["reason"] == "INSUFFICIENT_RESOURCES"


@pytest.mark.parametrize("used,expected", [(GIB // 2, resources.MIN_MEMORY), (3 * GIB, 0)])
def test_exact_floor_and_over_limit_reading(host, used, expected):
    (host.group / "memory.max").write_text(str(2 * GIB))
    (host.group / "memory.current").write_text(str(used))
    report = host.report()
    assert report["memory"]["availableBytes"] == expected
    assert report["ready"] == (expected >= resources.MIN_MEMORY)


def test_more_inclusive_mount_cannot_be_shadowed_by_subtree_bind(host):
    (host.group / "memory.max").write_text(str(GIB))
    mountinfo = host.proc / "self/mountinfo"
    mountinfo.write_text(f"28 20 0:20 /user {host.group}/bind rw - cgroup2 cgroup rw\n" + mountinfo.read_text())
    # Properly escape the added path too.
    mountinfo.write_text(mountinfo.read_text().replace("control group", r"control\040group"))
    assert host.report()["memory"]["availableBytes"] == GIB


def test_non_root_mount_root_and_namespaced_root(host):
    mountinfo = host.proc / "self/mountinfo"
    mountinfo.write_text(mountinfo.read_text().replace(" / ", " /user "))
    (host.proc / "self/cgroup").write_text("0::/user\n")
    (host.group / "memory.max").write_text(str(GIB))
    assert host.report()["memory"]["availableBytes"] == GIB
    mountinfo.write_text(mountinfo.read_text().replace(" /user ", " / "))
    (host.proc / "self/cgroup").write_text("0::/\n")
    assert host.report()["memory"]["availableBytes"] == GIB


def test_absent_v2_controller_at_leaf_does_not_hide_parent_limit(host):
    (host.group / "user/profile/memory.max").unlink()
    (host.group / "user/profile/memory.current").unlink()
    (host.group / "user/memory.max").write_text(str(GIB))
    assert host.report()["memory"]["availableBytes"] == GIB


def test_no_enabled_memory_controller_uses_host_memory(host):
    for path in host.group.rglob("memory.*"):
        path.unlink()
    assert host.report()["memory"]["availableBytes"] == 4 * GIB
    (host.proc / "self/cgroup").write_text("3:cpu:/user/profile\n")
    assert host.report()["ready"]


def test_v1_and_hybrid_membership_use_memory_controller(host):
    (host.proc / "self/cgroup").write_text("0::/unrelated\n5:memory,devices:/user/profile\n")
    mountinfo = host.proc / "self/mountinfo"
    mountinfo.write_text(mountinfo.read_text().replace("cgroup2 cgroup rw", "cgroup cgroup rw,memory,devices"))
    for level in (host.group, host.group / "user", host.group / "user/profile"):
        (level / "memory.limit_in_bytes").write_text(str(2**63 - 4096))
        (level / "memory.usage_in_bytes").write_text("0")
    assert host.report()["ready"]
    (host.group / "user/memory.limit_in_bytes").write_text(str(2 * GIB))
    (host.group / "user/memory.usage_in_bytes").write_text(str(GIB))
    assert host.report()["memory"]["availableBytes"] == GIB


@pytest.mark.parametrize("file,text", [
    ("meminfo", "MemTotal: 999999999 kB"),
    ("meminfo", "MemAvailable: 1000 kB\nMemAvailable: 2000 kB\n"),
    ("self/cgroup", ""), ("self/cgroup", "invalid"),
    ("self/cgroup", "0::/../../outside"),
    ("self/cgroup", "0::/missing (deleted)"),
    ("self/cgroup", "0::/\n0::/other"),
    ("self/mountinfo", ""), ("self/mountinfo", "invalid"),
    ("self/mountinfo", "x" * (1024 * 1024 + 1)),
])
def test_unknown_kernel_records_fail_closed(host, file, text):
    (host.proc / file).write_text(text)
    report = host.report()
    assert not report["ready"]
    assert report["reason"] == "RESOURCE_CHECK_UNAVAILABLE"
    assert report["memory"]["availableBytes"] is None


@pytest.mark.parametrize("text", ["-1", "NaN", "", "max " * 100, "1.5", "1\n2"])
def test_invalid_counter_cannot_be_treated_as_unlimited(host, text):
    (host.group / "user/memory.current").write_text(text)
    assert host.report()["issues"] == ["MEMORY_UNAVAILABLE"]


@pytest.mark.parametrize("missing", ["memory.max", "memory.current"])
def test_half_present_counter_pair_is_unknown(host, missing):
    (host.group / "user" / missing).unlink()
    assert host.report()["issues"] == ["MEMORY_UNAVAILABLE"]


def test_permission_failure_is_not_a_missing_controller(host, monkeypatch):
    original = resources._read
    def read(path, limit=65536):
        if path.name == "memory.max":
            raise PermissionError("private path must not escape")
        return original(path, limit)
    monkeypatch.setattr(resources, "_read", read)
    assert host.report()["issues"] == ["MEMORY_UNAVAILABLE"]
    assert "private" not in str(host.report())


def test_low_and_unreadable_disks_remain_distinct(host, monkeypatch):
    def disk(path):
        if path == host.proc.parent:
            raise PermissionError("private filesystem")
        return SimpleNamespace(free=resources.MIN_DISK - 1)
    monkeypatch.setattr(resources.shutil, "disk_usage", disk)
    value = host.report()
    assert value["issues"] == ["LOW_PROFILE_DISK", "TEMPORARY_DISK_UNAVAILABLE"]
    assert value["reason"] == "RESOURCE_CHECK_UNAVAILABLE"


def test_disk_uses_existing_ancestor_without_mkdir(tmp_path):
    path = tmp_path / "not-created/profile/desktop"
    assert resources._disk(path) > 0
    assert not path.exists()
