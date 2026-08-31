"""What this gateway tells a client about itself.

Desktop has to answer two questions before it can decide anything about the
local service: which version is running, and who started it. Both answers were
being reconstructed from the outside — read a launchd plist, extract an
executable from a process command line, run it with ``--version``, parse the
output. Every step of that can be missing or wrong on a perfectly healthy
machine, and when any of it fails Desktop reports that it "could not compare
the CLI and Desktop versions" and refuses to act.

The gateway knows both answers about itself, for certain. It publishes them on
``/health``, which Desktop already reads and already looks for these exact
fields in. Nothing else needs to change on that side.
"""
from __future__ import annotations

import os
import sys

#: Desktop rejects a health payload whose ``service_id`` is present and not
#: this value — it reads a wrong id as "something else is on this port". The
#: constant is duplicated there deliberately; both sides must agree exactly.
GATEWAY_SERVICE_ID = "ai.flowly.gateway"

#: The only owners a client will accept. Anything else is dropped rather than
#: sent, because a value the reader does not recognise is worse than no value:
#: it looks like an answer.
VALID_OWNERS = ("desktop", "cli", "manual")

#: Where Desktop keeps its bundled runtime. macOS wraps it in a sub-bundle;
#: Windows and Linux ship it loose under the app's resources, and on neither
#: of those does Desktop set the environment variable — the macOS service
#: manager is the only thing that does. Without these markers a packaged
#: Desktop runtime on Windows or Linux would be described as something else.
_DESKTOP_MARKERS = (
    ".app/contents/",
    "/resources/flowly-runtime/",
)

#: Where a CLI installation puts its executables. Mirrors the classification
#: Desktop applies to a process command line, so the two agree about the same
#: process whichever way it is asked.
_CLI_MARKERS = (
    "/.venv/",
    "/venv/",
    "/uv/tools/",
    "/.local/",
    "/usr/local/bin/",
    "/opt/homebrew/bin/",
    # Windows keeps a virtual environment's executables here; the separator is
    # normalised before matching, so one spelling covers every platform.
    "/appdata/roaming/uv/",
)


def runtime_owner(
    env: dict[str, str] | None = None,
    executable: str | None = None,
) -> str | None:
    """Who started this gateway: ``desktop``, ``cli``, ``manual``, or unknown.

    The environment is asked first because Desktop already answers it. Its
    LaunchAgent sets ``FLOWLY_SERVICE_OWNER=desktop`` — a fact stated by the
    thing that did the starting, which beats anything inferred afterwards.

    Otherwise the executable's own path decides, by the same markers Desktop
    uses on a command line. Inside an app bundle is Desktop; inside a virtual
    environment or a user bin directory is an installation; anything else is
    somebody running it by hand.

    Returns None when neither says anything definite. Sending nothing lets the
    reader fall back to its own classification, which is what it did before
    this existed; sending a guess would override a better answer with a worse
    one.
    """
    source = os.environ if env is None else env
    declared = str(source.get("FLOWLY_SERVICE_OWNER", "") or "").strip().lower()
    if declared in VALID_OWNERS:
        return declared
    # `distribution` is the same fact under the name Desktop's older payloads
    # used; accepting both means neither side has to be upgraded first.
    declared = str(source.get("FLOWLY_RUNTIME_DISTRIBUTION", "") or "").strip().lower()
    if declared in VALID_OWNERS:
        return declared

    path = (executable if executable is not None else sys.argv[0] or sys.executable) or ""
    # Windows spells the same path with backslashes. Normalising once means the
    # markers are written a single way instead of twice, and a Windows CLI
    # install stops reading as something hand-started.
    lowered = path.replace("\\", "/").lower()
    if not lowered:
        return None
    if any(marker in lowered for marker in _DESKTOP_MARKERS):
        return "desktop"
    if any(marker in lowered for marker in _CLI_MARKERS):
        return "cli"
    # Deliberately not "manual". From in here an unrecognised path is a layout
    # this build has not been taught, which is not the same thing as somebody
    # running the gateway by hand — and Desktop treats "manual" as eligible for
    # takeover, so guessing it could have Desktop offering to take over its own
    # gateway on a platform whose layout was missing. Saying nothing leaves the
    # reader with its own classification, which knows where it put things.
    return None


def gateway_version() -> str | None:
    """This build's version, or None when the checkout cannot state one.

    A source tree with no installed metadata reports ``0.0.0-dev``, which is
    true but useless to compare against a release: it would read as older than
    every real version and invite a downgrade that is not one. Withholding it
    leaves the reader where it was before, which is the honest position for a
    build that does not know what it is.
    """
    try:
        from flowly import __version__
    except Exception:  # pragma: no cover - import cannot realistically fail
        return None
    version = str(__version__ or "").strip()
    if not version or version.startswith("0.0.0"):
        return None
    return version


def health_identity(
    env: dict[str, str] | None = None,
    executable: str | None = None,
) -> dict[str, str]:
    """The identity fields for ``/health``, omitting whatever is not known.

    Absent is a valid answer everywhere here. A reader that gets no version
    behaves exactly as it did before this shipped; a reader that gets a wrong
    one makes a confident decision on bad grounds, which is the failure this
    is meant to remove rather than relocate.
    """
    identity: dict[str, str] = {"service_id": GATEWAY_SERVICE_ID}
    version = gateway_version()
    if version:
        identity["version"] = version
    owner = runtime_owner(env=env, executable=executable)
    if owner:
        identity["runtime_owner"] = owner
    return identity
