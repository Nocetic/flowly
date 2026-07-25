"""What the installer leaves on screen is the product's first instruction.

Both installers used to close with a numbered list whose middle step was
``flowly service install --start`` — a command the user had to run before
anything worked. The CLI starts the gateway on demand now, so that step is
gone; these tests keep it gone, because the failure mode is silent (an
installer that still prescribes it simply looks like a longer product).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _get_started_block(text: str) -> str:
    """Everything from the 'Get started' heading to the end of its block."""
    marker = text.index("Get started")
    return text[marker : marker + 400]


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_get_started_does_not_prescribe_starting_the_gateway(script: str) -> None:
    block = _get_started_block((SCRIPTS / script).read_text(encoding="utf-8"))

    assert "flowly" in block
    assert "service install" not in block, (
        f"{script} still tells the user to start the gateway by hand"
    )


def test_unix_installer_offers_exactly_one_command() -> None:
    block = _get_started_block((SCRIPTS / "install.sh").read_text(encoding="utf-8"))
    commands = [
        line for line in block.splitlines() if "flowly" in line and "printf" in line
    ]

    assert len(commands) == 1
    assert "start chatting" in commands[0]
