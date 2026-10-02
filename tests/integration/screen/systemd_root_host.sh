#!/usr/bin/env bash
# Root-VPS variant of systemd_user_host.sh. Runs inside the disposable
# Dockerfile.systemd container as root after boot. Installs this checkout for
# root like install.sh and starts `flowly service install --start` as root's
# own systemd user unit (lingering), as on a root server, on 127.0.0.1:18790.
# Screen components are not installed. A TLS terminator on :8443 forwards to
# the gateway, like a reverse proxy on a real VPS. No relay is configured.
# Usage: systemd_root_host.sh GATEWAY_TOKEN /path/to/combined.pem
set -euo pipefail
token="$1" pem="$2"
loginctl enable-linger root
for _ in $(seq 100); do [[ -S /run/user/0/bus ]] && break; sleep 0.1; done
[[ -S /run/user/0/bus ]] || { echo "root user manager did not start" >&2; exit 1; }
install -d -m 0755 /root/.local/bin
env HOME=/root FLOWLY_HOME=/root/.flowly XDG_RUNTIME_DIR=/run/user/0 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus \
  PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" TOKEN="$token" bash -euo pipefail -c '
    uv --no-config pip install --python /opt/flowly/.venv/bin/python --no-deps -e /workspace >/dev/null
    ln -sf /opt/flowly/.venv/bin/flowly "$HOME/.local/bin/flowly"
    mkdir -p "$FLOWLY_HOME"
    flowly setup byok openrouter --key sk-or-v1-disposable-screen-install >/dev/null
    /opt/flowly/.venv/bin/python - <<PY
import json, os
from pathlib import Path
path = Path(os.environ["FLOWLY_HOME"]) / "config.json"
config = json.loads(path.read_text()) if path.exists() else {}
config.setdefault("gateway", {}).update({"host": "127.0.0.1", "port": 18790, "token": os.environ["TOKEN"]})
path.write_text(json.dumps(config, indent=2)); path.chmod(0o600)
PY
    flowly service install --start
    systemctl --user show ai.flowly.gateway --property=MainPID --value
  '
# TLS terminator in front of the loopback gateway (test certificate only).
systemd-run --unit flowly-test-tls --quiet socat "openssl-listen:8443,reuseaddr,fork,cert=$pem,verify=0" TCP:127.0.0.1:18790
