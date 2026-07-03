"""Tests for configuration schema validation."""

from pathlib import Path

import pytest

from flowly.config.schema import (
    AgentDefaults,
    ChannelsConfig,
    Config,
    DiscordConfig,
    ExecToolConfig,
    GatewayConfig,
    ProviderConfig,
    ProvidersConfig,
    SlackConfig,
    TelegramConfig,
    VoiceBridgeConfig,
)


class TestConfig:
    def test_defaults(self):
        config = Config()
        assert config.gateway.port == 18790
        assert config.agents.defaults.model == "moonshotai/kimi-k2.5"
        assert config.agents.defaults.temperature == 0.7
        assert config.agents.defaults.max_tokens == 8192

    def test_workspace_path_expansion(self):
        config = Config()
        path = config.workspace_path
        assert isinstance(path, Path)
        assert "~" not in str(path)

    def test_get_api_key_priority(self):
        """OpenRouter > Anthropic > OpenAI > xAI > Gemini > Zhipu > vLLM."""
        config = Config()
        assert config.get_api_key() is None

        config.providers.openai.api_key = "openai-key"
        assert config.get_api_key() == "openai-key"

        config.providers.anthropic.api_key = "anthropic-key"
        assert config.get_api_key() == "anthropic-key"

        config.providers.openrouter.api_key = "openrouter-key"
        assert config.get_api_key() == "openrouter-key"

    def test_get_api_base_openrouter(self):
        config = Config()
        config.providers.openrouter.api_key = "key"
        assert config.get_api_base() == "https://openrouter.ai/api/v1"

    def test_get_api_base_xai(self):
        config = Config()
        config.providers.xai.api_key = "key"
        assert config.get_api_base() == "https://api.x.ai/v1"

    def test_get_api_base_none(self):
        config = Config()
        assert config.get_api_base() is None

    def test_get_api_base_custom(self):
        config = Config()
        config.providers.openrouter.api_key = "key"
        config.providers.openrouter.api_base = "https://custom.api/v1"
        assert config.get_api_base() == "https://custom.api/v1"


class TestAgentDefaults:
    def test_defaults(self):
        defaults = AgentDefaults()
        assert defaults.persona == "default"
        assert defaults.max_tool_iterations == 100
        assert defaults.context_messages == 100
        assert defaults.action_temperature == 0.1

    def test_compaction_defaults(self):
        defaults = AgentDefaults()
        assert defaults.compaction.mode == "safeguard"
        assert defaults.compaction.context_window == 128000
        assert defaults.compaction.reserve_tokens_floor == 20000


class TestChannelsConfig:
    def test_all_disabled_by_default(self):
        channels = ChannelsConfig()
        assert channels.telegram.enabled is False
        assert channels.discord.enabled is False
        assert channels.slack.enabled is False
        assert channels.whatsapp.enabled is False

    def test_telegram_dm_policy(self):
        tg = TelegramConfig()
        assert tg.dm_policy == "pairing"

    def test_discord_intents(self):
        dc = DiscordConfig()
        assert dc.intents == 37377

    def test_slack_defaults(self):
        slack = SlackConfig()
        assert slack.mode == "socket"
        assert slack.group_policy == "mention"
        assert slack.dm.enabled is True
        assert slack.dm.policy == "open"


class TestExecToolConfig:
    def test_defaults(self):
        # exec is enabled out of the box; the security *policy* (security
        # mode / ask mode / allowlist) lives in the approvals store, NOT on
        # this model — see ExecToolConfig docstring. Only enabled, timeouts
        # and cron_mode are config.json-owned.
        exec_cfg = ExecToolConfig()
        assert exec_cfg.enabled is True
        assert exec_cfg.timeout_seconds == 300
        assert exec_cfg.cron_mode == "deny"
        # Policy fields intentionally removed from config.json.
        assert not hasattr(exec_cfg, "security")
        assert not hasattr(exec_cfg, "ask")

    def test_custom_values(self):
        exec_cfg = ExecToolConfig(
            enabled=True,
            timeout_seconds=60,
            cron_mode="approve",
        )
        assert exec_cfg.enabled is True
        assert exec_cfg.timeout_seconds == 60
        assert exec_cfg.cron_mode == "approve"

    def test_legacy_policy_keys_ignored(self):
        # A config dict from an older build carries security/ask; loading
        # must silently ignore them, not raise.
        exec_cfg = ExecToolConfig(enabled=True, security="allowlist", ask="always")
        assert exec_cfg.enabled is True
        assert not hasattr(exec_cfg, "security")


class TestVoiceBridgeConfig:
    def test_defaults(self):
        voice = VoiceBridgeConfig()
        assert voice.enabled is False
        assert voice.stt_provider == "groq"
        assert voice.tts_provider == "elevenlabs"
        assert voice.language == "en-US"

    def test_custom_stt_tts(self):
        voice = VoiceBridgeConfig(
            stt_provider="openai",
            tts_provider="deepgram",
        )
        assert voice.stt_provider == "openai"
        assert voice.tts_provider == "deepgram"


class TestProviderConfig:
    def test_empty_by_default(self):
        p = ProviderConfig()
        assert p.api_key == ""
        assert p.api_base is None

    def test_all_providers_exist(self):
        providers = ProvidersConfig()
        for name in ("anthropic", "openai", "openrouter", "zhipu", "vllm", "gemini", "groq", "xai"):
            assert hasattr(providers, name)
            provider = getattr(providers, name)
            assert isinstance(provider, ProviderConfig)
        assert hasattr(providers, "zai_coding")
        assert providers.zai_coding.enabled is True
        assert providers.zai_coding.api_base == "https://api.z.ai/api/coding/paas/v4"
