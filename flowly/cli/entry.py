"""CLI entry point — extracts --profile BEFORE any Flowly module import.

This is critical because many modules evaluate ``get_flowly_home()`` at
import time (module-level constants).  By setting ``FLOWLY_HOME`` here,
before ``from flowly.cli.commands import app`` runs, every constant
resolves to the correct profile directory.

Resolution order:
  1. ``-p`` / ``--profile`` CLI flag  (highest priority)
  2. ``FLOWLY_PROFILE`` env var       (for wrapper scripts)
  3. existing ``FLOWLY_HOME`` env var (custom/isolated home)
  4. ``~/.flowly/active_profile``     (sticky default)
  5. ``"default"``                    (fallback)
"""

from __future__ import annotations

import os
import platform
import sys


def _configure_windows_utf8_stdio() -> None:
    """Force UTF-8 stdout/stderr on Windows so Rich's unicode (the ``✦`` logo,
    checkmarks) doesn't crash every command.

    Outside a UTF-8-aware terminal (a piped/redirected stream, a legacy
    console host, certain locales), Python defaults stdio to the legacy code
    page (cp1252 etc.) on Windows, and the first ``console.print`` with a
    non-cp1252 character raises ``UnicodeEncodeError`` — aborting even
    ``flowly --version``. ``errors="replace"`` degrades to ``?`` instead of
    crashing. Mirrors the fix already applied to the gateway subcommand
    (``gateway_cmd.py``); hoisted here so it covers every command, not just
    ``gateway``.
    """
    if platform.system() != "Windows":
        return
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _configure_bundled_ssl_ca() -> None:
    """Point Python's SSL stack at certifi's CA bundle when running as a
    Nuitka-compiled standalone binary.

    In a bundled binary there is no system CA store, so `ssl.create_default_context`
    fails to load any trusted roots and every HTTPS/WSS connection errors out with
    `CERTIFICATE_VERIFY_FAILED`. This hits the web channel (wss to the Flowly relay)
    and the openai SDK (https to the Flowly proxy) on first boot. Setting
    `SSL_CERT_FILE` (and REQUESTS_CA_BUNDLE for anything old-school) BEFORE any
    other flowly imports makes Python's ssl module pick up certifi's bundle at
    the next `create_default_context()` call.

    This must run before `from flowly.cli.commands import app` below, which
    transitively imports aiohttp / websockets / openai — all of which create
    their default SSL contexts at module load.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
        ca_path = certifi.where()
        if ca_path and os.path.exists(ca_path):
            os.environ["SSL_CERT_FILE"] = ca_path
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_path)
            os.environ.setdefault("CURL_CA_BUNDLE", ca_path)
    except Exception:
        # certifi missing or unreadable — fall back to whatever Python finds.
        pass


def main() -> None:
    """Parse profile flag, set FLOWLY_HOME, then launch the typer app."""
    # Belt-and-braces: do this before anything else might print.
    _configure_windows_utf8_stdio()

    # Sandbox first — may not return. If sandboxing is on and we
    # haven't already wrapped, this re-execs the current command
    # under sandbox-exec(1) and the next line never runs. The inner
    # process re-enters main() with the recursion guard set and falls
    # through to the rest of startup.
    #
    # This must precede the SSL config and profile resolution: the
    # outer process should do the absolute minimum before handing
    # control to the wrapped child.
    from flowly.sandbox.cli_wrap import maybe_reexec_sandboxed
    maybe_reexec_sandboxed()

    # Configure SSL CA bundle first — must happen before any flowly import so
    # aiohttp / websockets / openai pick up the correct trust store in the
    # Nuitka-bundled binary.
    _configure_bundled_ssl_ca()

    profile: str | None = None
    argv = sys.argv[1:]

    # 1. Extract -p / --profile from argv (before typer sees it)
    for i, arg in enumerate(argv):
        if arg in ("-p", "--profile") and i + 1 < len(argv):
            profile = argv[i + 1]
            sys.argv = [sys.argv[0]] + argv[:i] + argv[i + 2:]
            break
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
            sys.argv = [sys.argv[0]] + argv[:i] + argv[i + 1:]
            break

    # 2. Check FLOWLY_PROFILE env var (wrapper scripts set this)
    if profile is None:
        profile = os.environ.get("FLOWLY_PROFILE")

    existing_home = os.environ.get("FLOWLY_HOME")

    # 3. If FLOWLY_HOME is already explicit, preserve it. This keeps tests,
    # wrappers, and one-off isolated runs from being silently redirected back to
    # the default/sticky profile. -p / FLOWLY_PROFILE above still override it.
    if profile is None and existing_home:
        pass

    # 4. Check ~/.flowly/active_profile (sticky default)
    elif profile is None:
        from flowly.profile import get_active_profile
        active = get_active_profile()
        if active != "default":
            profile = active

    # 5. Set FLOWLY_HOME before ANY other flowly import, unless an explicit
    # FLOWLY_HOME was already present and no profile override was requested.
    if not (profile is None and existing_home):
        from flowly.profile import set_profile
        set_profile(profile)

    # NOW it's safe to import the rest of flowly
    from flowly.cli.commands import app
    app()


if __name__ == "__main__":
    main()
