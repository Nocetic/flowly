"""The isolated dev instance never keeps the real Mac's Flowly server entry.

``flowly server connect`` re-registers whatever ``agentHostId`` the config
names, so a dev instance that kept the copied one would re-register the real
Mac and rotate its secret (scripts/dev_install_config.py).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_install_config.py"


def _module():
    spec = importlib.util.spec_from_file_location("dev_install_config", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real() -> dict:
    return {
        "gateway": {"port": 18790},
        "channels": {
            "telegram": {"enabled": True, "token": "t"},
            "flowlyServer": {"enabled": True, "serverUrl": "wss://cloud.useflowlyapp.com/v1/agent",
                             "agentHostId": "home-real", "agentSecret": "secret-real"},
        },
    }


def test_a_copied_config_drops_the_real_server_entry_and_disables_channels():
    cfg = _module().copied(_real())
    server = cfg["channels"]["flowlyServer"]
    assert "agentHostId" not in server and "agentSecret" not in server
    assert server["enabled"] is False
    assert server["serverUrl"] == "wss://cloud.useflowlyapp.com/v1/agent"
    assert cfg["channels"]["telegram"]["enabled"] is False


def test_invariants_drop_the_real_entry_left_by_an_older_copy_but_keep_the_dev_one(tmp_path):
    module = _module()
    older_copy = module.invariants(json.loads(json.dumps(_real())), str(tmp_path), 18890, _real())
    assert "agentHostId" not in older_copy["channels"]["flowlyServer"]
    assert older_copy["gateway"]["port"] == 18890
    assert older_copy["agents"]["defaults"]["workspace"] == str(tmp_path / "workspace")

    own = _real()
    own["channels"]["flowlyServer"].update(agentHostId="home-dev", agentSecret="secret-dev")
    kept = module.invariants(own, str(tmp_path), 18890, _real())["channels"]["flowlyServer"]
    assert kept["agentHostId"] == "home-dev" and kept["agentSecret"] == "secret-dev"


def test_the_command_line_edits_the_file(tmp_path):
    dev, real = tmp_path / "config.json", tmp_path / "real.json"
    dev.write_text(json.dumps(_real()))
    real.write_text(json.dumps(_real()))
    module = _module()
    module.main(["copied", str(dev)])
    module.main(["invariants", str(dev), str(tmp_path), "18890", str(real)])
    cfg = json.loads(dev.read_text())
    assert "agentHostId" not in cfg["channels"]["flowlyServer"]
    assert cfg["gateway"]["port"] == 18890
