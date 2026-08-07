#!/usr/bin/env bash
#
# dev-gateway.sh — serve the gateway from THIS checkout, on the port Flowly
# Desktop and the iOS app connect to.
#
# Desktop and iOS read ~/.flowly and connect to the configured gateway port
# (18790 by default), so there is no isolated way to develop against them: the
# dev build has to take over that port. This script does the handover safely.
#
# Clone the repo and run this — it needs nothing else on the machine:
#
#   1. Installs uv if it's missing (same installer install.sh uses). `uv run`
#      then builds the virtualenv and installs Flowly on first use, so there is
#      no separate setup step. The first run takes a minute; later ones don't.
#   2. Stops the installed background service, if there is one. (`kill` is not
#      enough — launchd KeepAlive and systemd Restart=always bring it back.)
#   3. Refuses to continue if anything else still holds the port, naming it,
#      rather than killing a process it doesn't own.
#   4. Runs `uv run flowly gateway` from this checkout, in the foreground.
#   5. On exit (Ctrl+C included), reinstalls and restarts the service it
#      stopped, so the machine goes back to its normal setup.
#
# For tests and lint you also want the dev tooling, which `uv run` alone does
# not install:  uv pip install -e ".[dev]"
#
# This runs your development code against your REAL ~/.flowly — the same
# memory, sessions, and channel tokens your everyday agent uses. For everyday
# isolated development use scripts/dev-install.sh instead; this script is the
# advanced path (see CONTRIBUTING.md, "running against your REAL install").
#
# Usage: scripts/dev-gateway.sh [--port N] [-- <extra flowly gateway args>]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --port=*) PORT="${1#--port=}"; shift ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

log()  { printf '\033[34m[dev-gateway]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dev-gateway]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[dev-gateway]\033[0m %s\n' "$*" >&2; exit 1; }

# uv is the only prerequisite, and installing it is not the developer's problem
# any more than it is a user's — install.sh does this for them, so do it here.
# Everything after it (the virtualenv, the dependencies, Flowly itself) is built
# by `uv run` on first use.
refresh_path() {
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
}

ensure_uv() {
  refresh_path
  command -v uv >/dev/null 2>&1 && return 0

  log "Installing uv (the one prerequisite)…"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    die "curl or wget is required to install uv."
  fi
  refresh_path

  command -v uv >/dev/null 2>&1 \
    || die "uv installed but isn't on PATH — open a new terminal and re-run."
  log "Using uv $(uv --version 2>/dev/null | awk '{print $2}')"
}
ensure_uv

# Not fatal: Flowly runs without these, but voice/media (ffmpeg) and the agent's
# shell tooling (ripgrep) don't. install.sh installs them; say so rather than
# letting those features fail later for no visible reason.
missing_deps=()
command -v ffmpeg >/dev/null 2>&1 || missing_deps+=("ffmpeg")
command -v rg >/dev/null 2>&1 || missing_deps+=("ripgrep")
if [[ ${#missing_deps[@]} -gt 0 ]]; then
  warn "Missing (optional): ${missing_deps[*]} — voice/media and the agent's search tooling need them."
  case "$(uname -s)" in
    Darwin) warn "  brew install ${missing_deps[*]}" ;;
    Linux)  warn "  sudo apt install -y ${missing_deps[*]}" ;;
  esac
fi

# Guard against the two-configs trap. This script exists to serve Desktop and
# iOS, and they only ever read the real ~/.flowly — the relay credentials the
# composer needs (channels.web serverId/authToken, written by Desktop sign-in)
# live in THAT config. With FLOWLY_HOME pointing at an isolated dev home (a
# dev-install.sh setup), the gateway boots from a config with no channels, prints
# "Warning: No channels enabled", and the dashboard shows it running while the
# composer never sees it. Refuse that silent half-working state.
if [[ -n "${FLOWLY_HOME:-}" && "${FLOWLY_HOME%/}" != "${HOME}/.flowly" ]]; then
  warn "FLOWLY_HOME is set to: ${FLOWLY_HOME}"
  warn "Desktop and iOS only read ~/.flowly — with an isolated home the gateway"
  warn "has no relay credentials, so the dashboard will show it running but the"
  warn "composer/chat will never see it."
  warn "  fix:  unset FLOWLY_HOME   (also remove it from ~/.zshrc etc.), re-run"
  die  "  (running an isolated instance on purpose? use 'uv run flowly gateway' directly — this script is only for the Desktop/iOS handover)"
fi

# The port Desktop/iOS will look for: --port wins, else config, else the default.
if [[ -z "$PORT" ]]; then
  PORT="$(python3 - <<'EOF' 2>/dev/null || true
import json, os, pathlib
home = pathlib.Path(os.environ.get("FLOWLY_HOME") or (pathlib.Path.home() / ".flowly"))
try:
    print(int(json.loads((home / "config.json").read_text()).get("gateway", {}).get("port") or 18790))
except Exception:
    pass
EOF
)"
fi
PORT="${PORT:-18790}"

# "<pid> <command>" of whatever listens on $PORT, or empty. Never fails: lsof
# exits non-zero when it matches nothing, and under `set -e` that would end the
# script the moment the port turned out to be free.
port_holder() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fpc 2>/dev/null \
      | awk '/^p/{pid=substr($0,2)} /^c/{print pid, substr($0,2); exit}' || true
  fi
  return 0
}

# A service this machine actually has installed — the only thing we may stop.
service_installed() {
  [[ -f "${HOME}/Library/LaunchAgents/ai.flowly.gateway.plist" ]] && return 0
  [[ -f "${HOME}/.config/systemd/user/ai.flowly.gateway.service" ]] && return 0
  return 1
}

INSTALLED_FLOWLY="$(command -v flowly || true)"
SERVICE_STOPPED=0
GATEWAY_PID=""

# Stop the dev gateway we started. Ctrl+C in a terminal reaches it directly (same
# process group), but a signal sent to this script alone doesn't — so shut it
# down explicitly, and clear the port before handing it back to the service.
stop_dev_gateway() {
  [[ -n "$GATEWAY_PID" ]] || return 0
  kill "$GATEWAY_PID" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -z "$(port_holder)" ]] && break
    sleep 0.5
  done
  # `uv run` wraps the interpreter, so the process still on the port may be the
  # grandchild. We verified the port was free before starting, so it's ours.
  local leftover
  leftover="$(port_holder)"
  [[ -n "$leftover" ]] && kill "${leftover%% *}" 2>/dev/null || true
  GATEWAY_PID=""
}

cleanup() {
  stop_dev_gateway
  if [[ "$SERVICE_STOPPED" == "1" ]]; then
    SERVICE_STOPPED=0
    printf '\n'
    log "Restoring the background service…"
    if "$INSTALLED_FLOWLY" service install --port "$PORT" --start >/dev/null 2>&1; then
      log "Service is back on port ${PORT}."
    else
      warn "Couldn't restore it — run: flowly service install --start"
    fi
  fi
}
trap cleanup EXIT INT TERM

if service_installed; then
  if [[ -z "$INSTALLED_FLOWLY" ]]; then
    die "A gateway service is installed but no 'flowly' is on PATH to stop it."
  fi
  log "Stopping the installed gateway service…"
  "$INSTALLED_FLOWLY" service stop >/dev/null 2>&1 || warn "service stop reported an error; continuing"
  SERVICE_STOPPED=1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -z "$(port_holder)" ]] && break
    sleep 0.5
  done
fi

holder="$(port_holder)"
if [[ -n "$holder" ]]; then
  warn "Port ${PORT} is still held by: ${holder}"
  die "Stop that process yourself, then re-run. (A gateway started by hand is not a service — this script won't kill it.)"
fi

log "Serving port ${PORT} from ${REPO_ROOT} against ${FLOWLY_HOME:-$HOME/.flowly}"
log "Desktop and iOS will attach to it. Ctrl+C gives the machine back."
cd "$REPO_ROOT"
uv run flowly gateway --port "$PORT" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} &
GATEWAY_PID=$!
wait "$GATEWAY_PID" || true
