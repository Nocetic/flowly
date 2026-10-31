"""The isolated dev instance's config.json edits (scripts/dev-install.sh).

    python dev_install_config.py copied <dev-config>
    python dev_install_config.py invariants <dev-config> <dev-home> <port> <real-config>

``copied`` runs once, right after the real config is copied: every channel
starts disabled, and the Flowly server entry is dropped, because it is the
real Mac's (``flowly server connect`` re-registers whatever entry the config
names, which would rotate the real Mac's secret).

``invariants`` runs on every run: the dev port and workspace, and the real
Mac's server entry dropped again if it is still there (a copy made before
this file existed).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_ENTRY = ("agentHostId", "agentSecret")


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _write(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n")


def _server_channel(cfg: dict) -> dict | None:
    channel = (cfg.get("channels") or {}).get("flowlyServer")
    return channel if isinstance(channel, dict) else None


def _drop_server_entry(channel: dict) -> None:
    channel["enabled"] = False
    for key in SERVER_ENTRY:
        channel.pop(key, None)


def copied(cfg: dict) -> dict:
    for channel in (cfg.get("channels") or {}).values():
        if isinstance(channel, dict) and channel.get("enabled"):
            channel["enabled"] = False
    server = _server_channel(cfg)
    if server is not None:
        _drop_server_entry(server)
    return cfg


def invariants(cfg: dict, dev_home: str, port: int, real: dict | None) -> dict:
    cfg.setdefault("gateway", {})["port"] = port
    cfg.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(Path(dev_home) / "workspace")
    server, real_server = _server_channel(cfg), _server_channel(real or {})
    real_host = (real_server or {}).get("agentHostId")
    if server is not None and real_host and server.get("agentHostId") == real_host:
        _drop_server_entry(server)
    return cfg


def main(argv: list[str]) -> None:
    command, dev_config = argv[0], Path(argv[1])
    cfg = _read(dev_config)
    if command == "copied":
        _write(dev_config, copied(cfg))
    elif command == "invariants":
        real_path = Path(argv[4])
        real = _read(real_path) if real_path.is_file() else None
        _write(dev_config, invariants(cfg, argv[2], int(argv[3]), real))
    else:
        raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
