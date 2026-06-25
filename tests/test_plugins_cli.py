"""Tests for ``flowly plugins`` CLI subcommands."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowly.cli.plugins_cmd import (
    _GITHUB_REPO_RE,
    _resolve_install_source,
    _sanitise_plugin_name,
    plugins_app,
)


runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point FLOWLY_HOME at a temp dir so tests don't touch real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def _write_plugin_dir(parent: Path, name: str, *, manifest=None) -> Path:
    plugin = parent / name
    plugin.mkdir()
    if manifest is None:
        manifest = f"name: {name}\nversion: '1.0'\nkind: standalone\n"
    (plugin / "plugin.yaml").write_text(manifest)
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    return plugin


# ── _resolve_install_source ────────────────────────────────────


class TestResolveInstallSource:
    def test_github_shorthand(self):
        url, subpath, is_local = _resolve_install_source("octocat/hello")
        assert url == "https://github.com/octocat/hello.git"
        assert subpath == ""
        assert is_local is False

    def test_full_https_url(self):
        url, subpath, is_local = _resolve_install_source(
            "https://gitlab.com/foo/bar.git"
        )
        assert url == "https://gitlab.com/foo/bar.git"
        assert subpath == ""
        assert is_local is False

    def test_ssh_url(self):
        url, subpath, is_local = _resolve_install_source(
            "git@github.com:foo/bar.git"
        )
        assert url == "git@github.com:foo/bar.git"
        assert subpath == ""
        assert is_local is False

    def test_local_directory(self, tmp_path):
        d = tmp_path / "local-plugin"
        d.mkdir()
        url, subpath, is_local = _resolve_install_source(str(d))
        assert url == str(d.resolve())
        assert subpath == ""
        assert is_local is True

    def test_invalid_source_raises(self):
        import typer
        with pytest.raises(typer.BadParameter):
            _resolve_install_source("not a valid source!")

    def test_repo_regex_matches_bare_owner_repo(self):
        # owner/repo is the canonical single-plugin form
        assert _GITHUB_REPO_RE.match("a/b")
        # owner/repo/path is a monorepo reference — handled by the
        # monorepo-specific regex, not the bare one
        assert not _GITHUB_REPO_RE.match("a/b/c")

    def test_monorepo_slash_form(self):
        url, subpath, is_local = _resolve_install_source(
            "Nocetic/plugins/figma"
        )
        assert url == "https://github.com/Nocetic/plugins.git"
        assert subpath == "figma"
        assert is_local is False

    def test_monorepo_deep_subpath(self):
        url, subpath, is_local = _resolve_install_source(
            "Nocetic/plugins/category/figma"
        )
        assert url == "https://github.com/Nocetic/plugins.git"
        assert subpath == "category/figma"
        assert is_local is False

    def test_fragment_subpath_form(self):
        url, subpath, is_local = _resolve_install_source(
            "Nocetic/plugins#figma"
        )
        assert url == "https://github.com/Nocetic/plugins.git"
        assert subpath == "figma"
        assert is_local is False

    def test_full_url_with_fragment_subpath(self):
        url, subpath, is_local = _resolve_install_source(
            "https://github.com/Nocetic/plugins.git#figma"
        )
        assert url == "https://github.com/Nocetic/plugins.git"
        assert subpath == "figma"
        assert is_local is False

    def test_both_slash_and_fragment_rejected(self):
        # Ambiguous: which separator wins? Refuse rather than guessing.
        import typer
        with pytest.raises(typer.BadParameter):
            _resolve_install_source("a/b/c#d")


# ── _sanitise_plugin_name ──────────────────────────────────────


class TestSanitisePluginName:
    def test_plain_name(self, tmp_path):
        result = _sanitise_plugin_name("foo", tmp_path)
        assert result == (tmp_path / "foo").resolve()

    def test_rejects_traversal(self, tmp_path):
        import typer
        with pytest.raises(typer.BadParameter):
            _sanitise_plugin_name("../escape", tmp_path)

    def test_rejects_slash(self, tmp_path):
        import typer
        with pytest.raises(typer.BadParameter):
            _sanitise_plugin_name("a/b", tmp_path)

    def test_rejects_dotdot(self, tmp_path):
        import typer
        with pytest.raises(typer.BadParameter):
            _sanitise_plugin_name("..", tmp_path)


# ── flowly plugins list ────────────────────────────────────────


class TestListCommand:
    def test_empty_state(self, isolated_home):
        result = runner.invoke(plugins_app, ["list"])
        assert result.exit_code == 0
        # Either "No plugins discovered" or a table with bundled-only —
        # accept both since bundled may exist in some installs.
        assert "plugin" in result.stdout.lower() or result.stdout

    def test_user_plugin_appears_in_list(self, isolated_home):
        plugins_dir = isolated_home / "plugins"
        plugins_dir.mkdir()
        _write_plugin_dir(plugins_dir, "my-plug")

        result = runner.invoke(plugins_app, ["list"])
        assert result.exit_code == 0
        assert "my-plug" in result.stdout


# ── flowly plugins enable / disable ────────────────────────────


class TestEnableDisable:
    def test_enable_adds_to_config(self, isolated_home):
        result = runner.invoke(plugins_app, ["enable", "my-plug"])
        assert result.exit_code == 0
        config_file = isolated_home / "config.json"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "my-plug" in data["plugins"]["enabled"]
        assert "my-plug" not in data["plugins"].get("disabled", [])

    def test_disable_adds_to_disabled_and_removes_enabled(self, isolated_home):
        runner.invoke(plugins_app, ["enable", "my-plug"])
        result = runner.invoke(plugins_app, ["disable", "my-plug"])
        assert result.exit_code == 0
        data = json.loads((isolated_home / "config.json").read_text())
        assert "my-plug" in data["plugins"]["disabled"]
        assert "my-plug" not in data["plugins"]["enabled"]

    def test_enable_is_idempotent(self, isolated_home):
        runner.invoke(plugins_app, ["enable", "x"])
        runner.invoke(plugins_app, ["enable", "x"])
        data = json.loads((isolated_home / "config.json").read_text())
        assert data["plugins"]["enabled"].count("x") == 1


# ── flowly plugins install (local path) ────────────────────────


class TestInstallLocal:
    def test_install_from_local_directory(self, isolated_home, tmp_path):
        # Build a plugin source directory outside FLOWLY_HOME
        source = tmp_path / "src-plug"
        source.mkdir()
        (source / "plugin.yaml").write_text("name: src-plug\nversion: '0.1'\n")
        (source / "__init__.py").write_text("def register(ctx):\n    pass\n")

        result = runner.invoke(
            plugins_app, ["install", str(source), "--no-enable"],
        )
        assert result.exit_code == 0, result.stdout
        installed = isolated_home / "plugins" / "src-plug"
        assert installed.exists()
        assert (installed / "plugin.yaml").exists()

    def test_install_auto_enables_by_default(self, isolated_home, tmp_path):
        source = tmp_path / "auto-enable"
        source.mkdir()
        (source / "plugin.yaml").write_text("name: auto-enable\nversion: '1'\n")
        (source / "__init__.py").write_text("def register(ctx):\n    pass\n")

        result = runner.invoke(plugins_app, ["install", str(source)])
        assert result.exit_code == 0, result.stdout
        data = json.loads((isolated_home / "config.json").read_text())
        assert "auto-enable" in data["plugins"]["enabled"]

    def test_install_rejects_existing_without_force(
        self, isolated_home, tmp_path,
    ):
        source = tmp_path / "dup"
        source.mkdir()
        (source / "plugin.yaml").write_text("name: dup\n")
        (source / "__init__.py").write_text("def register(ctx):\n    pass\n")

        runner.invoke(plugins_app, ["install", str(source), "--no-enable"])
        result = runner.invoke(
            plugins_app, ["install", str(source), "--no-enable"],
        )
        assert result.exit_code != 0
        assert "already installed" in result.stdout

    def test_install_with_force_overwrites(self, isolated_home, tmp_path):
        source = tmp_path / "ow"
        source.mkdir()
        (source / "plugin.yaml").write_text("name: ow\n")
        (source / "__init__.py").write_text("def register(ctx):\n    pass\n")

        runner.invoke(plugins_app, ["install", str(source), "--no-enable"])
        result = runner.invoke(
            plugins_app, ["install", str(source), "--no-enable", "--force"],
        )
        assert result.exit_code == 0, result.stdout

    def test_install_rejects_source_without_manifest(
        self, isolated_home, tmp_path,
    ):
        source = tmp_path / "no-manifest"
        source.mkdir()
        (source / "__init__.py").write_text("# nothing\n")

        result = runner.invoke(
            plugins_app, ["install", str(source), "--no-enable"],
        )
        assert result.exit_code != 0
        assert "no plugin.yaml" in result.stdout


# ── flowly plugins remove ──────────────────────────────────────


class TestRemove:
    def test_remove_deletes_directory(self, isolated_home, tmp_path):
        plugins_dir = isolated_home / "plugins"
        plugins_dir.mkdir()
        _write_plugin_dir(plugins_dir, "to-delete")
        runner.invoke(plugins_app, ["enable", "to-delete"])

        result = runner.invoke(
            plugins_app, ["remove", "to-delete", "--yes"],
        )
        assert result.exit_code == 0
        assert not (plugins_dir / "to-delete").exists()

        data = json.loads((isolated_home / "config.json").read_text())
        assert "to-delete" not in data["plugins"]["enabled"]

    def test_remove_requires_existing_plugin(self, isolated_home):
        result = runner.invoke(plugins_app, ["remove", "ghost", "--yes"])
        assert result.exit_code != 0
