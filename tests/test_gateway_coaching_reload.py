"""Gateway provider reload keeps Meeting Coach in lockstep with chat."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from flowly.cli.gateway_cmd import _reload_coaching_runtime


def test_reload_coaching_runtime_updates_provider_and_both_models():
    provider = object()
    manager = SimpleNamespace(reconfigure_llm=Mock())
    server = SimpleNamespace(_coaching_manager=manager)

    assert _reload_coaching_runtime(server, provider, "new-model") is True
    manager.reconfigure_llm.assert_called_once_with(
        provider,
        gate_model="new-model",
        summary_model="new-model",
    )


def test_reload_coaching_runtime_is_optional_when_coach_is_unavailable():
    server = SimpleNamespace(_coaching_manager=None)

    assert _reload_coaching_runtime(server, object(), "new-model") is False


def test_reload_coaching_runtime_propagates_partial_reload_failure():
    manager = SimpleNamespace(
        reconfigure_llm=Mock(side_effect=RuntimeError("cannot reconfigure")),
    )
    server = SimpleNamespace(_coaching_manager=manager)

    with pytest.raises(RuntimeError, match="cannot reconfigure"):
        _reload_coaching_runtime(server, object(), "new-model")
