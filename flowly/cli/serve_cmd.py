"""Desktop-managed profile runtime entry point."""

from __future__ import annotations

import typer

from flowly.cli.gateway_cmd import gateway


def serve(
    port: int = typer.Option(
        0,
        "--port",
        help="Loopback port; 0 asks the operating system for an unused port.",
    ),
    token: str = typer.Option(
        "",
        "--token",
        help="Per-process secret. A random secret is generated when omitted.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose runtime logs."),
) -> None:
    """Run one named profile as an authenticated local RPC backend."""
    gateway(
        port=port,
        verbose=verbose,
        persona="",
        host="127.0.0.1",
        remote=False,
        token=token,
        rotate_token=False,
        local_runtime=True,
    )
