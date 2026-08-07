"""`flowly gateway` takes the port over from the installed service itself.

There used to be a wrapper script for this; the logic now lives in the
gateway command. The contract:

  * free port → no-op, nothing asked, nothing touched;
  * busy port with NO installed service unit → not ours, left alone
    (the normal bind error explains it);
  * busy port + installed unit + no TTY → refuse with the two commands
    spelled out (exit 1), so automation never stops a service silently;
  * busy port + installed unit + TTY → confirm, stop via the CLI,
    signal the caller to restore on exit;
  * restore reinstalls with the same port and --start.
"""

from __future__ import annotations

import io
import sys

import pytest
import typer

from flowly.cli import gateway_cmd


def test_free_port_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: False)
    monkeypatch.setattr(
        gateway_cmd, "_installed_service_unit",
        lambda: (_ for _ in ()).throw(AssertionError("must not be consulted")),
    )
    assert gateway_cmd.take_over_port(18790) is False


def test_busy_port_without_a_unit_is_not_ours(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: True)
    monkeypatch.setattr(gateway_cmd, "_installed_service_unit", lambda: None)
    assert gateway_cmd.take_over_port(18790) is False


def test_no_tty_refuses_and_names_both_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: True)
    monkeypatch.setattr(gateway_cmd, "_installed_service_unit", lambda: object())
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # isatty() → False

    with pytest.raises(typer.Exit) as exc:
        gateway_cmd.take_over_port(18790)
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "flowly service stop" in out
    assert "flowly service install --start" in out


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # noqa: D102
        return True


def test_confirmed_takeover_stops_service_and_flags_restore(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: True)
    monkeypatch.setattr(gateway_cmd, "_installed_service_unit", lambda: object())
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(
        "rich.prompt.Confirm.ask", lambda *a, **k: True
    )
    monkeypatch.setattr(
        gateway_cmd, "_run_cli", lambda *args: calls.append(args) or 0
    )

    assert gateway_cmd.take_over_port(18790) is True
    assert calls == [("service", "stop")]


def test_declined_takeover_exits_zero_touching_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: True)
    monkeypatch.setattr(gateway_cmd, "_installed_service_unit", lambda: object())
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: False)
    monkeypatch.setattr(
        gateway_cmd, "_run_cli",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(typer.Exit) as exc:
        gateway_cmd.take_over_port(18790)
    assert (exc.value.exit_code or 0) == 0


def test_failed_stop_refuses_to_continue(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gateway_cmd, "_port_in_use", lambda port: True)
    monkeypatch.setattr(gateway_cmd, "_installed_service_unit", lambda: object())
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: True)
    monkeypatch.setattr(gateway_cmd, "_run_cli", lambda *args: 1)

    with pytest.raises(typer.Exit) as exc:
        gateway_cmd.take_over_port(18790)
    assert exc.value.exit_code == 1


def test_restore_reinstalls_on_the_same_port(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gateway_cmd, "_run_cli", lambda *args: calls.append(args) or 0
    )
    gateway_cmd.restore_service(18790)
    assert calls == [("service", "install", "--port", "18790", "--start")]
