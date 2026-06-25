"""Flowly CLI-side sandbox wrapping.

The Electron desktop wraps the agent process at spawn time (see
``flowly-desktop/src/main/local/sandbox/``). When a user runs the
CLI directly (``flowly gateway``, ``uv run flowly``, etc.) there is
no Electron in front to wrap, so the CLI wraps itself by re-execing
under the platform sandbox primitive.

The only public entry point is :func:`maybe_reexec_sandboxed` in
:mod:`flowly.sandbox.cli_wrap`. Call it at the very top of
:func:`flowly.cli.entry.main` and it either returns (no sandbox
wanted on this run) or replaces the current process via
``os.execve`` (sandbox active; the new process re-runs ``main()``
with the recursion guard set).

Trust model + policy live in ``SECURITY.md`` at the repo root.
"""
