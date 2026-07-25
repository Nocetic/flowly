"""Bare ``flowly`` starts its own gateway instead of assigning homework.

A configured user who typed ``flowly`` used to get three lines telling them
to run ``flowly service install --start`` and try again. The product knows
how to do that, so it does it.

The guards matter as much as the behaviour: this must never fire inside
Flowly Desktop (which spawns the binary as a non-TTY subprocess and owns the
gateway lifecycle itself), never install a unit for a port the user only
asked about on the command line, and never try to "start" a gateway that
lives on another machine.
"""

from __future__ import annotations

import pytest

from flowly.integrations import gateway_autostart as autostart


@pytest.fixture
def never_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record subprocess launches; fail loudly if one escapes a guard."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    monkeypatch.setattr(autostart, "_flowly_argv", lambda: ["flowly"])
    return calls


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("FLOWLY_NO_GATEWAY_AUTOSTART", raising=False)


def _reachable(*ports: int):
    return lambda host, port, timeout=0.5: port in ports


def test_running_gateway_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    monkeypatch.setattr(autostart, "_port_open", _reachable(18790))

    result = autostart.ensure_gateway_running("127.0.0.1", 18790)

    assert result.running is True
    assert result.attempted is False
    assert never_runs == []


def test_installs_and_starts_when_no_unit_exists(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    monkeypatch.setattr(autostart, "_unit_installed", lambda: False)
    monkeypatch.setattr(autostart, "_configured_port", lambda: 18790)
    ports: set[int] = set()
    monkeypatch.setattr(autostart, "_port_open", lambda h, p, timeout=0.5: p in ports)

    def fake_run(argv, **_kwargs):
        never_runs.append(list(argv))
        ports.add(18790)  # the service came up

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)

    result = autostart.ensure_gateway_running("127.0.0.1", 18790)

    assert result.running is True
    assert result.installed_unit is True
    assert never_runs == [["flowly", "service", "install", "--start"]]


def test_existing_unit_is_only_started_not_reinstalled(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    monkeypatch.setattr(autostart, "_unit_installed", lambda: True)
    monkeypatch.setattr(autostart, "_configured_port", lambda: 18790)
    ports: set[int] = set()
    monkeypatch.setattr(autostart, "_port_open", lambda h, p, timeout=0.5: p in ports)

    def fake_run(argv, **_kwargs):
        never_runs.append(list(argv))
        ports.add(18790)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)

    result = autostart.ensure_gateway_running("127.0.0.1", 18790)

    assert result.running is True
    assert result.installed_unit is False
    assert never_runs == [["flowly", "service", "start"]]


def test_non_tty_never_autostarts(
    monkeypatch: pytest.MonkeyPatch, never_runs
) -> None:
    """Flowly Desktop spawns the binary as a non-TTY subprocess and manages
    the gateway itself — autostart must be invisible there."""
    monkeypatch.delenv("FLOWLY_NO_GATEWAY_AUTOSTART", raising=False)
    monkeypatch.setattr(autostart.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(autostart, "_port_open", _reachable())

    result = autostart.ensure_gateway_running("127.0.0.1", 18790)

    assert result.running is False
    assert result.attempted is False
    assert never_runs == []


def test_opt_out_env_var_is_honoured(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    monkeypatch.setenv("FLOWLY_NO_GATEWAY_AUTOSTART", "1")
    monkeypatch.setattr(autostart, "_port_open", _reachable())

    result = autostart.ensure_gateway_running("127.0.0.1", 18790)

    assert result.attempted is False
    assert never_runs == []


def test_remote_gateway_is_never_started_locally(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    """``flowly --host 192.168.1.20`` targets another machine's gateway."""
    monkeypatch.setattr(autostart, "_port_open", _reachable())

    result = autostart.ensure_gateway_running("192.168.1.20", 18790)

    assert result.attempted is False
    assert never_runs == []


def test_a_one_off_port_never_installs_a_unit(
    monkeypatch: pytest.MonkeyPatch, never_runs, interactive
) -> None:
    """``flowly --port 9999`` must not bake 9999 into the machine's service."""
    monkeypatch.setattr(autostart, "_unit_installed", lambda: False)
    monkeypatch.setattr(autostart, "_configured_port", lambda: 18790)
    monkeypatch.setattr(autostart, "_port_open", _reachable())

    result = autostart.ensure_gateway_running("127.0.0.1", 9999)

    assert result.attempted is False
    assert result.installed_unit is False
    assert never_runs == []


def test_failure_reports_why_without_raising(
    monkeypatch: pytest.MonkeyPatch, interactive
) -> None:
    monkeypatch.setattr(autostart, "_unit_installed", lambda: True)
    monkeypatch.setattr(autostart, "_configured_port", lambda: 18790)
    monkeypatch.setattr(autostart, "_port_open", _reachable())
    monkeypatch.setattr(autostart, "_flowly_argv", lambda: ["flowly"])

    def failing_run(argv, **_kwargs):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = "launchctl: Operation not permitted"

        return _Result()

    monkeypatch.setattr(autostart.subprocess, "run", failing_run)

    result = autostart.ensure_gateway_running("127.0.0.1", 18790, timeout=0.1)

    assert result.running is False
    assert result.attempted is True
    assert "Operation not permitted" in result.detail


class _FakeTUI:
    launched: list[str] = []

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def run(self):
        _FakeTUI.launched.append(self._kwargs.get("session_key", ""))


@pytest.fixture
def tui_without_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """``flowly`` on a configured install whose gateway is down."""
    import flowly.tui.app as tui_app
    import flowly.tui.entry as entry
    from flowly.integrations.active_provider import ProviderReadiness

    home = tmp_path / "flowly-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setattr(entry, "_gateway_reachable", lambda *a, **k: False)
    monkeypatch.setattr(
        entry,
        "provider_readiness",
        lambda: ProviderReadiness(ready=True, provider=None, has_account=False),
    )
    monkeypatch.setattr(tui_app, "FlowlyTUI", _FakeTUI)
    _FakeTUI.launched = []
    return entry


def test_bare_flowly_starts_the_gateway_and_opens_the_ui(
    tui_without_gateway, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    entry = tui_without_gateway
    monkeypatch.setattr(
        entry,
        "ensure_gateway_running",
        lambda host, port: autostart.AutostartResult(
            running=True, attempted=True, installed_unit=True
        ),
    )

    entry.run_tui(host="127.0.0.1", port=18790)

    assert len(_FakeTUI.launched) == 1
    out = capsys.readouterr().out
    # Installing a background service changes their machine — say so once.
    assert "background" in out.lower()


def test_silent_when_an_existing_service_just_needed_starting(
    tui_without_gateway, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    entry = tui_without_gateway
    monkeypatch.setattr(
        entry,
        "ensure_gateway_running",
        lambda host, port: autostart.AutostartResult(
            running=True, attempted=True, installed_unit=False
        ),
    )

    entry.run_tui(host="127.0.0.1", port=18790)

    assert len(_FakeTUI.launched) == 1
    assert capsys.readouterr().out.strip() == ""


def test_manual_instructions_survive_when_autostart_fails(
    tui_without_gateway, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import typer

    entry = tui_without_gateway
    monkeypatch.setattr(
        entry,
        "ensure_gateway_running",
        lambda host, port: autostart.AutostartResult(
            running=False, attempted=True, detail="launchctl said no"
        ),
    )

    with pytest.raises(typer.Exit) as exc:
        entry.run_tui(host="127.0.0.1", port=18790)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "launchctl said no" in out
    assert "flowly gateway" in out
    assert _FakeTUI.launched == []


def test_remote_host_gets_remote_advice_not_a_local_service_command(
    tui_without_gateway, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``flowly --host 192.168.1.20`` dials another machine's gateway.

    "Run flowly service install --start" would install a service on THIS
    machine, which is not the one that isn't answering.
    """
    import typer

    entry = tui_without_gateway
    monkeypatch.setattr(
        entry,
        "ensure_gateway_running",
        lambda host, port: autostart.AutostartResult(
            running=False, detail="192.168.1.20 is not this machine"
        ),
    )

    with pytest.raises(typer.Exit):
        entry.run_tui(host="192.168.1.20", port=18790)

    out = capsys.readouterr().out
    assert "service install" not in out
    assert "192.168.1.20" in out


def test_a_broken_launcher_does_not_crash_the_cli(
    monkeypatch: pytest.MonkeyPatch, interactive
) -> None:
    monkeypatch.setattr(autostart, "_unit_installed", lambda: True)
    monkeypatch.setattr(autostart, "_configured_port", lambda: 18790)
    monkeypatch.setattr(autostart, "_port_open", _reachable())
    monkeypatch.setattr(autostart, "_flowly_argv", lambda: ["flowly"])

    def exploding_run(argv, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(autostart.subprocess, "run", exploding_run)

    result = autostart.ensure_gateway_running("127.0.0.1", 18790, timeout=0.1)

    assert result.running is False
    assert "no such file" in result.detail
