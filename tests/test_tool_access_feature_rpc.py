"""Owner-facing model tool access policy over the shared feature RPC."""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.channels import feature_rpc
from flowly.config.loader import load_config


class _Registry:
    def get_toolsets(self) -> dict[str, str]:
        return {
            "read_file": "filesystem",
            "write_file": "filesystem",
            "exec": "execution",
            "mcp_calendar_list": "mcp",
        }


@pytest.fixture(autouse=True)
def isolated_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    feature_rpc.set_tool_access_provider(None)
    yield
    feature_rpc.set_tool_access_provider(None)


def test_access_payload_is_sanitized_and_uses_live_registry() -> None:
    feature_rpc.set_tool_access_provider(lambda: _Registry())

    payload = feature_rpc.tools_access_get()

    groups = {group["name"]: group for group in payload["groups"]}
    assert groups["filesystem"] == {
        "name": "filesystem",
        "enabled": True,
        "available": True,
        "tools": ["read_file", "write_file"],
    }
    assert groups["mcp"]["tools"] == ["mcp_calendar_list"]
    assert "config" not in payload


@pytest.mark.asyncio
async def test_access_set_persists_denylist_and_applies_live() -> None:
    refreshed = []
    feature_rpc.set_tool_access_provider(
        lambda: _Registry(),
        lambda: refreshed.append(True),
    )

    result = await feature_rpc.tools_access_set({
        "disabledToolsets": ["filesystem", "execution"],
    })

    assert result["ok"] is True
    assert result["willRestart"] is False
    assert result["disabledToolsets"] == ["execution", "filesystem"]
    assert refreshed == [True]
    assert load_config().tools.routing.disabled_toolsets == ["execution", "filesystem"]


@pytest.mark.asyncio
async def test_access_set_rejects_unknown_group() -> None:
    feature_rpc.set_tool_access_provider(lambda: _Registry())

    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.tools_access_set({"disabledToolsets": ["not-a-toolset"]})

    assert raised.value.code == "INVALID"


def test_access_methods_are_advertised() -> None:
    methods = feature_rpc.system_capabilities()["featureMethods"]
    assert "tools.access.get" in methods
    assert "tools.access.set" in methods
