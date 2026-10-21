"""Dedicated runtime account per profile incarnation on root-owned hosts.

The shared installer account (``flowly-screen``) remains the provenance gate.
Each layout (one per profile incarnation) runs its applications under its own
system account, so one profile's Terminal or browser can never read another
profile's browser storage, and every process of a profile's screen is
identifiable by UID for cleanup. Names derive only from the random layout id.
Account changes run the fixed payload in an isolated interpreter.
"""

from __future__ import annotations

import json
import re
import subprocess

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.runtime_account import (
    ROOT,
    RuntimeAccount,
    _receipted_account,
    runtime_account,
)

PROFILE_ACCOUNTS = ROOT / "accounts"
_NAME = re.compile(r"flowly-s-[a-f0-9]{12}")
_ERRORS = {"INSTALL_BUSY": "SCREEN_BUSY", "INSTALL_RECOVERY_REQUIRED": "RUNTIME_RECOVERY_REQUIRED",
           "INSTALL_STATE_INVALID": "RUNTIME_STATE_INVALID", "INVALID_PARAMS": "RUNTIME_STATE_INVALID",
           "INSTALL_PRIVILEGE_REQUIRED": "UNPRIVILEGED_RUNTIME_REQUIRED",
           "INSTALL_ACCOUNT_CONFLICT": "RUNTIME_ACCOUNT_CONFLICT"}


def account_name(layout_id: str) -> str:
    if not isinstance(layout_id, str) or re.fullmatch(r"[a-f0-9]{32}", layout_id) is None:
        raise ScreenError("RUNTIME_STATE_INVALID")
    return "flowly-s-" + layout_id[:12]


def profile_account(name: str) -> RuntimeAccount:
    """Receipted, unchanged identity or fail closed (never adopt by name)."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ScreenError("RUNTIME_STATE_INVALID")
    runtime_account()
    return _receipted_account(PROFILE_ACCOUNTS, PROFILE_ACCOUNTS / (name + ".json"), name,
                              missing="RUNTIME_RECOVERY_REQUIRED")


def _run_payload(function: str, argument: str, timeout: int) -> object:
    from flowly.desktop_runtime.runtime_account_payload import PROFILE_ACCOUNT_SOURCE

    code = PROFILE_ACCOUNT_SOURCE + f"""
import sys
try:
    value = {function}(sys.argv[1])
    print(json.dumps({{"ok": True, "value": value}}))
except ValueError as exc:
    print(json.dumps({{"error": str(exc)}}))
"""
    try:
        completed = subprocess.run(["/usr/bin/python3", "-I", "-c", code, argument], stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=timeout, cwd="/", close_fds=True,
                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise ScreenError("RUNTIME_RECOVERY_REQUIRED") from None
    except OSError:
        raise ScreenError("RUNTIME_STATE_INVALID") from None
    try:
        result = json.loads(completed.stdout[-65536:])
    except (ValueError, UnicodeError):
        raise ScreenError("RUNTIME_STATE_INVALID") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        code_name = result.get("error") if isinstance(result, dict) else None
        raise ScreenError(_ERRORS.get(code_name, "RUNTIME_STATE_INVALID"))
    return result.get("value")


def _payload(function: str, name: str) -> None:
    if _NAME.fullmatch(name) is None:
        raise ScreenError("RUNTIME_STATE_INVALID")
    _run_payload(function, name, 60)


def provision_profile_account(name: str) -> RuntimeAccount:
    _payload("provision_profile_account", name)
    return profile_account(name)


def retire_profile_account(name: str) -> None:
    """Kill every process of the account, clear its temporary files, delete it."""
    _payload("retire_profile_account", name)


def terminate_profile_account_processes(name: str) -> None:
    _payload("terminate_profile_account_processes", name)


_RESTORED = {"present", "restored"}


def restore_runtime_accounts() -> dict:
    """Recreate every receipted screen account a root filesystem reset removed.

    For hosts whose /etc/passwd does not persist but whose screen runtime state
    does (a cloud machine's volume). Each account gets back exactly the UID and
    GID in its receipt; an account or ID held by anything else is reported per
    profile and left alone, and that profile's screen keeps failing closed.
    Returns {"shared": "absent"|"present"|"restored",
             "profiles": {name: "present"|"restored"|<screen error code>}}.
    """
    value = _run_payload("restore_runtime_accounts", "-", 600)
    if (not isinstance(value, dict) or set(value) != {"shared", "profiles"}
            or value["shared"] not in _RESTORED | {"absent"} or not isinstance(value["profiles"], dict)):
        raise ScreenError("RUNTIME_STATE_INVALID")
    profiles = {}
    for name, outcome in value["profiles"].items():
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or not isinstance(outcome, str):
            raise ScreenError("RUNTIME_STATE_INVALID")
        profiles[name] = outcome if outcome in _RESTORED else _ERRORS.get(outcome, "RUNTIME_STATE_INVALID")
    return {"shared": value["shared"], "profiles": profiles}
