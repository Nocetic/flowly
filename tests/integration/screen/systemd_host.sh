#!/usr/bin/env bash
# Runs inside the disposable Dockerfile.systemd container as root, after
# systemd has booted. Installs this checkout like install.sh (editable into
# the agent venv, launcher in ~/.local/bin) and starts the real
# `flowly service install --start` systemd user unit connected to the relay.
# Usage: systemd_host.sh RELAY_URL SERVER_ID SERVER_AUTH_TOKEN JWT_SECRET
set -euo pipefail
relay_url="$1" server_id="$2" token="$3" secret="$4"
export HOME=/root FLOWLY_HOME=/root/.flowly
uv=/root/.local/bin/uv
# Editable install from the read-only checkout, dependencies already locked.
"$uv" --no-config pip install --python /opt/flowly/.venv/bin/python --no-deps -e /workspace >/dev/null
ln -sf /opt/flowly/.venv/bin/flowly /root/.local/bin/flowly
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mkdir -p "$FLOWLY_HOME"
# Disposable provider key: the installation test never calls a model.
flowly setup byok openrouter --key sk-or-v1-disposable-screen-install >/dev/null
/opt/flowly/.venv/bin/python - "$relay_url" "$server_id" "$token" "$secret" <<'PY'
import json, sys
from pathlib import Path
relay_url, server_id, token, secret = sys.argv[1:]
path = Path("/root/.flowly/config.json")
config = json.loads(path.read_text()) if path.exists() else {}
# No gateway token: a relay host must generate its own at start (loopback
# stays exempt for local clients; display routes always demand the token).
config.setdefault("gateway", {}).update({"host": "127.0.0.1", "port": 18790})
config.setdefault("channels", {})["web"] = {"enabled": True, "relayUrl": relay_url, "serverId": server_id,
                                             "authToken": token, "jwtSecret": secret}
path.write_text(json.dumps(config, indent=2))
path.chmod(0o600)
PY
loginctl enable-linger root
for _ in $(seq 100); do [[ -S /run/user/0/bus ]] && break; sleep 0.1; done
[[ -S /run/user/0/bus ]] || { echo "user manager did not start" >&2; exit 1; }
export XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
flowly service install --start
systemctl --user show ai.flowly.gateway --property=MainPID --value
