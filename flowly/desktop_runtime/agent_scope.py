"""One cgroup per agent on a root host, so a profile's browser admits only its agent.

Every agent of a root host runs as root, so the DevTools rule
(``devtools_guard``) tells them apart by the cgroup of the connecting socket.
The default agent keeps its gateway's cgroup (normally its systemd service).
Each named agent's gateway starts in a transient systemd scope of its own,
``PartOf`` that service, so stopping or restarting the service still stops it,
as it did while it shared the service's cgroup. Every gateway records its
cgroup in its home; the screen's supervisor admits exactly that cgroup.

This keeps one agent out of another agent's browser, including an agent that
improvises with a shell. It is not a boundary against a root process that sets
out to remove it: root can rewrite nftables.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

AGENT_CGROUP_FILE = "agent-cgroup"
SCOPE_PREFIX = "flowly-agent-"
CGROUP_ROOT = Path("/sys/fs/cgroup")
_COMPONENT = re.compile(r"[A-Za-z0-9@._:-]{1,255}")


def valid_cgroup(path: str) -> bool:
    """A cgroup v2 path safe to place in an nftables rule (no quoting, no escapes)."""
    if not isinstance(path, str) or not path or len(path) > 1024:
        return False
    parts = path.split("/")
    return len(parts) <= 32 and all(_COMPONENT.fullmatch(part) and part not in {".", ".."} for part in parts)


def own_cgroup(proc: Path = Path("/proc/self/cgroup")) -> str | None:
    """This process's cgroup v2 path, without the leading slash."""
    try:
        records = [line[3:] for line in proc.read_text().splitlines() if line.startswith("0::")]
    except OSError:
        return None
    if len(records) != 1 or not records[0].startswith("/"):
        return None
    path = records[0].lstrip("/")
    return path if valid_cgroup(path) else None


def _root_systemd() -> bool:
    from flowly.desktop_runtime import resource_scope

    return sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0 and resource_scope.available()


@functools.lru_cache(maxsize=2)
def _scopes_work(executable: str, user: bool) -> bool:
    """Whether this manager accepts a transient scope; probed once per process."""
    try:
        result = subprocess.run([executable, *(["--user"] if user else []), "--scope", "--quiet", "--collect",
                                 "--", "/bin/true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def agent_scope_command(command: list[str], profile: str) -> list[str]:
    """``command`` wrapped to start in a new scope of its own on a root systemd host.

    The scope lives in the manager of the calling gateway (root's user manager
    for the usual user service), ``PartOf`` the gateway's service. Elsewhere the
    command is returned unchanged; its agent then has no separate cgroup and its
    screen admits none (see ``admitted_agent_cgroup``).
    """
    if not _root_systemd():
        return command
    own = own_cgroup()
    executable = shutil.which("systemd-run", path="/usr/bin:/bin")
    if own is None or executable is None:
        return command
    user = any(part.startswith("user@") and part.endswith(".service") for part in own.split("/"))
    if not _scopes_work(executable, user):
        logger.warning("[Screen] agent scopes are unavailable; a named agent cannot use its screen browser")
        return command
    tag = hashlib.sha256(profile.encode()).hexdigest()[:12]
    unit = f"{SCOPE_PREFIX}{tag}-{secrets.token_hex(4)}.scope"
    service = own.rsplit("/", 1)[-1]
    # In scope mode systemd-run moves itself into the scope and executes the
    # command in place: the PID, environment and pipes stay the caller's.
    return [executable, *(["--user"] if user else []), "--scope", "--quiet", "--collect", "--unit=" + unit,
            *(["--property=PartOf=" + service] if service.endswith(".service") else []), "--", *command]


def record_agent_cgroup(home: Path) -> None:
    """Publish this gateway's cgroup for its profile's screen (root hosts only)."""
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    path = home / AGENT_CGROUP_FILE
    own = own_cgroup()
    try:
        if own is None:
            path.unlink(missing_ok=True)
            return
        temporary = home / (AGENT_CGROUP_FILE + ".%s.tmp" % secrets.token_hex(6))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as target:
                target.write(own + "\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        logger.warning("[Screen] could not record this agent's cgroup for its screen")


def admitted_agent_cgroup(home: Path) -> str | None:
    """The cgroup the screen of ``home`` admits as its agent, or None.

    A named profile's agent must be in its own agent scope; the default agent in
    anything but one. A record that breaks this (a gateway started before agent
    scopes existed, or where they are unavailable) admits nobody: no agent then
    shares a browser it does not own.
    """
    from flowly.profile import _PROFILE_NAME_RE, _PROFILES_ROOT

    try:
        fd = os.open(home / AGENT_CGROUP_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            path = source.read(2048).strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not valid_cgroup(path):
        return None
    named = home.parent == _PROFILES_ROOT and bool(_PROFILE_NAME_RE.fullmatch(home.name))
    if named != path.rsplit("/", 1)[-1].startswith(SCOPE_PREFIX):
        return None
    return path
