"""Service-definition builders must use a stable WorkingDirectory and
write FLOWLY_HOME (never capture the install-time cwd). Pure builders —
no launchctl/systemd/schtasks invoked.
"""

from __future__ import annotations

from pathlib import Path

from flowly.cli.service_cmd import _build_linux_unit, _build_mac_plist_obj


ARGV = ["/opt/flowly/flowly", "gateway", "--port", "18790"]
HOME = "/home/flowly"


def test_mac_plist_working_directory_is_home_not_cwd():
    obj = _build_mac_plist_obj(
        label="ai.flowly.gateway", argv=ARGV, flowly_home=HOME, runtime_cwd="",
    )
    assert obj["WorkingDirectory"] == str(Path.home())
    # Must NOT be the process cwd (the bug we are fixing).
    import os
    assert obj["WorkingDirectory"] != os.getcwd() or str(Path.home()) == os.getcwd()


def test_mac_plist_writes_flowly_home():
    obj = _build_mac_plist_obj(
        label="x", argv=ARGV, flowly_home=HOME, runtime_cwd="",
    )
    env = obj["EnvironmentVariables"]
    assert env["FLOWLY_HOME"] == HOME
    assert "FLOWLY_CWD" not in env


def test_mac_plist_writes_flowly_cwd_when_given():
    obj = _build_mac_plist_obj(
        label="x", argv=ARGV, flowly_home=HOME, runtime_cwd="/work/proj",
    )
    assert obj["EnvironmentVariables"]["FLOWLY_CWD"] == "/work/proj"


def test_linux_unit_working_directory_is_home():
    unit = _build_linux_unit(
        exec_line="/opt/flowly/flowly gateway --port 18790",
        flowly_home=HOME, runtime_cwd="",
    )
    assert f"WorkingDirectory={Path.home()}" in unit
    assert f"Environment=FLOWLY_HOME={HOME}" in unit
    assert "FLOWLY_CWD" not in unit


def test_linux_unit_includes_flowly_cwd_when_given():
    unit = _build_linux_unit(
        exec_line="/opt/flowly/flowly gateway --port 18790",
        flowly_home=HOME, runtime_cwd="/work/proj",
    )
    assert "Environment=FLOWLY_CWD=/work/proj" in unit
