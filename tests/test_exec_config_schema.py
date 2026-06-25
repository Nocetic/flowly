"""ExecToolConfig no longer carries `security`/`ask` (they moved to the
approvals store). A config.json written by an older build still has those
keys; loading it must not raise — the extra keys are ignored — and the
fields the runtime actually reads (`enabled`, `cron_mode`, timeouts) must
survive.
"""

import json

from flowly.config.loader import load_config


def test_legacy_exec_keys_are_tolerated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))

    # A config.json shaped like an older build wrote it.
    (home / "config.json").write_text(
        json.dumps(
            {
                "tools": {
                    "exec": {
                        "enabled": True,
                        "security": "allowlist",  # legacy, now ignored here
                        "ask": "always",          # legacy, now ignored here
                        "cron_mode": "approve",
                        "timeout_seconds": 123,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    # Runtime-relevant fields preserved.
    assert cfg.tools.exec.enabled is True
    assert cfg.tools.exec.cron_mode == "approve"
    assert cfg.tools.exec.timeout_seconds == 123

    # The dead fields are gone from the model — not exposed as attributes.
    assert not hasattr(cfg.tools.exec, "security")
    assert not hasattr(cfg.tools.exec, "ask")
