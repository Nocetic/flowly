"""What the gateway says about itself, and what it refuses to say.

Desktop decides whether to take the local service over from these fields. A
wrong answer here is worse than none: `service_id` that does not match makes
a healthy gateway look like a stranger on the port, and a version that is not
really this build invites a downgrade that is not one.
"""
from __future__ import annotations

import aiohttp
import pytest

from flowly.gateway.identity import (
    GATEWAY_SERVICE_ID,
    VALID_OWNERS,
    gateway_version,
    health_identity,
    runtime_owner,
)


def test_the_service_id_is_the_exact_string_the_reader_expects():
    # Desktop treats a present-but-different id as proof that something else
    # holds the port and marks the whole health response invalid. This is the
    # one field where being wrong is worse than being absent.
    assert GATEWAY_SERVICE_ID == "ai.flowly.gateway"


def test_the_starter_is_believed_over_anything_inferred():
    # Desktop's LaunchAgent states this outright. A fact from the thing that
    # did the starting beats a path guess made afterwards.
    assert runtime_owner(env={"FLOWLY_SERVICE_OWNER": "desktop"}, executable="/x/.venv/bin/flowly") == "desktop"
    assert runtime_owner(env={"FLOWLY_RUNTIME_DISTRIBUTION": "desktop"}, executable="/x/.venv/bin/flowly") == "desktop"


def test_a_declared_owner_nobody_understands_is_not_passed_on():
    # Falls through to inference rather than forwarding a word the reader
    # would have to drop anyway.
    assert runtime_owner(env={"FLOWLY_SERVICE_OWNER": "whatever"}, executable="/x/.venv/bin/flowly") == "cli"
    assert runtime_owner(env={"FLOWLY_SERVICE_OWNER": ""}, executable="/x/.venv/bin/flowly") == "cli"


def test_the_path_says_the_rest_on_macos():
    assert runtime_owner(env={}, executable="/Applications/Flowly.app/Contents/MacOS/flowly-agent") == "desktop"
    assert runtime_owner(env={}, executable="/Users/x/.local/bin/flowly") == "cli"
    assert runtime_owner(env={}, executable="/Users/x/.local/share/uv/tools/flowly-ai/bin/flowly") == "cli"
    assert runtime_owner(env={}, executable="/opt/homebrew/bin/flowly") == "cli"
    assert runtime_owner(env={}, executable="/Users/x/repo/.venv/bin/flowly") == "cli"


def test_windows_paths_are_read_the_same_way():
    """Only the macOS service manager sets the environment variable.

    On Windows the path is the whole answer, and it is spelled with
    backslashes — which the POSIX-shaped markers would have missed, describing
    an ordinary CLI install as something hand-started.
    """
    assert runtime_owner(
        env={}, executable=r"C:\Users\x\AppData\Roaming\Flowly\resources\flowly-runtime\flowly-bin.exe"
    ) == "desktop"
    assert runtime_owner(env={}, executable=r"C:\proj\.venv\Scripts\flowly.exe") == "cli"
    assert runtime_owner(
        env={}, executable=r"C:\Users\x\AppData\Roaming\uv\tools\flowly-ai\Scripts\flowly.exe"
    ) == "cli"


def test_linux_paths_are_read_the_same_way():
    # Desktop ships the runtime loose under its resources here, with no
    # sub-bundle to recognise it by.
    assert runtime_owner(env={}, executable="/opt/Flowly/resources/flowly-runtime/flowly-bin") == "desktop"
    assert runtime_owner(env={}, executable="/home/x/.local/bin/flowly") == "cli"
    assert runtime_owner(env={}, executable="/usr/local/bin/flowly") == "cli"


def test_an_unrecognised_layout_says_nothing_rather_than_manual():
    """"Manual" is a claim, and from in here an unverifiable one.

    A path this build has not been taught is not the same thing as somebody
    running the gateway by hand — and Desktop treats "manual" as eligible for
    takeover, so guessing it could have Desktop offering to take over its own
    gateway on a platform whose layout was simply missing here.
    """
    assert runtime_owner(env={}, executable="/tmp/build/flowly") is None
    assert runtime_owner(env={}, executable=r"D:\somewhere\flowly.exe") is None


def test_nothing_is_claimed_when_there_is_nothing_to_go_on():
    assert runtime_owner(env={}, executable="") is None


def test_owner_is_always_a_word_the_reader_accepts():
    for executable in (
        "/Applications/Flowly.app/Contents/MacOS/flowly-agent",
        "/Users/x/.local/bin/flowly",
        "/opt/Flowly/resources/flowly-runtime/flowly-bin",
        r"C:\proj\.venv\Scripts\flowly.exe",
    ):
        assert runtime_owner(env={}, executable=executable) in VALID_OWNERS


def test_a_source_checkout_declines_to_name_a_version(monkeypatch):
    # `0.0.0-dev` is what a tree with no installed metadata reports. It would
    # compare as older than every release and read as "Desktop is newer, take
    # over" — a downgrade dressed as an upgrade.
    import flowly

    monkeypatch.setattr(flowly, "__version__", "0.0.0-dev", raising=False)
    assert gateway_version() is None
    assert "version" not in health_identity(env={}, executable="/tmp/x")


def test_a_real_version_is_published(monkeypatch):
    import flowly

    monkeypatch.setattr(flowly, "__version__", "3.1.0", raising=False)
    assert gateway_version() == "3.1.0"
    assert health_identity(env={}, executable="/tmp/x")["version"] == "3.1.0"


def test_the_payload_omits_what_it_does_not_know(monkeypatch):
    import flowly

    monkeypatch.setattr(flowly, "__version__", "0.0.0-dev", raising=False)
    identity = health_identity(env={}, executable="")
    # The id is the one thing always true of this process.
    assert identity == {"service_id": GATEWAY_SERVICE_ID}


def test_the_payload_is_flat_strings_a_json_client_can_read(monkeypatch):
    import flowly

    monkeypatch.setattr(flowly, "__version__", "3.1.0", raising=False)
    identity = health_identity(
        env={"FLOWLY_SERVICE_OWNER": "desktop"}, executable="/tmp/x"
    )
    assert identity == {
        "service_id": GATEWAY_SERVICE_ID,
        "version": "3.1.0",
        "runtime_owner": "desktop",
    }
    assert all(isinstance(value, str) for value in identity.values())


@pytest.mark.asyncio
async def test_the_running_gateway_actually_publishes_its_identity(
    tmp_path, monkeypatch
):
    """The wire, not the helper.

    The helper being right is not the same as the field arriving. Desktop
    reads this exact response, and the one field it will reject the whole
    payload over is `service_id`.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FLOWLY_SERVICE_OWNER", "desktop")

    import flowly
    from flowly.gateway.server import GatewayServer

    monkeypatch.setattr(flowly, "__version__", "3.1.0", raising=False)

    server = GatewayServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{server.port}/health"
            ) as response:
                assert response.status == 200
                payload = await response.json()
    finally:
        await server.stop()

    assert payload["status"] == "ok"
    assert payload["service_id"] == "ai.flowly.gateway"
    assert payload["version"] == "3.1.0"
    assert payload["runtime_owner"] == "desktop"
    # What was already there has to stay there: an older reader checks these
    # and nothing else.
    assert payload["auth_required"] is False
    assert "tool_events" in payload["capabilities"]
