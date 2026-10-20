#!/usr/bin/env bash
# Runs inside the disposable Dockerfile.systemd container as root after boot.
# Creates an unprivileged `flowly` user whose sudo requires a password, installs
# this checkout for that user like install.sh (editable, uv in ~/.local/bin,
# user-writable venv) and starts `flowly service install --start` as that
# user's systemd unit on 127.0.0.1:18790. A TLS terminator on :8443 forwards to
# it, like a reverse proxy on a real VPS. No relay is configured.
# Usage: systemd_user_host.sh GATEWAY_TOKEN SUDO_PASSWORD /path/to/combined.pem
set -euo pipefail
token="$1" password="$2" pem="$3"
useradd --create-home --shell /bin/bash flowly
echo "flowly:$password" | chpasswd
echo 'flowly ALL=(ALL) ALL' > /etc/sudoers.d/flowly && chmod 0440 /etc/sudoers.d/flowly
install -d -o flowly -g flowly -m 0755 /home/flowly/.local /home/flowly/.local/bin
install -o flowly -g flowly -m 0755 /root/.local/bin/uv /home/flowly/.local/bin/uv
chown -R flowly:flowly /opt/flowly/.venv
uid="$(id -u flowly)"
loginctl enable-linger flowly
for _ in $(seq 100); do [[ -S "/run/user/$uid/bus" ]] && break; sleep 0.1; done
[[ -S "/run/user/$uid/bus" ]] || { echo "user manager did not start" >&2; exit 1; }
runuser -u flowly -- env HOME=/home/flowly FLOWLY_HOME=/home/flowly/.flowly \
  XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
  PATH="/home/flowly/.local/bin:/usr/local/bin:/usr/bin:/bin" TOKEN="$token" bash -euo pipefail -c '
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
