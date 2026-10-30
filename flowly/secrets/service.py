"""What the RPCs, the tools and the command runners ask of this agent's secrets.

One place decides what a save must satisfy, so a card in the chat and a row
in Settings are held to the same rules: a name a secret may have, the key the
values were sealed to, the request they answer, values that are text of a
sane size and not a card number (cards belong in a vault that fills pages,
never in a command's environment).
"""

from __future__ import annotations

import re
from typing import Any

from flowly.secrets import envelope
from flowly.secrets.names import SecretNameError, check_name
from flowly.secrets.requests import MAX_FIELDS, get_secret_requests
from flowly.secrets.vault import MAX_VALUE_BYTES, SecretsError, get_vault

_CARD_DIGITS = re.compile(r"[\s-]")


def looks_like_card(value: str) -> bool:
    """13 to 19 digits (spaces and dashes aside) that pass the Luhn check."""
    digits = _CARD_DIGITS.sub("", value)
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _names(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FIELDS:
        raise SecretsError(f"Name 1 to {MAX_FIELDS} secrets.", "BAD_NAME")
    try:
        names = [check_name(name) for name in raw]
    except SecretNameError as exc:
        raise SecretsError(str(exc), "BAD_NAME") from None
    if len(set(names)) != len(names):
        raise SecretsError("A name is given twice.", "BAD_NAME")
    return names


def status() -> dict[str, Any]:
    """The key an app seals values to, and how many secrets are saved."""
    data = get_vault().load_or_create()
    return {"key": data.key_wire(), "count": len(data.secrets)}


def list_secrets() -> dict[str, Any]:
    data = get_vault().load_or_create()
    return {"key": data.key_wire(),
            "secrets": [secret.summary(name) for name, secret in sorted(data.secrets.items())]}


def saved_names() -> list[str]:
    """The names saved, for the agent (never a value); empty when locked."""
    try:
        data = get_vault().load()
    except SecretsError:
        return []
    return sorted(data.secrets) if data else []


async def submit(params: dict[str, Any]) -> dict[str, Any]:
    """Open values an app sealed to this agent and save them."""
    names = _names(params.get("names"))
    request_id = params.get("requestId") or None
    if request_id is not None and not isinstance(request_id, str):
        raise SecretsError("The request id is not text.", "REQUEST_NOT_FOUND")
    vault = get_vault()
    data = vault.load_or_create()
    if params.get("kid") != data.kid:
        raise SecretsError("This agent's key changed; seal the values again.", "KEY_CHANGED")
    requests = get_secret_requests()
    request = None
    if request_id is not None:
        request = requests.get(request_id)
        if request is None:
            raise SecretsError("This request is no longer waiting.", "REQUEST_NOT_FOUND")
        if sorted(request.names) != sorted(names):
            raise SecretsError("These are not the names the agent asked for.", "NAMES_MISMATCH")
    try:
        opened = envelope.open_sealed(params.get("sealed"), data.private_key, kid=data.kid,
                                      request_id=request_id, names=names)
    except envelope.EnvelopeError:
        raise SecretsError("The values could not be opened.", "DECRYPT_FAILED") from None
    for name, value in opened.values.items():
        if not value.strip():
            raise SecretsError(f"{name} is empty.", "EMPTY_VALUE")
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise SecretsError(f"{name} is longer than {MAX_VALUE_BYTES // 1024} KiB.", "TOO_LARGE")
        if looks_like_card(value):
            raise SecretsError("A card number is not saved as a secret.", "LOOKS_LIKE_CARD")
    description = request.description if request is not None else opened.description
    saved = vault.put(opened.values, description)
    _audit("secret.saved", saved, request_id)
    if request is not None:
        await requests.close(request.id, "saved")
    else:
        # Saved from Settings: a request waiting for exactly these is answered too.
        for waiting in requests.pending():
            if set(waiting.names) <= set(saved):
                await requests.close(waiting.id, "saved")
    return {"saved": saved}


async def decline(params: dict[str, Any]) -> dict[str, Any]:
    request_id = params.get("requestId")
    if not isinstance(request_id, str) or await get_secret_requests().close(request_id, "declined") is None:
        raise SecretsError("This request is no longer waiting.", "REQUEST_NOT_FOUND")
    return {"declined": True}


def delete(params: dict[str, Any]) -> dict[str, Any]:
    try:
        name = check_name(params.get("name"))
    except SecretNameError as exc:
        raise SecretsError(str(exc), "BAD_NAME") from None
    deleted = get_vault().delete(name)
    if deleted:
        _audit("secret.deleted", [name], None)
    return {"deleted": deleted}


def pending_requests() -> dict[str, Any]:
    return {"requests": [request.to_wire() for request in get_secret_requests().pending()]}


def command_env() -> dict[str, str]:
    """The saved secrets as a command's environment; empty when none or locked.

    A secret named like a provider key Flowly strips from commands
    (``OPENROUTER_API_KEY``) is given anyway: it is the person's own value,
    handed over on purpose. :func:`forced_env` marks it for ``env_scrub``.
    """
    vault = get_vault()
    try:
        data = vault.load()
    except SecretsError:
        return {}
    except Exception:
        return {}
    if not data or not data.secrets:
        return {}
    vault.mark_used(list(data.secrets))
    return {name: secret.value for name, secret in data.secrets.items()}


def names_in_command(command: str) -> list[str]:
    """The saved secrets a command names: ``$NAME``, ``${NAME}``, ``$env:NAME``, ``%NAME%``.

    For an approval to say what the command is given; a script that reads one
    by itself is not seen here (every command gets them all).
    """
    saved = saved_names()
    if not saved or not isinstance(command, str):
        return []
    return [name for name in saved
            if re.search(rf"(\$\{{?{name}\b|\$env:{name}\b|%{name}%)", command, flags=re.IGNORECASE)]


def approval_reasons(command: str) -> list[str]:
    """What an approval adds about saved secrets (shown with its other reasons)."""
    return [f"Uses saved secret {name}" for name in names_in_command(command)]


def forced_env() -> dict[str, str]:
    """:func:`command_env` in the form ``sanitize_subprocess_env`` passes through."""
    from flowly.exec.env_scrub import _FORCE_PREFIX

    return {f"{_FORCE_PREFIX}{name}": value for name, value in command_env().items()}


def _audit(event: str, names: list[str], request_id: str | None) -> None:
    try:
        from flowly.account import audit_log

        audit_log.info(event, names=",".join(names), request=request_id or "")
    except Exception:
        pass
