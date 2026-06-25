"""CLI-side sandbox wrapping — re-exec under sandbox-exec on macOS.

Called at the very top of :func:`flowly.cli.entry.main`. Inspects
``FLOWLY_SANDBOX``, the config file at ``~/.flowly/config.json``, and
the host platform; when it decides the agent should run sandboxed,
writes an SBPL profile to ``/tmp`` and re-execs the current command
under ``sandbox-exec(1)``.

The recursion guard is the ``FLOWLY_SANDBOX_WRAPPED`` env var. The
outer process sets it before ``os.execve``; the inner process sees
it set and returns from :func:`maybe_reexec_sandboxed` immediately,
so the rest of ``main()`` runs once — inside the sandbox.

The deny/allow lists mirror the TypeScript desktop policy in
``flowly-desktop/src/main/local/sandbox/policy.ts``. The two lists
are small enough to keep in sync by hand; a shared JSON manifest
would cost more in coordination than it would save in lines.

Linux support (bwrap) is intentionally absent in v1. CLI runs on
Linux without sandboxing, matching the desktop's posture. The
cross-platform plan lives in ``SECURITY.md`` §2.2.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from pathlib import Path

# Set on the env passed to the re-execed child so the recursive call
# to maybe_reexec_sandboxed() returns immediately. The child wears
# the sandbox already; running it again inside itself would either
# error (sandbox-exec inside sandbox-exec) or, worse, succeed quietly
# and double the per-spawn cost.
_RECURSION_MARKER = "FLOWLY_SANDBOX_WRAPPED"

# Hard-coded path. sandbox-exec ships with every macOS version since
# 10.5; if it's missing the host is so non-standard that fail-open is
# the right call.
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Operator-facing env var. Same semantics as the TypeScript desktop's
# sandboxEnabledFromEnv(): "0"/"false"/"off"/"no" disables; anything
# else (including unset) leaves the config-driven default in charge.
_ENV_TOGGLE = "FLOWLY_SANDBOX"
_FALSE_VALUES = {"0", "false", "off", "no"}


def maybe_reexec_sandboxed() -> None:
    """Re-exec self under sandbox-exec if we should be sandboxed and
    aren't already.

    Returns normally in any of these cases:

      • ``FLOWLY_SANDBOX_WRAPPED=1`` (recursion guard, we're already
        inside the wrap).
      • ``FLOWLY_SANDBOX`` set to a falsy value (operator override).
      • Env var unset and ``config.security.sandbox`` is ``false``.
      • Platform is Windows (native sandbox roadmap, not shipped).
      • The required primitive is missing on the host
        (``/usr/bin/sandbox-exec`` on macOS, ``bwrap`` on Linux).
      • Any I/O error writing the profile file or building the
        wrapper (fail open — running unsandboxed is no worse than
        the pre-sandbox baseline, refusing to start would be a
        regression).

    Otherwise this function does not return — :func:`os.execve`
    replaces the current process with the platform sandbox wrapper
    executing the same Python command line.
    """
    env = os.environ

    # 0. Nuitka-compiled standalone binary: skip self-wrap entirely.
    #
    #    Two reasons:
    #
    #    (a) In a Nuitka --standalone bundle `sys.executable` points at
    #        ``<dist_dir>/python3`` (Nuitka's compat placeholder for an
    #        interpreter that is actually embedded in the .bin itself —
    #        no real file lives there). The os.execve below would then
    #        ENOENT and exit the process with code 71 before any flowly
    #        code gets to run. This is exactly the 2026-05-20 CI failure
    #        on macos-15 / macos-15-intel runners: smoke test
    #        ``entry.bin --version`` aborted with
    #        ``sandbox-exec: execvp() of '.../entry.dist/python3' failed``.
    #
    #    (b) The Nuitka binary only ships inside the Electron desktop
    #        bundle, and that bundle already wraps the helper in an
    #        outer sandbox (flowly-desktop/src/main/local/sandbox/).
    #        Re-wrapping here would either be a no-op (sandbox-exec
    #        inside sandbox-exec) or double the per-spawn cost.
    #
    #    Detection: Nuitka injects a module-level ``__compiled__`` global
    #    into every compiled module. Plain CPython has no such name, so
    #    the ``globals()`` lookup is the cheapest and most reliable
    #    discriminator. ``sys.frozen`` is checked as a belt-and-braces
    #    fallback in case a future Nuitka version drops ``__compiled__``.
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return

    # 1. Recursion guard. Must be first — saves work in the inner run.
    if env.get(_RECURSION_MARKER) == "1":
        return

    # 2. Explicit env opt-out wins over config.
    explicit = env.get(_ENV_TOGGLE, "").strip().lower()
    if explicit in _FALSE_VALUES:
        return

    # 3. Env unset → consult config. Default-on if config can't be
    #    read or doesn't say anything.
    if explicit == "" and not _config_sandbox_enabled():
        return

    # 4. Platform dispatch.
    system = platform.system()
    if system == "Darwin":
        _reexec_macos(env)
    elif system == "Linux":
        _reexec_linux(env)
    # Windows / other platforms fall through to no-op (SECURITY.md
    # §2.2 documents Windows native sandboxing as roadmap).


def _reexec_macos(env: "os._Environ[str]") -> None:
    """macOS re-exec under sandbox-exec(1) with a generated SBPL profile."""
    if not Path(_SANDBOX_EXEC).exists():
        return

    home = str(Path.home())
    try:
        profile_path = _write_profile(_build_sbpl_profile(home))
    except Exception:
        return

    new_env = dict(env)
    new_env[_RECURSION_MARKER] = "1"
    argv = [_SANDBOX_EXEC, "-f", profile_path, sys.executable, *sys.argv]

    try:
        os.execve(_SANDBOX_EXEC, argv, new_env)
    except OSError:
        # execve can fail (e.g. SIP weirdness, broken sandbox-exec).
        # Fall through to unsandboxed rather than crashing the CLI.
        return


def _reexec_linux(env: "os._Environ[str]") -> None:
    """Linux re-exec under bubblewrap (bwrap) with a namespace-based sandbox.

    The argv shape mirrors the TypeScript desktop launcher in
    ``flowly-desktop/src/main/local/sandbox/linux.ts`` so a single
    SandboxPolicy intent — deny these paths, allow writes to these,
    inherit network — produces the same isolation on both spawn
    paths (Electron-driven and CLI-direct).

    Failure modes (return without re-exec):
      • bwrap not installed (Alpine, minimal server images).
      • bwrap exec fails (kernel without user-namespace support).
    """
    bwrap = _find_bwrap()
    if not bwrap:
        return

    home = str(Path.home())
    try:
        bwrap_args = _build_bwrap_args(home)
    except Exception:
        return

    new_env = dict(env)
    new_env[_RECURSION_MARKER] = "1"
    argv = [bwrap, *bwrap_args, "--", sys.executable, *sys.argv]

    try:
        os.execve(bwrap, argv, new_env)
    except OSError:
        return


# Candidate bwrap install paths, probed in order. Distro packages
# land in /usr/bin; from-source / dev installs sometimes use
# /usr/local/bin; the Linuxbrew prefix /home/linuxbrew/.linuxbrew/bin
# matters for power users on minimal distros.
_BWRAP_CANDIDATES = (
    "/usr/bin/bwrap",
    "/usr/local/bin/bwrap",
    "/home/linuxbrew/.linuxbrew/bin/bwrap",
)


def _find_bwrap() -> str | None:
    """Locate a usable bwrap binary on the host, or ``None``."""
    for candidate in _BWRAP_CANDIDATES:
        try:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _build_bwrap_args(home: str) -> list[str]:
    """Generate the bwrap argv list for the Linux sandbox.

    Exposed at module level so tests can pin the output without
    actually re-execing.
    """
    args: list[str] = []

    # Filesystem layout: read-only root, fresh /proc + /dev + /tmp,
    # then read-only home (with read-write punches below).
    args += ["--ro-bind", "/", "/"]
    args += ["--proc", "/proc"]
    args += ["--dev", "/dev"]
    args += ["--tmpfs", "/tmp"]
    args += ["--ro-bind", home, home]

    # Read-write holes. /private/* paths from the policy are macOS-
    # specific; on Linux only /tmp is meaningful (already tmpfs'd
    # above, but bind-try is idempotent for an already-mounted path).
    allow_writes = [f"{home}/.flowly", home, "/tmp"]
    for p in allow_writes:
        args += ["--bind-try", p, p]

    # Mask sensitive paths with empty tmpfs mounts. Filter to entries
    # that actually exist — bwrap's --tmpfs errors on a missing
    # mount target, and the default deny list carries macOS-flavoured
    # paths (~/Library/Keychains, ~/Library/Cookies, …) that don't
    # exist on Linux.
    for rel in _DENY_READ_PATHS_REL:
        absolute = f"{home}/{rel}"
        try:
            if Path(absolute).exists():
                args += ["--tmpfs", absolute]
        except OSError:
            continue

    # Phase A/B: share host network. Egress filtering is operator's
    # firewall responsibility (SECURITY.md §4.1).
    args += ["--share-net"]

    # Other namespaces: own PID / UTS / IPC / cgroup view; die with
    # parent so a crashed CLI doesn't orphan a process; new session
    # for clean signal propagation.
    args += ["--unshare-pid", "--unshare-uts", "--unshare-ipc"]
    args += ["--unshare-cgroup-try"]
    args += ["--die-with-parent"]
    args += ["--new-session"]

    return args


# ── Config gate ─────────────────────────────────────────────────────


def _config_sandbox_enabled() -> bool:
    """Read ``security.sandbox`` from ``~/.flowly/config.json``.

    Returns ``True`` unless the field is explicitly ``false``. A
    missing field, missing config, or read error all resolve to
    ``True`` — the default-on stance documented in SECURITY.md §2.2.
    """
    try:
        cfg_path = Path.home() / ".flowly" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    sec = cfg.get("security") if isinstance(cfg, dict) else None
    if not isinstance(sec, dict):
        return True
    return sec.get("sandbox") is not False


# ── Profile generation ─────────────────────────────────────────────


# Relative to $HOME. Kept in sync by hand with the TypeScript desktop
# policy. The TS list is the source of truth for the description; we
# duplicate the values, not the comments.
_DENY_READ_PATHS_REL = (
    ".ssh",
    ".aws",
    ".config/gcloud",
    ".gcp",
    "Library/Keychains",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft/Edge",
    "Library/Cookies",
    ".mozilla",
    ".config/google-chrome",
    ".config/BraveSoftware",
)

_POSIX_TMP = ("/tmp", "/private/tmp", "/private/var/folders")


def _build_sbpl_profile(home: str) -> str:
    """Generate the SBPL profile string.

    Exposed at module level so tests can pin the output without
    going through ``os.execve``. Production callers should never
    need to invoke this directly.
    """
    deny_reads = [f"{home}/{rel}" for rel in _DENY_READ_PATHS_REL]
    allow_writes = [
        f"{home}/.flowly",
        # Workspace = $HOME until a Settings-level workspace picker
        # lands. Matches the unsandboxed baseline.
        home,
        *_POSIX_TMP,
    ]

    lines: list[str] = ["(version 1)", "(allow default)", ""]

    if deny_reads:
        lines.append(";; Sensitive paths the agent must not read.")
        lines.append("(deny file-read*")
        for p in deny_reads:
            lines.append(f"  (subpath {_sbpl_string(p)})")
        lines.append(")")
        lines.append("")

    lines.append(";; Writes default-deny outside the explicit allow list.")
    lines.append("(deny file-write*)")
    lines.append("(allow file-write*")
    for p in allow_writes:
        lines.append(f"  (subpath {_sbpl_string(p)})")
    lines.append('  (literal "/dev/null")')
    lines.append('  (literal "/dev/dtracehelper")')
    lines.append(")")
    lines.append("")

    # Phase A/B: outbound network unrestricted. Phase C will swap
    # this for a (remote-host ...) allowlist sourced from the
    # operator's configured providers.
    lines.append(";; Phase A/B: outbound network unrestricted.")
    lines.append("(allow network*)")
    lines.append("")

    # Subprocesses (git, npm, MCP servers, etc.) inherit this profile
    # because they're launched under sandbox-exec.
    lines.append(";; Subprocesses inherit this profile.")
    lines.append("(allow process-exec*)")
    lines.append("(allow process-fork)")

    return "\n".join(lines) + "\n"


def _sbpl_string(s: str) -> str:
    """Quote a path for SBPL.

    SBPL strings are Lisp-style double-quoted with backslash escapes
    for ``"`` and ``\\``. Newlines confuse the tokeniser and are
    illegal in macOS paths anyway, so we refuse them outright.
    """
    if "\n" in s or "\r" in s:
        raise ValueError(f"SBPL path contains a newline: {s!r}")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_profile(profile: str) -> str:
    """Drop the profile to ``/tmp`` and return the path.

    macOS prunes ``/tmp`` on reboot, so we don't explicitly clean up.
    Filename includes the PID so concurrent agent starts don't
    clobber each other.
    """
    fd, path = tempfile.mkstemp(
        prefix=f"flowly-agent-{os.getpid()}-",
        suffix=".sb",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(profile)
    return path
