"""Materialize browser-friendly artifacts and open them on the TUI host."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowly.config.loader import get_data_dir
from flowly.utils.subprocess_compat import detach_kwargs

_EXTENSIONS = {"html": ".html", "svg": ".svg"}


@dataclass(frozen=True)
class ArtifactOpenResult:
    status: str
    path: Path | None = None
    detail: str = ""


def is_external_artifact_type(artifact_type: str) -> bool:
    return artifact_type.lower() in _EXTENSIONS


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def _opener_command() -> tuple[str, list[str] | None]:
    """Return capability status and an opener command without its target."""
    allow_remote = os.environ.get("FLOWLY_TUI_ALLOW_REMOTE_OPEN") == "1"
    if sys.platform == "darwin":
        if not allow_remote and (os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION")):
            return "headless", None
        opener = shutil.which("open")
        return ("ready", [opener]) if opener else ("unsupported", None)

    if sys.platform == "win32":
        cmd = shutil.which("cmd") or shutil.which("cmd.exe")
        return ("ready", [cmd, "/c", "start", ""]) if cmd else ("unsupported", None)

    if _is_wsl():
        wslview = shutil.which("wslview")
        if wslview:
            return "ready", [wslview]

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "headless", None
    opener = shutil.which("xdg-open")
    return ("ready", [opener]) if opener else ("unsupported", None)


def _materialize(artifact: dict[str, Any]) -> Path:
    artifact_type = str(artifact.get("type") or "").lower()
    extension = _EXTENSIONS[artifact_type]
    artifact_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(artifact.get("id") or "artifact"))
    version = int(artifact.get("version") or 1)
    target_dir = get_data_dir() / "tmp" / "artifacts"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"{artifact_id}-v{version}{extension}"
    for stale in target_dir.glob(f"{artifact_id}-v*{extension}"):
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                pass
    target.write_text(str(artifact.get("content") or ""), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def open_artifact_external(artifact: dict[str, Any]) -> ArtifactOpenResult:
    artifact_type = str(artifact.get("type") or "").lower()
    if artifact_type not in _EXTENSIONS:
        return ArtifactOpenResult("unsupported", detail=f"unsupported type: {artifact_type}")

    capability, command = _opener_command()
    if capability != "ready" or command is None:
        return ArtifactOpenResult(capability)

    try:
        target = _materialize(artifact)
        subprocess.Popen(
            [*command, str(target)],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach_kwargs(),
        )
        return ArtifactOpenResult("opened", path=target)
    except (OSError, ValueError, TypeError) as exc:
        return ArtifactOpenResult(
            "failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
