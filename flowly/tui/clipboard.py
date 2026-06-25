"""System clipboard image extraction for the TUI."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import uuid
from pathlib import Path

from flowly.profile import get_flowly_home

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def save_clipboard_image() -> Path | None:
    """Save the current clipboard image as a local file, if one exists."""
    dest = _clipboard_image_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok = False
    if sys.platform == "darwin":
        ok = _macos_save(dest)
    elif sys.platform == "win32":
        ok = _windows_save(dest)
    else:
        ok = _linux_save(dest)
    if ok and dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _clipboard_image_path() -> Path:
    return get_flowly_home() / "clipboard" / f"clip_{uuid.uuid4().hex}.png"


def _macos_save(dest: Path) -> bool:
    return _macos_pngpaste(dest) or _macos_osascript(dest)


def _macos_pngpaste(dest: Path) -> bool:
    try:
        result = subprocess.run(
            ["pngpaste", str(dest)],
            capture_output=True,
            timeout=3,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _macos_has_image() -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    return "class PNGf" in result.stdout or "class TIFF" in result.stdout


def _macos_osascript(dest: Path) -> bool:
    if not _macos_has_image():
        return False
    script = (
        "try\n"
        "  set imgData to the clipboard as <<class PNGf>>\n"
        f'  set f to open for access POSIX file "{dest}" with write permission\n'
        "  write imgData to f\n"
        "  close access f\n"
        '  return "ok"\n'
        "on error\n"
        '  return "fail"\n'
        "end try\n"
    ).replace("<<class PNGf>>", "\u00abclass PNGf\u00bb")
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return (
        result.returncode == 0
        and "fail" not in result.stdout
        and dest.exists()
        and dest.stat().st_size > 0
    )


_PS_CHECK_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[System.Windows.Forms.Clipboard]::ContainsImage()"
)

_PS_EXTRACT_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$img = [System.Windows.Forms.Clipboard]::GetImage();"
    "if ($null -eq $img) { exit 1 }"
    "$ms = New-Object System.IO.MemoryStream;"
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
    "[System.Convert]::ToBase64String($ms.ToArray())"
)


def _windows_save(dest: Path) -> bool:
    for exe in ("powershell", "pwsh"):
        if _powershell_save(exe, dest, timeout=12):
            return True
    return False


def _wsl_save(dest: Path) -> bool:
    return _powershell_save("powershell.exe", dest, timeout=15)


def _powershell_save(exe: str, dest: Path, *, timeout: int) -> bool:
    try:
        check = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_CHECK_IMAGE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    if check.returncode != 0 or "True" not in check.stdout:
        return False
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_EXTRACT_IMAGE],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return _write_base64_image(dest, result.stdout.strip())


def _write_base64_image(dest: Path, b64_data: str) -> bool:
    try:
        data = base64.b64decode(b64_data, validate=True)
    except Exception:
        return False
    if not data:
        return False
    dest.write_bytes(data)
    return dest.exists() and dest.stat().st_size > 0


def _linux_save(dest: Path) -> bool:
    if _is_wsl() and _wsl_save(dest):
        return True
    if os.environ.get("WAYLAND_DISPLAY") and _wayland_save(dest):
        return True
    return _xclip_save(dest)


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def _wayland_save(dest: Path) -> bool:
    try:
        types = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    if types.returncode != 0:
        return False
    mime = next(
        (
            item
            for item in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp")
            if item in types.stdout.splitlines()
        ),
        None,
    )
    if not mime:
        return False
    try:
        with dest.open("wb") as handle:
            subprocess.run(
                ["wl-paste", "--type", mime],
                stdout=handle,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
    except Exception:
        dest.unlink(missing_ok=True)
        return False
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return False
    if mime != "image/png":
        return _convert_to_png(dest)
    return True


def _xclip_save(dest: Path) -> bool:
    try:
        targets = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    if targets.returncode != 0 or "image/png" not in targets.stdout:
        return False
    try:
        with dest.open("wb") as handle:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                stdout=handle,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
    except Exception:
        dest.unlink(missing_ok=True)
        return False
    return dest.exists() and dest.stat().st_size > 0


def _convert_to_png(path: Path) -> bool:
    if _is_png_file(path):
        return True
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.save(path, "PNG")
        return _is_png_file(path)
    except Exception:
        return path.exists() and path.stat().st_size > 0


def _is_png_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False
