"""Read-only startup budget checks, including visible Linux cgroup ancestors.

These are admission floors, not reservations or a guarantee against later OOM.
Cgroup namespaces can hide ancestors; only visible constraints can be measured.
Never use a later low-memory reading to stop an already running browser.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

MIN_MEMORY = 1536 * 1024**2
MIN_DISK = 1024**3
_MAX_PUBLIC = 2**53 - 1


def _read(path: Path, limit: int = 65536) -> str:
    with path.open() as source:
        text = source.read(limit + 1)
    if len(text) > limit:
        raise ValueError("oversized kernel record")
    return text


def _number(text: str) -> int:
    value = text.strip()
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("invalid kernel counter")
    return int(value)


def _absolute(text: str) -> PurePosixPath:
    if not text.startswith("/") or ".." in text.split("/") or "\x00" in text:
        raise ValueError("invalid kernel path")
    return PurePosixPath(text)


def _unescape(text: str) -> str:
    # mountinfo escapes whitespace and backslashes as octal bytes.
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), text)


def _cgroup_headroom(proc: Path) -> int | None:
    groups = []
    for line in _read(proc / "self/cgroup").splitlines():
        hierarchy, controllers, member = line.split(":", 2)
        _number(hierarchy)
        groups.append((controllers.split(","), _absolute(member), hierarchy))
    if not groups:
        raise ValueError("missing cgroup membership")
    # In hybrid setups the memory controller belongs to exactly one hierarchy.
    candidates = [(1, member) for controllers, member, _ in groups if "memory" in controllers]
    if not candidates:
        candidates = [(2, member) for controllers, member, hierarchy in groups
                      if controllers == [""] and hierarchy == "0"]
    if not candidates:
        return None  # The memory controller is not attached to this process.
    if len(candidates) != 1:
        raise ValueError("ambiguous cgroup membership")
    version, member = candidates[0]
    mounts = []
    for line in _read(proc / "self/mountinfo", 1024 * 1024).splitlines():
        left, right = line.split(" - ", 1)
        fields, filesystem = left.split(), right.split()
        if len(fields) < 6 or len(filesystem) < 3:
            raise ValueError("invalid mount record")
        matches = (version == 2 and filesystem[0] == "cgroup2") or (
            version == 1 and filesystem[0] == "cgroup" and "memory" in filesystem[2].split(","))
        if not matches:
            continue
        root, mount = _absolute(_unescape(fields[3])), _absolute(_unescape(fields[4]))
        if member.is_relative_to(root):
            mounts.append((root, mount))
    if not mounts:
        raise ValueError("memory hierarchy is not visible")
    # Prefer the most inclusive mount so an extra subtree bind mount cannot hide
    # an ancestor budget. Duplicate complete mounts do not change the minimum.
    root, mount = min(mounts, key=lambda item: len(item[0].parts))
    relative = member.relative_to(root)
    if len(relative.parts) > 64:
        raise ValueError("cgroup hierarchy too deep")
    current = Path(mount / relative)
    floor = Path(mount)
    budgets = []
    while True:
        limit_name, usage_name = (("memory.max", "memory.current") if version == 2
                                  else ("memory.limit_in_bytes", "memory.usage_in_bytes"))
        try:
            limit = _read(current / limit_name, 128).strip()
        except FileNotFoundError:
            # v2 may have no memory controller enabled at this level, including
            # the real root. A half-present pair or unreadable level is unknown.
            if version != 2:
                raise
            try:
                _read(current / usage_name, 128)
            except FileNotFoundError:
                if not current.is_dir():
                    raise ValueError("cgroup disappeared") from None
            else:
                raise ValueError("incomplete memory counters") from None
        else:
            used = _number(_read(current / usage_name, 128))
            if not (version == 2 and limit == "max"):
                maximum = _number(limit)
                if not (version == 1 and maximum >= 2**60):
                    budgets.append(max(0, maximum - used))
        if current == floor:
            break
        current = current.parent
    return min(budgets) if budgets else None


def _memory(proc: Path) -> int:
    values = re.findall(r"^MemAvailable:\s+([0-9]+) kB$", _read(proc / "meminfo"), re.MULTILINE)
    if len(values) != 1:
        raise ValueError("available memory missing")
    available = _number(values[0]) * 1024
    headroom = _cgroup_headroom(proc)
    return available if headroom is None else min(available, headroom)


def _disk(path: Path) -> int:
    # Inspect the filesystem that would hold a new profile without creating it.
    path = path.absolute()
    while True:
        try:
            return shutil.disk_usage(path).free
        except FileNotFoundError:
            if path.parent == path:
                raise
            path = path.parent


def resource_report(directory: Path, *, proc: Path = Path("/proc"), temporary: Path = Path("/tmp")) -> dict:
    """Closed, path-free snapshot. An unavailable reading never means unlimited."""
    issues = []
    metrics = {}
    for name, threshold, low, unknown, read in (
        ("memory", MIN_MEMORY, "LOW_MEMORY", "MEMORY_UNAVAILABLE", lambda: _memory(proc)),
        ("profileDisk", MIN_DISK, "LOW_PROFILE_DISK", "PROFILE_DISK_UNAVAILABLE", lambda: _disk(directory)),
        ("temporaryDisk", MIN_DISK, "LOW_TEMPORARY_DISK", "TEMPORARY_DISK_UNAVAILABLE", lambda: _disk(temporary)),
    ):
        try:
            available = read()
            if type(available) is not int or not 0 <= available <= _MAX_PUBLIC:
                raise ValueError("counter outside public range")
        except (OSError, ValueError, UnicodeError):
            available = None
            issues.append(unknown)
        else:
            if available < threshold:
                issues.append(low)
        metrics[name] = {"availableBytes": available, "requiredBytes": threshold}
    reason = ("RESOURCE_CHECK_UNAVAILABLE" if any(issue.endswith("_UNAVAILABLE") for issue in issues)
              else "INSUFFICIENT_RESOURCES" if issues else None)
    return {"ready": not issues, "reason": reason, "issues": issues, **metrics}
