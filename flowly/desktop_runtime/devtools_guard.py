"""Loopback DevTools of a root host's profile browser: only its own agent may connect.

Chromium's remote-debugging port accepts any local connection and has no
authentication, so on a root host another profile's account (its Terminal,
its agent) could drive this profile's browser and read its cookies. Before
Chromium starts, the supervisor picks the port and installs an nftables rule
that resets every loopback connection to it except from root sockets in the
admitted cgroups: the screen's own supervisor scope and the cgroup of this
profile's agent (``agent_scope``). Every agent of a root host is root, so the
cgroup is what keeps another agent, and its shell, out. Hosts without systemd
scopes, or kernels without ``socket cgroupv2`` (before 5.13), get the earlier
rule that admits any root socket. The gateway also checks that the port's
listener is the profile's own account before attaching, so a process that
grabbed the port while the browser was closed cannot pose as the browser.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError

NFT = "/usr/sbin/nft"
_PROC_TCP = Path("/proc/net/tcp")
_LISTEN = "0A"


def table_name(layout_id: str) -> str:
    if not layout_id.isalnum() or len(layout_id) > 32:
        raise ScreenError("RUNTIME_STATE_INVALID")
    return "flowly_screen_" + layout_id[:16]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _nft(script: str) -> bool:
    try:
        return subprocess.run([NFT, "-f", "-"], input=script, text=True, capture_output=True,
                              timeout=10, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def guard(layout_id: str, port: int, admitted: list[str] | None = None) -> None:
    """Replace this layout's rule atomically; the screen must not start without it.

    ``admitted`` lists the cgroup v2 paths (no leading slash) whose root sockets
    may connect; each must exist when the rule loads. None admits any root socket.
    """
    from flowly.desktop_runtime.agent_scope import valid_cgroup

    if type(port) is not int or not 0 < port < 65536:
        raise ScreenError("RUNTIME_STATE_INVALID")
    name = table_name(layout_id)
    if admitted is None:
        rules = f"    oif \"lo\" tcp dport {port} meta skuid != 0 reject with tcp reset\n"
    else:
        if not admitted or not all(valid_cgroup(path) for path in admitted):
            raise ScreenError("RUNTIME_STATE_INVALID")
        rules = "".join(f"    oif \"lo\" tcp dport {port} meta skuid 0 socket cgroupv2 level "
                        f"{len(path.split('/'))} \"{path}\" accept\n" for path in dict.fromkeys(admitted))
        rules += f"    oif \"lo\" tcp dport {port} reject with tcp reset\n"
    script = (f"table inet {name} {{}}\n"
              f"delete table inet {name}\n"
              f"table inet {name} {{\n"
              f"  chain output {{\n"
              f"    type filter hook output priority 0; policy accept;\n"
              f"{rules}"
              f"  }}\n"
              f"}}\n")
    if not _nft(script):
        raise ScreenError("SCREEN_ISOLATION_UNAVAILABLE")


def unguard(layout_id: str) -> None:
    name = table_name(layout_id)
    _nft(f"table inet {name} {{}}\ndelete table inet {name}\n")


def listener_uid(port: int) -> int | None:
    """UID owning the IPv4 loopback listener on ``port``, or None."""
    try:
        lines = _PROC_TCP.read_text().splitlines()[1:]
    except OSError:
        return None
    local = f"0100007F:{port:04X}"
    for line in lines:
        fields = line.split()
        if len(fields) > 7 and fields[1] == local and fields[3] == _LISTEN:
            return int(fields[7])
    return None
