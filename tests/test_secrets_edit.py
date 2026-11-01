"""Settings → Secrets: a saved key's last characters, and editing it without ever reading it back (flowly.secrets).

A long value shows its last three characters so the person can tell which key
is saved; a short one shows nothing. Editing changes the description or the
name, never reveals the value; a new value is still a sealed ``secrets.submit``.
The model never sees the hint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flowly.agent.tools.secrets import ListSecretsTool
from flowly.secrets import envelope, redact, service
from flowly.secrets import requests as secret_requests
from flowly.secrets import vault as vault_mod
from flowly.secrets.vault import SecretsError

LONG = "sk-or-v1-9f8e7d6c5b4a3a1f"
SHORT = "abc12345"


@pytest.fixture
def saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(vault_mod, "_keyring", lambda: None)
    for module in (vault_mod, secret_requests, redact):
        module._reset_for_tests()
    key = service.status()["key"]
    for name, value in (("OPENROUTER_API_KEY", LONG), ("SHORT_PIN", SHORT)):
        sealed = envelope.seal({name: value}, envelope.unb64url(key["publicKey"]), kid=key["kid"], request_id=None,
                               description="For models")
        asyncio.run(service.submit({"names": [name], "kid": key["kid"], "sealed": sealed}))
    yield
    for module in (vault_mod, secret_requests, redact):
        module._reset_for_tests()


def listed() -> dict[str, dict]:
    return {entry["name"]: entry for entry in service.list_secrets()["secrets"]}


def test_a_long_value_shows_its_last_three_characters_and_a_short_one_nothing(saved):
    secrets = listed()
    assert secrets["OPENROUTER_API_KEY"]["hint"] == "a1f"
    assert secrets["SHORT_PIN"]["hint"] is None
    assert LONG not in json.dumps(service.list_secrets())


async def test_the_model_never_sees_the_hint(saved):
    shown = await ListSecretsTool().execute()
    assert "a1f" not in shown and "hint" not in shown


def test_editing_changes_the_description_and_keeps_the_value(saved):
    assert service.update({"name": "OPENROUTER_API_KEY", "description": "Image models"}) == {"name": "OPENROUTER_API_KEY"}
    assert listed()["OPENROUTER_API_KEY"]["description"] == "Image models"
    assert service.secret_values(["OPENROUTER_API_KEY"]) == {"OPENROUTER_API_KEY": LONG}


def test_a_renamed_secret_moves_with_its_value(saved):
    assert service.update({"name": "OPENROUTER_API_KEY", "newName": "OPENROUTER_KEY"}) == {"name": "OPENROUTER_KEY"}
    assert "OPENROUTER_API_KEY" not in listed()
    assert service.secret_values(["OPENROUTER_KEY"]) == {"OPENROUTER_KEY": LONG}
    assert listed()["OPENROUTER_KEY"]["description"] == "For models"


def test_a_rename_onto_a_saved_name_or_an_unknown_one_is_refused(saved):
    with pytest.raises(SecretsError) as taken:
        service.update({"name": "OPENROUTER_API_KEY", "newName": "SHORT_PIN"})
    assert taken.value.code == "NAME_TAKEN"
    with pytest.raises(SecretsError) as missing:
        service.update({"name": "NOT_SAVED", "description": "x"})
    assert missing.value.code == "NOT_FOUND"
    with pytest.raises(SecretsError) as bad:
        service.update({"name": "OPENROUTER_API_KEY", "newName": "PATH"})
    assert bad.value.code == "BAD_NAME"
    assert service.secret_values(["OPENROUTER_API_KEY"]) == {"OPENROUTER_API_KEY": LONG}
