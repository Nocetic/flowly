"""Which packages and which browser a host's screen installation uses.

One stdlib-only source, executed both for the read-only plan (what the person
reviews) and inside the isolated privileged worker (what runs as root), so the
two cannot drift. Only `/etc/os-release`, the machine architecture and fixed
path probes decide the recipe; nothing comes from a client. Python 3.9
compatible: the worker runs under the host's own `/usr/bin/python3`.
"""

import os

RECIPE_SOURCE = r'''
import hashlib as _recipe_hashlib
import json as _recipe_json

RECIPE_VERSION = 1
BROWSER_ROOT = "/opt/flowly-screen/chromium"
APPARMOR_PROFILE = "/etc/apparmor.d/flowly-screen-chromium"
APPARMOR_KNOB = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
APPARMOR_PARSER = "/usr/sbin/apparmor_parser"
# The userns rule needs AppArmor 4 (Ubuntu 24.04+). A 22.04 host on a newer
# kernel may have the knob with an AppArmor 3 parser that cannot express it.
APPARMOR_ABI4 = "/etc/apparmor.d/abi/4.0"

# Chromium pinned to the screen extra's Playwright (1.58.0 -> revision 1208).
# x86_64 is Google's Chrome for Testing build; Google publishes no Linux arm64
# build, so arm64 is Playwright's. Size and SHA-256 are verified before use;
# the mirrors serve the same bytes.
PINNED_BROWSER = {
    "version": "145.0.7632.6",
    "x86_64": {
        "directory": "chrome-linux64",
        "size": 175440843,
        "sha256": "b5e3195041af345a668d110f5daf5581961fa3608626ea588c97dd0fe81c4e38",
        "urls": [
            "https://storage.googleapis.com/chrome-for-testing-public/145.0.7632.6/linux64/chrome-linux64.zip",
            "https://cdn.playwright.dev/chrome-for-testing-public/145.0.7632.6/linux64/chrome-linux64.zip",
        ],
    },
    "aarch64": {
        "directory": "chrome-linux",
        "size": 188304613,
        "sha256": "70b4620521b1ada360475e1059dfe9d549a6461a79b9342f85b7d47e44123077",
        "urls": [
            "https://cdn.playwright.dev/dbazure/download/playwright/builds/chromium/1208/chromium-linux-arm64.zip",
            "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/chromium/1208/chromium-linux-arm64.zip",
        ],
    },
}

# The desktop around the browser: same names on every apt release.
APT_DESKTOP = (
    "tigervnc-standalone-server", "xauth", "dbus-x11", "xdotool", "x11-utils", "fonts-dejavu-core",
    "xfwm4", "xfdesktop4", "xfce4-panel", "xfce4-settings", "xfce4-terminal", "xfconf",
    "adwaita-icon-theme", "thunar", "python3", "bash", "nftables",
)
DEBIAN_BROWSER = ("chromium", "chromium-sandbox")
# Libraries the pinned Chromium needs (Playwright's own per-release tables);
# 24.04 renamed several for the 64-bit time_t transition. Newer releases use
# the newest table, as Playwright does.
UBUNTU_BROWSER_LIBRARIES = {
    "22.04": (
        "libasound2", "libatk-bridge2.0-0", "libatk1.0-0", "libatspi2.0-0", "libcairo2", "libcups2",
        "libdbus-1-3", "libdrm2", "libgbm1", "libglib2.0-0", "libnspr4", "libnss3", "libpango-1.0-0",
        "libwayland-client0", "libx11-6", "libxcb1", "libxcomposite1", "libxdamage1", "libxext6",
        "libxfixes3", "libxkbcommon0", "libxrandr2",
    ),
    "24.04": (
        "libasound2t64", "libatk-bridge2.0-0t64", "libatk1.0-0t64", "libatspi2.0-0t64", "libcairo2",
        "libcups2t64", "libdbus-1-3", "libdrm2", "libgbm1", "libglib2.0-0t64", "libnspr4", "libnss3",
        "libpango-1.0-0", "libx11-6", "libxcb1", "libxcomposite1", "libxdamage1", "libxext6",
        "libxfixes3", "libxkbcommon0", "libxrandr2",
    ),
}
# Fedora and Enterprise Linux 9 (Rocky, Alma, CentOS Stream): the
# distribution's Chromium; EL9 takes Xfce and Chromium from EPEL (with CRB).
# EL10 has no Xorg server or Xfce at all, so no recipe; RHEL itself needs a
# subscription to enable CRB, so none yet either.
DNF_DESKTOP = (
    "tigervnc-server-minimal", "xorg-x11-xauth", "dbus-daemon", "dbus-x11", "xdotool", "xkbcomp",
    "dejavu-sans-fonts", "xfwm4", "xfdesktop", "xfce4-panel", "xfce4-settings", "xfce4-terminal", "xfconf",
    "adwaita-icon-theme", "Thunar", "python3", "bash", "nftables", "chromium",
)
EL9_IDS = ("rocky", "almalinux", "centos")
# Derivatives (Mint, Pop!_OS, ...) name the Ubuntu release they are built on.
UBUNTU_CODENAMES = {"jammy": "22.04", "noble": "24.04", "oracular": "24.10", "plucky": "25.04",
                    "questing": "25.10"}

def os_release():
    """/etc/os-release as a dict (Python 3.9 has no platform.freedesktop_os_release)."""
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as source:
                text = source.read(65536)
        except (OSError, UnicodeError):
            continue
        values = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
                for escaped in ("\\\"", "\\'", "\\$", "\\`", "\\\\"):
                    value = value.replace(escaped, escaped[1:])
            values[key.strip()] = value
        return values
    return {}

def _release_version(text):
    parts = text.split(".") if isinstance(text, str) else []
    if not 1 <= len(parts) <= 2 or not all(part.isdigit() and len(part) <= 4 for part in parts):
        return None
    return tuple(int(part) for part in parts)

def screen_recipe(release, machine, exists):
    """The reviewed recipe for this host, or None where none exists yet."""
    if machine not in PINNED_BROWSER or not isinstance(release, dict):
        return None
    if exists("/usr/bin/apt-get") and exists("/usr/bin/dpkg"):
        return _apt_recipe(release, machine, exists)
    if exists("/usr/bin/dnf") and exists("/usr/bin/rpm"):
        return _dnf_recipe(release, machine)
    return None

def _dnf_recipe(release, machine):
    version = _release_version(release.get("VERSION_ID"))
    if version is None:
        return None
    if release.get("ID") == "fedora" and version >= (41,):
        distribution, packages, repositories = "fedora-%d" % version[0], DNF_DESKTOP + ("xprop",), []
    elif release.get("ID") in EL9_IDS and version[0] == 9:
        distribution, packages = "%s-9" % release["ID"], DNF_DESKTOP + ("xorg-x11-utils",)
        repositories = ["epel", "crb"]
    else:
        return None
    return {"recipeVersion": RECIPE_VERSION, "family": "dnf", "distribution": distribution,
            "architecture": machine, "packages": list(packages), "repositories": repositories,
            "browser": {"source": "distribution", "package": "chromium"}, "apparmor": False}

def _apt_recipe(release, machine, exists):
    family = [release.get("ID", "")] + release.get("ID_LIKE", "").split()
    if release.get("ID") == "debian":
        version = _release_version(release.get("VERSION_ID"))
        if version is None or version < (12,):
            return None
        packages = APT_DESKTOP[:3] + DEBIAN_BROWSER + APT_DESKTOP[3:]
        return {"recipeVersion": RECIPE_VERSION, "family": "apt", "distribution": "debian-%d" % version[0],
                "architecture": machine, "packages": list(packages), "repositories": [],
                "browser": {"source": "distribution", "package": "chromium"}, "apparmor": False}
    if "ubuntu" in family:
        label = release.get("VERSION_ID") if release.get("ID") == "ubuntu" else UBUNTU_CODENAMES.get(
            release.get("UBUNTU_CODENAME", ""))
        version = _release_version(label)
        if version is None or version < (22, 4):
            return None
        libraries = UBUNTU_BROWSER_LIBRARIES["24.04" if version >= (24, 4) else "22.04"]
        pinned = PINNED_BROWSER[machine]
        # Ubuntu 23.10+ kernels can deny unprivileged user namespaces; the
        # profile lets only the pinned browser create its sandbox's.
        apparmor = bool(exists(APPARMOR_KNOB) and exists(APPARMOR_PARSER) and exists(APPARMOR_ABI4))
        return {"recipeVersion": RECIPE_VERSION, "family": "apt", "distribution": "ubuntu-%d.%02d" % version,
                "architecture": machine, "packages": list(APT_DESKTOP + libraries), "repositories": [],
                "browser": {"source": "pinned", "version": PINNED_BROWSER["version"], "size": pinned["size"],
                            "sha256": pinned["sha256"]},
                "apparmor": apparmor}
    return None

def recipe_id(recipe):
    canonical = _recipe_json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    return _recipe_hashlib.sha256(canonical.encode()).hexdigest()
'''


def _namespace(source: str) -> dict:
    namespace: dict = {}
    exec(compile(source, "<screen-install-recipe>", "exec"), namespace)
    return namespace


_RECIPE = _namespace(RECIPE_SOURCE)
screen_recipe = _RECIPE["screen_recipe"]
os_release = _RECIPE["os_release"]
recipe_id = _RECIPE["recipe_id"]
PINNED_BROWSER = _RECIPE["PINNED_BROWSER"]
BROWSER_ROOT = _RECIPE["BROWSER_ROOT"]


# Root-only, stdlib-only steps of the privileged worker for a pinned browser.
# Appended to the worker after RECIPE_SOURCE. Every path is fixed here.
BROWSER_SOURCE = r'''
import hashlib
import json
import os
import shutil as _browser_shutil
import stat
import tempfile
import time
from pathlib import Path
import ssl as _browser_ssl
import urllib.request as _browser_urllib
import zipfile as _browser_zipfile
from pathlib import PurePosixPath as _BrowserPurePath

BROWSER_MANIFEST = "browser.json"
DOWNLOAD_SECONDS = 600

class BrowserInstallError(Exception):
    """code: INSTALL_DOWNLOAD_FAILED or INSTALL_BROWSER_FAILED; nothing half-done."""
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code

def _root_directory(path, create=False):
    # Every component must be a root-owned directory nobody else can write.
    for component in reversed((path,) + tuple(path.parents)):
        if create and not component.exists():
            component.mkdir(mode=0o755)
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise BrowserInstallError("INSTALL_BROWSER_FAILED")

def browser_manifest_valid(version, machine):
    root = Path(BROWSER_ROOT)
    try:
        _root_directory(root)
        fd = os.open(root / BROWSER_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return False
            value = json.loads(source.read(4096))
        executable = root / version / PINNED_BROWSER[machine]["directory"] / "chrome"
        info = executable.lstat()
        return (value == {"browserVersion": 1, "version": version, "executable": str(executable)}
                and stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and bool(info.st_mode & 0o100))
    except (OSError, ValueError, BrowserInstallError):
        return False

def _download(build, directory):
    """A root-owned temporary file holding exactly the pinned bytes."""
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    fd, name = tempfile.mkstemp(prefix=".download-", dir=directory)
    os.close(fd)
    context = _browser_ssl.create_default_context()
    # No proxy or other environment reaches the root worker (ENV is fixed).
    opener = _browser_urllib.build_opener(_browser_urllib.ProxyHandler({}), _browser_urllib.HTTPSHandler(context=context))
    for url in build["urls"]:
        digest, size = hashlib.sha256(), 0
        try:
            request = _browser_urllib.Request(url, headers={"User-Agent": "Flowly-screen-installer"})
            with opener.open(request, timeout=60) as response, open(name, "wb") as output:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > build["size"]:
                        raise ValueError()
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == build["size"] and digest.hexdigest() == build["sha256"]:
                return name
        except Exception:
            # Any network, TLS, HTTP or size problem: try the next mirror.
            continue
    os.unlink(name)
    raise BrowserInstallError("INSTALL_DOWNLOAD_FAILED")

def _extract(archive, build, staging):
    top = build["directory"]
    with _browser_zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            parts = _BrowserPurePath(entry.filename).parts
            mode = entry.external_attr >> 16
            # Only plain files and directories under the expected top directory.
            if (not parts or entry.filename.startswith("/") or ".." in parts or parts[0] != top
                    or stat.S_ISLNK(mode) or not (entry.is_dir() or stat.S_ISREG(mode) or mode == 0)):
                raise BrowserInstallError("INSTALL_BROWSER_FAILED")
            target = staging.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with bundle.open(entry) as source, open(target, "xb") as output:
                _browser_shutil.copyfileobj(source, output, 1 << 20)
            # Never keep setuid/setgid or group/other write bits from an archive.
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)

def install_pinned_browser(machine):
    """Download, verify and publish the pinned browser; the manifest last."""
    version, build = PINNED_BROWSER["version"], PINNED_BROWSER[machine]
    if browser_manifest_valid(version, machine):
        return
    root = Path(BROWSER_ROOT)
    _root_directory(root, create=True)
    archive = _download(build, root)
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
        os.chmod(staging, 0o755)
        try:
            _extract(archive, build, staging)
        except (OSError, ValueError, _browser_zipfile.BadZipFile):
            raise BrowserInstallError("INSTALL_BROWSER_FAILED") from None
        final = root / version
        if final.is_symlink():
            final.unlink()
        elif final.exists():
            # An earlier unpublished copy (no valid manifest): replaced whole.
            _browser_shutil.rmtree(final)
        os.rename(staging, final)
        staging = None
        executable = final / build["directory"] / "chrome"
        fd, name = tempfile.mkstemp(prefix=".manifest-", dir=root)
        with os.fdopen(fd, "w") as output:
            json.dump({"browserVersion": 1, "version": version, "executable": str(executable)}, output)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(name, root / BROWSER_MANIFEST)
    finally:
        os.unlink(archive)
        if staging is not None:
            _browser_shutil.rmtree(staging, ignore_errors=True)

def apparmor_profile_text(abi=True):
    return ("# Flowly Screen: only the pinned Chromium may create the user namespaces its\n"
            "# sandbox needs where the kernel restricts them (Ubuntu 23.10+). Sandbox stays on.\n"
            + ("abi <abi/4.0>,\n" if abi else "")
            + "include <tunables/global>\n\n"
            + "profile flowly-screen-chromium " + BROWSER_ROOT + "/*/chrome-linux*/chrome flags=(unconfined) {\n"
            + "  userns,\n  include if exists <local/flowly-screen-chromium>\n}\n")

def install_apparmor_profile():
    text = apparmor_profile_text(True)
    directory = Path(APPARMOR_PROFILE).parent
    fd, name = tempfile.mkstemp(prefix=".flowly-screen-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(name, APPARMOR_PROFILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    code, _ = command([APPARMOR_PARSER, "-r", APPARMOR_PROFILE], 60)
    if code:
        raise BrowserInstallError("INSTALL_BROWSER_FAILED")
'''


_BROWSER = _namespace(RECIPE_SOURCE + BROWSER_SOURCE)


def screen_browser() -> str | None:
    """The browser a screen runs: the pinned one this installer published (its
    root-owned manifest and binary checked), else the distribution's own
    Chromium. Root hosts look only in /usr/bin:/bin; an unprivileged agent may
    use its PATH, as before. A snap Chromium (Ubuntu's chromium-browser shim,
    /snap/bin) is never used: its confinement breaks the screen's layout."""
    import platform
    import shutil

    machine, version = platform.machine(), PINNED_BROWSER["version"]
    if machine in PINNED_BROWSER and _BROWSER["browser_manifest_valid"](version, machine):
        return os.path.join(BROWSER_ROOT, version, PINNED_BROWSER[machine]["directory"], "chrome")
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    # Debian names it chromium; Fedora and EPEL chromium-browser (a wrapper).
    for name in ("chromium", "chromium-browser"):
        found = shutil.which(name, path="/usr/bin:/bin" if root else None)
        if found and not _snap(found):
            return os.path.abspath(found)
    return None


def _snap(path: str) -> bool:
    """A snap Chromium, or Ubuntu's transitional script that starts one."""
    if "/snap/" in path or os.path.basename(os.path.realpath(path)) == "snap":
        return True
    try:
        with open(path, "rb") as source:
            head = source.read(65536)
    except OSError:
        # Unreadable is not evidence of a snap shim; starting it will tell.
        return False
    return head.startswith(b"#!") and b"/snap/" in head
