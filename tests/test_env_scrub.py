"""Boundary tests for subprocess env scrubbing.

SECURITY.md §2.3: provider API keys, channel bot tokens, and gateway
auth are stripped from subprocess environments. User-owned credentials
(AWS, GitHub, npm, etc) flow through so legitimate workflows the
operator approved actually work.

This test file is regression cover for the boundary, not a generic
unit-test sweep. The shape of each assertion mirrors a specific
attack vector documented in SECURITY.md.
"""

from __future__ import annotations

import pytest

from flowly.exec.env_scrub import (
    force_prefix,
    is_flowly_credential,
    list_blocklist,
    sanitize_subprocess_env,
)
from flowly.exec.env_passthrough import (
    clear_env_passthrough,
    is_env_passthrough,
    register_env_passthrough,
)


# ── §2.3 — Flowly-managed credentials stripped ────────────────────────


class TestBlocklistStrips:
    """Every name in the blocklist disappears from sanitized env."""

    @pytest.mark.parametrize(
        "key",
        [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "ZHIPU_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "FLOWLY_JWT_SECRET",
            "FLOWLY_AUTH_TOKEN",
            "TRELLO_API_KEY",
            "LINEAR_API_KEY",
            "BRAVE_API_KEY",
            "HASS_TOKEN",
            "TWILIO_AUTH_TOKEN",
        ],
    )
    def test_blocklisted_key_stripped(self, key):
        env = {key: "secret-value", "PATH": "/usr/bin"}
        result = sanitize_subprocess_env(env)
        assert key not in result, f"{key} should be stripped"

    def test_blocklist_is_not_empty(self):
        # Defence in depth — a future "let's just clear this for tests"
        # PR would hit this assertion.
        bl = list_blocklist()
        assert len(bl) >= 10, "blocklist suspiciously small"
        assert "OPENAI_API_KEY" in bl
        assert "ANTHROPIC_API_KEY" in bl


# ── §2.3 — User-owned credentials flow through ────────────────────────


class TestUserOwnedPreserved:
    """Credentials Flowly does not manage must not be stripped.

    The deny-list approach (vs. regex on `.*_KEY$`) is the whole
    reason `aws s3 ls`, `gh pr create`, `npm publish`, etc. still
    work under the sandbox. If any of these start being stripped,
    the operator's day-to-day workflow breaks.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "NPM_TOKEN",
            "PYPI_TOKEN",
            "NOTION_TOKEN",
            "FIGMA_TOKEN",
            "VERCEL_TOKEN",
        ],
    )
    def test_user_owned_passes_through(self, key):
        env = {key: "user-owned-secret", "PATH": "/usr/bin"}
        result = sanitize_subprocess_env(env)
        assert result.get(key) == "user-owned-secret", f"{key} must pass through"

    def test_essential_runtime_vars_preserved(self):
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/x",
            "USER": "x",
            "SHELL": "/bin/zsh",
            "LANG": "en_US.UTF-8",
            "TMPDIR": "/tmp",
        }
        result = sanitize_subprocess_env(env)
        for k in env:
            assert result.get(k) == env[k]


# ── Force-prefix escape hatch ─────────────────────────────────────────


class TestForcePrefix:
    """``__FLOWLY_FORCE__OPENAI_API_KEY`` in extras forces a value
    through despite the blocklist. Reserved for internal code paths
    that legitimately need to forward a credential to a wrapped
    subprocess — not exposed to plugins.
    """

    def test_force_prefix_bypasses_blocklist(self):
        base = {"OPENAI_API_KEY": "from-env"}  # stripped
        extras = {f"{force_prefix()}OPENAI_API_KEY": "forced-value"}
        result = sanitize_subprocess_env(base, extras)
        assert result.get("OPENAI_API_KEY") == "forced-value"

    def test_force_prefix_does_not_appear_in_output(self):
        # The prefix is a marker, not a real env var name.
        extras = {f"{force_prefix()}MY_VAR": "v"}
        result = sanitize_subprocess_env({}, extras)
        for key in result:
            assert not key.startswith(
                force_prefix()
            ), "marker prefix leaked into child env"

    def test_force_prefix_in_base_env_is_dropped(self):
        # The prefix is only valid when set by the caller in extras.
        # If a parent process somehow has ``__FLOWLY_FORCE__OPENAI_API_KEY``
        # set, that's a misuse — drop it rather than respect it.
        base = {f"{force_prefix()}OPENAI_API_KEY": "leaked"}
        result = sanitize_subprocess_env(base)
        assert "OPENAI_API_KEY" not in result
        assert f"{force_prefix()}OPENAI_API_KEY" not in result


# ── GHSA-rhgp-j443-p4rf — passthrough cannot register provider creds ─


class TestGHSAGuard:
    """GHSA-rhgp-j443-p4rf: a malicious skill manifest declared
    OPENAI_API_KEY in required_environment_variables and received the
    credential in a sandboxed child process. Flowly's
    register_env_passthrough refuses to register names that appear
    in the Flowly-managed blocklist.
    """

    def setup_method(self):
        clear_env_passthrough()

    def test_provider_credential_register_refused(self):
        register_env_passthrough(["OPENAI_API_KEY"])
        assert not is_env_passthrough(
            "OPENAI_API_KEY"
        ), "GHSA guard failed — provider cred passed through"

    @pytest.mark.parametrize(
        "key",
        [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "FLOWLY_JWT_SECRET",
            "SLACK_BOT_TOKEN",
        ],
    )
    def test_each_blocklist_entry_unregisterable(self, key):
        register_env_passthrough([key])
        assert not is_env_passthrough(key)

    def test_legitimate_third_party_key_registers(self):
        register_env_passthrough(["MY_NOTION_TOKEN"])
        assert is_env_passthrough("MY_NOTION_TOKEN")

    def test_registered_passthrough_survives_scrub(self):
        # A legitimate third-party token registered by a skill
        # should reach the subprocess. This is the *positive* side
        # of the GHSA guard — passthrough still works for things
        # that aren't Flowly's secrets.
        register_env_passthrough(["MY_NOTION_TOKEN"])
        env = {"MY_NOTION_TOKEN": "notion-key", "OPENAI_API_KEY": "openai-key"}
        result = sanitize_subprocess_env(env)
        assert result.get("MY_NOTION_TOKEN") == "notion-key"
        assert "OPENAI_API_KEY" not in result

    def test_clear_drops_skill_allowlist(self):
        register_env_passthrough(["MY_NOTION_TOKEN"])
        clear_env_passthrough()
        assert not is_env_passthrough("MY_NOTION_TOKEN")


# ── Helper introspection ──────────────────────────────────────────────


class TestIntrospection:
    def test_is_flowly_credential_matches_blocklist(self):
        for name in list_blocklist():
            assert is_flowly_credential(name)
        assert not is_flowly_credential("AWS_ACCESS_KEY_ID")
        assert not is_flowly_credential("PATH")
