"""A root host's agents each get a cgroup, and a screen admits only its own agent's."""

import os

import pytest

import flowly.profile as profiles
from flowly.desktop_runtime import agent_scope, devtools_guard
from flowly.desktop_runtime.agent_scope import (
    admitted_agent_cgroup,
    agent_scope_command,
    own_cgroup,
    record_agent_cgroup,
    valid_cgroup,
)
from flowly.desktop_runtime.authority import ScreenError

SERVICE = "user.slice/user-0.slice/user@0.service/app.slice/ai.flowly.gateway.service"


@pytest.mark.parametrize("path, valid", [
    (SERVICE, True),
    ("system.slice/flowly-agent-1a2b3c4d5e6f-0011aabb.scope", True),
    ("init.scope", True),
    ("", False),
    ("/system.slice/x.scope", False),
    ("system.slice/../x", False),
    ('system.slice/x" accept', False),
    ("system.slice/x\\x2dy.scope", False),
    ("a/" * 40 + "b", False),
])
def test_only_plain_cgroup_paths_reach_a_rule(path, valid):
    assert valid_cgroup(path) is valid


def test_reads_the_unified_hierarchy_path(tmp_path):
    (tmp_path / "cgroup").write_text("0::/" + SERVICE + "\n")
    assert own_cgroup(tmp_path / "cgroup") == SERVICE
    (tmp_path / "cgroup").write_text("12:memory:/x\n1:name=systemd:/y\n")
    assert own_cgroup(tmp_path / "cgroup") is None


@pytest.fixture
def root_systemd(monkeypatch):
    monkeypatch.setattr(agent_scope, "_root_systemd", lambda: True)
    monkeypatch.setattr(agent_scope.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/systemd-run")
    monkeypatch.setattr(agent_scope, "_scopes_work", lambda *_args: True)
    return monkeypatch


def test_a_named_agent_starts_in_a_scope_of_its_own_bound_to_the_service(root_systemd):
    root_systemd.setattr(agent_scope, "own_cgroup", lambda: SERVICE)
    command = agent_scope_command(["flowly", "--profile", "writer", "serve"], "writer")
    separator = command.index("--")
    assert command[separator + 1:] == ["flowly", "--profile", "writer", "serve"]
    options = command[:separator]
    assert options[:2] == ["/usr/bin/systemd-run", "--user"]
    assert {"--scope", "--quiet", "--collect", "--property=PartOf=ai.flowly.gateway.service"} <= set(options)
    unit = next(option for option in options if option.startswith("--unit="))[7:]
    assert unit.startswith("flowly-agent-") and unit.endswith(".scope")
    # A restarted agent gets a new scope, never a name another process holds.
    second = agent_scope_command(["flowly"], "writer")
    assert next(option for option in second if option.startswith("--unit=")) != "--unit=" + unit


def test_a_system_service_or_a_terminal_session_uses_the_system_manager(root_systemd):
    root_systemd.setattr(agent_scope, "own_cgroup", lambda: "system.slice/flowly.service")
    command = agent_scope_command(["flowly"], "writer")
    assert "--user" not in command and "--property=PartOf=flowly.service" in command
    root_systemd.setattr(agent_scope, "own_cgroup", lambda: "user.slice/user-0.slice/session-3.scope")
    command = agent_scope_command(["flowly"], "writer")
    assert "--user" not in command and not any(option.startswith("--property=PartOf") for option in command)


def test_without_root_systemd_or_working_scopes_the_command_is_unchanged(root_systemd):
    root_systemd.setattr(agent_scope, "own_cgroup", lambda: SERVICE)
    root_systemd.setattr(agent_scope, "_scopes_work", lambda *_args: False)
    assert agent_scope_command(["flowly"], "writer") == ["flowly"]
    root_systemd.setattr(agent_scope, "_root_systemd", lambda: False)
    assert agent_scope_command(["flowly"], "writer") == ["flowly"]


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", tmp_path / ".flowly")
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", tmp_path / ".flowly" / "profiles")
    (tmp_path / ".flowly" / "profiles" / "writer").mkdir(parents=True)
    return tmp_path / ".flowly"


def recorded(home, path):
    (home / "agent-cgroup").write_text(path + "\n")


def test_a_screen_admits_its_own_kind_of_agent_only(homes):
    writer = homes / "profiles" / "writer"
    scope = "user.slice/user-0.slice/user@0.service/app.slice/flowly-agent-1a2b3c4d5e6f-0011aabb.scope"
    recorded(writer, scope)
    assert admitted_agent_cgroup(writer) == scope
    # A named agent still in the service's cgroup (started before agent scopes)
    # shares it with the main agent: its screen admits nobody.
    recorded(writer, SERVICE)
    assert admitted_agent_cgroup(writer) is None
    recorded(homes, SERVICE)
    assert admitted_agent_cgroup(homes) == SERVICE
    # The main agent is never a named agent's scope.
    recorded(homes, scope)
    assert admitted_agent_cgroup(homes) is None


def test_a_linked_or_malformed_record_admits_nobody(homes, tmp_path):
    writer = homes / "profiles" / "writer"
    (tmp_path / "elsewhere").write_text(SERVICE)
    (writer / "agent-cgroup").symlink_to(tmp_path / "elsewhere")
    assert admitted_agent_cgroup(writer) is None
    (writer / "agent-cgroup").unlink()
    recorded(writer, 'flowly-agent-x.scope" accept')
    assert admitted_agent_cgroup(writer) is None
    assert admitted_agent_cgroup(homes / "profiles" / "missing") is None


def test_a_root_gateway_records_its_cgroup_privately(homes, monkeypatch):
    monkeypatch.setattr(agent_scope.sys, "platform", "linux")
    monkeypatch.setattr(agent_scope.os, "geteuid", lambda: 0)
    monkeypatch.setattr(agent_scope, "own_cgroup", lambda: SERVICE)
    record_agent_cgroup(homes)
    assert (homes / "agent-cgroup").read_text() == SERVICE + "\n"
    assert (homes / "agent-cgroup").stat().st_mode & 0o777 == 0o600
    assert [entry.name for entry in homes.iterdir() if entry.name.startswith("agent-cgroup")] == ["agent-cgroup"]
    monkeypatch.setattr(agent_scope, "own_cgroup", lambda: None)
    record_agent_cgroup(homes)
    assert not (homes / "agent-cgroup").exists()
    monkeypatch.setattr(agent_scope.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(agent_scope, "own_cgroup", lambda: SERVICE)
    record_agent_cgroup(homes)
    assert not (homes / "agent-cgroup").exists()


@pytest.fixture
def scripts(monkeypatch):
    loaded = []
    monkeypatch.setattr(devtools_guard, "_nft", lambda script: loaded.append(script) or True)
    return loaded


def test_the_rule_admits_root_sockets_of_listed_cgroups_and_resets_the_rest(scripts):
    supervisor = "system.slice/flowly-screen-abc-0123.scope"
    devtools_guard.guard("layout1", 41234, [supervisor, SERVICE, supervisor])
    rules = [line.strip() for line in scripts[0].splitlines() if "dport" in line]
    assert rules == [
        f'oif "lo" tcp dport 41234 meta skuid 0 socket cgroupv2 level 2 "{supervisor}" accept',
        f'oif "lo" tcp dport 41234 meta skuid 0 socket cgroupv2 level 5 "{SERVICE}" accept',
        'oif "lo" tcp dport 41234 reject with tcp reset',
    ]
    devtools_guard.guard("layout1", 41234)
    assert [line.strip() for line in scripts[1].splitlines() if "dport" in line] == [
        'oif "lo" tcp dport 41234 meta skuid != 0 reject with tcp reset']


@pytest.mark.parametrize("admitted", [[], ['x.scope" accept'], ["/system.slice/x.scope"]])
def test_a_rule_with_nothing_or_an_unsafe_cgroup_is_never_loaded(scripts, admitted):
    with pytest.raises(ScreenError):
        devtools_guard.guard("layout1", 41234, admitted)
    assert scripts == []


@pytest.mark.skipif(os.name != "posix", reason="Linux hosts only")
def test_a_named_runtime_is_launched_through_its_agent_scope(monkeypatch, tmp_path):
    import asyncio

    from flowly.profile_host import ProfileHost

    calls, launched = [], []
    monkeypatch.setattr("flowly.desktop_runtime.agent_scope.agent_scope_command",
                        lambda command, name: calls.append(name) or ["scoped", *command])

    async def refuse(*command, **_kwargs):
        launched.append(command)
        raise OSError("not launched in this test")

    monkeypatch.setattr("flowly.profile_host.asyncio.create_subprocess_exec", refuse)
    host = ProfileHost.__new__(ProfileHost)
    host._emit = lambda *_args, **_kwargs: asyncio.sleep(0)
    host._manager_instance = "instance"
    with pytest.raises(OSError):
        asyncio.run(host._start_runtime("writer"))
    assert calls == ["writer"]
    assert launched[0][0] == "scoped" and launched[0][-5:] == ("--profile", "writer", "serve", "--port", "0")
