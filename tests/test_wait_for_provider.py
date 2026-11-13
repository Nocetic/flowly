"""An agent installed before its owner signs in (Flowly Desktop's first launch,
2026-11-13): the gateway runs with no provider, says so, and never crash-loops."""

from __future__ import annotations

import asyncio
from pathlib import Path

from flowly.agent.error_classifier import ErrorCategory, classify_response, present_provider_error
from flowly.cli import service_cmd
from flowly.providers.no_provider import NO_PROVIDER_CODE, NoProvider


def test_an_agent_with_no_provider_answers_that_it_needs_one() -> None:
    response = asyncio.run(NoProvider("anthropic/claude-haiku-5.5").chat([{"role": "user", "content": "selam"}]))

    assert response.finish_reason == "error"
    assert response.error_info is not None and response.error_info.code == NO_PROVIDER_CODE
    # Not retried: a retry cannot give the agent a provider.
    assert classify_response(response) == ErrorCategory.AUTH
    shown = present_provider_error(response)
    assert shown.code == "NO_PROVIDER" and not shown.retryable
    assert "Sign in" in shown.message


def test_a_real_auth_failure_still_reads_as_one() -> None:
    from flowly.providers.base import LLMErrorInfo, LLMResponse

    shown = present_provider_error(LLMResponse(content="401", finish_reason="error", error_info=LLMErrorInfo(status_code=401)))
    assert shown.code == "PROVIDER_AUTHENTICATION_FAILED"


def test_a_service_starts_only_when_its_gateway_waits_for_a_provider(tmp_path: Path, monkeypatch) -> None:
    plist = tmp_path / "ai.flowly.gateway.plist"
    monkeypatch.setattr(service_cmd, "_service_paths", lambda label: (plist, None, None))

    assert service_cmd._service_waits_for_provider("ai.flowly.gateway") is False
    plist.write_text("<string>gateway</string><string>--port</string><string>18790</string>")
    # Installed by an older Core: with no provider it would crash-loop, so it is not started.
    assert service_cmd._service_waits_for_provider("ai.flowly.gateway") is False
    plist.write_text("<string>gateway</string><string>--wait-for-provider</string>")
    assert service_cmd._service_waits_for_provider("ai.flowly.gateway") is True


def test_a_windows_service_names_the_flag_in_its_supervisor(tmp_path: Path, monkeypatch) -> None:
    xml = tmp_path / "ai.flowly.gateway.xml"
    xml.write_text("<Exec>wscript</Exec>", encoding="utf-16")
    monkeypatch.setattr(service_cmd, "_service_paths", lambda label: (None, None, xml))
    xml.with_suffix(".vbs").write_text('cmd = "python.exe -m flowly gateway --wait-for-provider"')

    assert service_cmd._service_waits_for_provider("ai.flowly.gateway") is True
