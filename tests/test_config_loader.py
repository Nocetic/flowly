"""Tests for configuration loading and key conversion."""

import json
from pathlib import Path

import pytest

from flowly.config.loader import (
    camel_to_snake,
    convert_keys,
    convert_to_camel,
    load_config,
    save_config,
    snake_to_camel,
)
from flowly.config.schema import Config


# ── Key conversion ──────────────────────────────────────────────────


class TestCamelToSnake:
    def test_simple(self):
        assert camel_to_snake("apiKey") == "api_key"

    def test_multiple_words(self):
        assert camel_to_snake("webhookBaseUrl") == "webhook_base_url"

    def test_single_word(self):
        assert camel_to_snake("enabled") == "enabled"

    def test_already_snake(self):
        assert camel_to_snake("api_key") == "api_key"

    def test_consecutive_uppercase(self):
        assert camel_to_snake("sttProvider") == "stt_provider"

    def test_oauth_acronym_inside_camel_key(self):
        assert camel_to_snake("xaiOAuth") == "xai_oauth"

    def test_empty(self):
        assert camel_to_snake("") == ""


class TestSnakeToCamel:
    def test_simple(self):
        assert snake_to_camel("api_key") == "apiKey"

    def test_multiple_words(self):
        assert snake_to_camel("webhook_base_url") == "webhookBaseUrl"

    def test_single_word(self):
        assert snake_to_camel("enabled") == "enabled"

    def test_already_camel(self):
        # Not ideal but expected behavior
        assert snake_to_camel("apiKey") == "apiKey"

    def test_empty(self):
        assert snake_to_camel("") == ""


class TestConvertKeys:
    def test_flat_dict(self):
        data = {"apiKey": "sk-123", "maxTokens": 1024}
        result = convert_keys(data)
        assert result == {"api_key": "sk-123", "max_tokens": 1024}

    def test_nested_dict(self):
        data = {"providers": {"openRouter": {"apiKey": "key"}}}
        result = convert_keys(data)
        assert result == {"providers": {"open_router": {"api_key": "key"}}}

    def test_list_of_dicts(self):
        data = {"allowFrom": [{"userId": "123"}]}
        result = convert_keys(data)
        assert result == {"allow_from": [{"user_id": "123"}]}

    def test_non_dict(self):
        assert convert_keys("hello") == "hello"
        assert convert_keys(42) == 42
        assert convert_keys(None) is None

    def test_list_of_primitives(self):
        data = {"items": [1, 2, 3]}
        result = convert_keys(data)
        assert result == {"items": [1, 2, 3]}


class TestConvertToCamel:
    def test_flat_dict(self):
        data = {"api_key": "sk-123", "max_tokens": 1024}
        result = convert_to_camel(data)
        assert result == {"apiKey": "sk-123", "maxTokens": 1024}

    def test_nested_dict(self):
        data = {"providers": {"open_router": {"api_key": "key"}}}
        result = convert_to_camel(data)
        assert result == {"providers": {"openRouter": {"apiKey": "key"}}}

    def test_roundtrip(self):
        """camelCase → snake_case → camelCase should preserve keys."""
        original = {"apiKey": "test", "webhookBaseUrl": "http://x", "maxTokens": 100}
        snake = convert_keys(original)
        camel = convert_to_camel(snake)
        assert camel == original


# ── Config load/save ────────────────────────────────────────────────


class TestLoadConfig:
    def test_default_when_no_file(self, tmp_path: Path):
        config = load_config(tmp_path / "nonexistent.json")
        assert isinstance(config, Config)
        assert config.gateway.port == 18790

    def test_load_camel_case_json(self, tmp_path: Path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "gateway": {"port": 9999},
            "providers": {"openrouter": {"apiKey": "sk-test"}},
            "agents": {"defaults": {"maxTokens": 4096}},
        }))
        config = load_config(config_file)
        assert config.gateway.port == 9999
        assert config.providers.openrouter.api_key == "sk-test"
        assert config.agents.defaults.max_tokens == 4096

    def test_load_xai_oauth_camel_case_json(self, tmp_path: Path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "providers": {
                "xaiOAuth": {
                    "enabled": True,
                    "clientId": "flowly-client",
                    "apiBase": "https://api.x.ai/v1",
                }
            },
        }))

        config = load_config(config_file)

        assert config.providers.xai_oauth.enabled is True
        assert config.providers.xai_oauth.client_id == "flowly-client"
        assert config.providers.xai_oauth.api_base == "https://api.x.ai/v1"

    def test_invalid_json_returns_default(self, tmp_path: Path):
        config_file = tmp_path / "config.json"
        config_file.write_text("not json{{{")
        config = load_config(config_file)
        assert isinstance(config, Config)
        assert config.gateway.port == 18790

    def test_empty_file_returns_default(self, tmp_path: Path):
        config_file = tmp_path / "config.json"
        config_file.write_text("")
        config = load_config(config_file)
        assert isinstance(config, Config)


class TestSaveConfig:
    def test_save_creates_file(self, tmp_path: Path):
        config = Config()
        config_file = tmp_path / "subdir" / "config.json"
        save_config(config, config_file)
        assert config_file.exists()

    def test_save_uses_camel_case(self, tmp_path: Path):
        config = Config()
        config_file = tmp_path / "config.json"
        save_config(config, config_file)

        data = json.loads(config_file.read_text())
        # Top-level keys should be camelCase (though single-word keys are same)
        assert "gateway" in data
        # Nested keys should be camelCase
        assert "maxTokens" in data["agents"]["defaults"]
        assert "apiKey" in data["providers"]["openrouter"]

    def test_roundtrip(self, tmp_path: Path):
        """Save and reload should produce equivalent config."""
        original = Config()
        original.gateway.port = 12345
        original.providers.openrouter.api_key = "sk-roundtrip"

        config_file = tmp_path / "config.json"
        save_config(original, config_file)
        loaded = load_config(config_file)

        assert loaded.gateway.port == 12345
        assert loaded.providers.openrouter.api_key == "sk-roundtrip"


# ── Self-heal recovery ──────────────────────────────────────────────


class TestSelfHealRecovery:
    """Recovery flow: corrupt config.json should auto-restore from
    config.json.bak so the user never sees a broken gateway from a
    single bad write. Real-world threat — manual edits, agent
    plugin-creator scripts, external sync tools, etc."""

    def test_seeds_bak_on_first_healthy_load(self, tmp_path: Path):
        """Existing users predate the .bak convention. First time
        load_config sees a valid config without a .bak, it must seed
        one so the next corruption is recoverable."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "providers": {"openrouter": {"apiKey": "sk-precious"}},
        }))
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        assert not bak.exists()

        load_config(config_file)
        assert bak.exists(), "Bootstrap .bak should be written on first load"
        assert json.loads(bak.read_text())["providers"]["openrouter"]["apiKey"] == "sk-precious"

    def test_does_not_overwrite_existing_bak_on_load(self, tmp_path: Path):
        """If a .bak already exists (from a prior save_config), don't
        clobber it on every subsequent load."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-current"}}}))
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        bak.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-older-but-valid"}}}))
        bak_mtime_before = bak.stat().st_mtime

        load_config(config_file)
        # Subtle: the .bak content from before is preserved as-is.
        # We're not comparing mtime (filesystem resolution may not
        # change it within the same call), but content must be untouched.
        assert json.loads(bak.read_text())["providers"]["openrouter"]["apiKey"] == "sk-older-but-valid"
        _ = bak_mtime_before  # silence unused warning

    def test_recovers_from_bak_when_config_corrupt(self, tmp_path: Path):
        """The headline feature: corrupt config + valid bak → restore."""
        config_file = tmp_path / "config.json"
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        # Active config is broken
        config_file.write_text("{ broken json}}}")
        # Backup is the last known good state. Use a valid port (the
        # gateway schema enforces 1-65535) so this test exercises the
        # recovery path, not a separate validation failure.
        bak.write_text(json.dumps({
            "gateway": {"port": 9999},
            "providers": {"openrouter": {"apiKey": "sk-rescued"}},
        }))

        cfg = load_config(config_file)

        # Recovered values
        assert cfg.gateway.port == 9999
        assert cfg.providers.openrouter.api_key == "sk-rescued"
        # config.json now contains the recovered content
        assert json.loads(config_file.read_text())["providers"]["openrouter"]["apiKey"] == "sk-rescued"
        # Original broken file kept for forensics
        broken_files = list(tmp_path.glob("config.json.broken-*"))
        assert len(broken_files) == 1
        assert "{ broken json" in broken_files[0].read_text()

    def test_falls_through_to_defaults_when_both_corrupt(self, tmp_path: Path):
        """If both config.json AND .bak are unparseable, return defaults
        rather than crash. User loses secrets but gateway boots."""
        config_file = tmp_path / "config.json"
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        config_file.write_text("{ also broken")
        bak.write_text("not even close to json")

        cfg = load_config(config_file)
        assert isinstance(cfg, Config)
        assert cfg.gateway.port == 18790  # default

    def test_recovers_when_config_is_empty_file(self, tmp_path: Path):
        """Disk failures sometimes leave 0-byte files. Empty = invalid;
        recover from .bak just like a parse error."""
        config_file = tmp_path / "config.json"
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        config_file.write_text("")  # 0 bytes
        bak.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-rescued"}}}))

        cfg = load_config(config_file)
        assert cfg.providers.openrouter.api_key == "sk-rescued"

    def test_save_config_creates_bak_from_existing(self, tmp_path: Path):
        """save_config rotates: existing valid file → .bak before write."""
        config_file = tmp_path / "config.json"
        # Manually plant a known valid file
        config_file.write_text(json.dumps({
            "providers": {"openrouter": {"apiKey": "sk-original"}},
        }))
        bak = config_file.with_suffix(config_file.suffix + ".bak")
        assert not bak.exists()

        # Save a new config; existing should land in .bak
        new_cfg = Config()
        new_cfg.providers.openrouter.api_key = "sk-new"
        save_config(new_cfg, config_file)

        assert bak.exists()
        bak_data = json.loads(bak.read_text())
        assert bak_data["providers"]["openrouter"]["apiKey"] == "sk-original"

    def test_save_config_does_not_back_up_corrupt_source(self, tmp_path: Path):
        """If the on-disk config is already corrupt, save_config must NOT
        copy it to .bak — that would propagate corruption into the
        recovery slot and make .bak useless. The prior valid .bak (if
        any) must be preserved as the safety net."""
        config_file = tmp_path / "config.json"
        bak = config_file.with_suffix(config_file.suffix + ".bak")

        # Plant a valid .bak first
        bak.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-known-good"}}}))
        # Active config is corrupt
        config_file.write_text("{not json")

        new_cfg = Config()
        new_cfg.providers.openrouter.api_key = "sk-new"
        save_config(new_cfg, config_file)

        # .bak should still hold the known-good state, NOT the corrupt one
        assert json.loads(bak.read_text())["providers"]["openrouter"]["apiKey"] == "sk-known-good"

    def test_recovery_round_trips_through_save(self, tmp_path: Path):
        """End-to-end: corrupt → recover → save_config produces a valid
        config.json with the recovered content + a fresh .bak. Subsequent
        load_config succeeds without any recovery dance."""
        config_file = tmp_path / "config.json"
        bak = config_file.with_suffix(config_file.suffix + ".bak")

        bak.write_text(json.dumps({
            "providers": {"openrouter": {"apiKey": "sk-rescued"}},
        }))
        config_file.write_text("{ corrupt }")

        cfg = load_config(config_file)
        assert cfg.providers.openrouter.api_key == "sk-rescued"

        # Save back, simulating any subsequent CLI command writing config
        save_config(cfg, config_file)

        # Active file is now valid
        loaded_again = load_config(config_file)
        assert loaded_again.providers.openrouter.api_key == "sk-rescued"

        # No new "broken" file from this second load (it was healthy)
        broken_files = list(tmp_path.glob("config.json.broken-*"))
        assert len(broken_files) == 1, (
            "Only one broken-file should exist (from the original corruption)"
        )
