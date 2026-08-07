#!/usr/bin/env bash
#
# dev-install.sh — one-shot developer setup: install.sh's sibling for people
# hacking on Flowly itself.
#
#   mkdir flowly-development && cd flowly-development
#   curl -fsSL https://raw.githubusercontent.com/Nocetic/flowly/main/scripts/dev-install.sh | bash
#
# One command, like the user installer — but everything lands in the directory
# you run it from, fully isolated from any installed Flowly:
#
#   flowly-development/
#   ├── flowly/        the git checkout (editable install — edit and run)
#   ├── home/          this instance's FLOWLY_HOME (config, workspace, memory)
#   └── flowly-dev     launcher: runs the checkout against home/
#
# What it does:
#   1. Installs uv if missing (same installer install.sh uses), checks git.
#   2. Clones the repo (or fast-forwards an existing ./flowly checkout).
#   3. Builds the venv and installs Flowly editable with dev tooling
#      (pytest + ruff).
#   4. Creates the dev home. If you have a real ~/.flowly/config.json it is
#      COPIED, so your providers and keys work immediately — with three edits:
#        • gateway.port → 18890, so it never collides with your real gateway;
#        • agents.defaults.workspace → home/workspace, so the dev agent can't
#          write into your real memory;
#        • every channel disabled — two bots on one Telegram token or one
#          relay identity poison each other's delivery. Re-enable per channel
#          on the dev instance only if you know the real bot isn't using it.
#      No real config? You get the normal `flowly setup` wizard, same as any
#      new user.
#   5. Writes the ./flowly-dev launcher.
#
# After that:
#   ./flowly-dev                terminal UI (starts the dev gateway on demand)
#   ./flowly-dev gateway        dev gateway in the foreground, port 18890
#   ./flowly-dev doctor         health check
#   cd flowly && uv run pytest  the test suite
#
# Flowly Desktop (engineers with the desktop repo): run it against this home —
#   FLOWLY_HOME=<this dir>/home npm run dev
# then sign in inside that Desktop; that writes fresh relay credentials into
# the dev home, and the composer chats with THIS bot while your real install
# keeps running untouched.
#
# Re-running is safe and is how you refresh the setup. Options via env:
#   FLOWLY_DEV_PORT=18795   pick a different dev gateway port
#   FLOWLY_REPO_URL=…       clone from a fork
#   FLOWLY_BRANCH=…         check out a branch other than main

set -euo pipefail

FLOWLY_REPO_URL="${FLOWLY_REPO_URL:-https://github.com/Nocetic/flowly.git}"
FLOWLY_BRANCH="${FLOWLY_BRANCH:-main}"
FLOWLY_DEV_PORT="${FLOWLY_DEV_PORT:-18890}"
FLOWLY_PYTHON="${FLOWLY_PYTHON:-3.12}"

log()  { printf '\033[34m[flowly-dev]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[flowly-dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[flowly-dev]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[flowly-dev]\033[0m %s\n' "$*" >&2; exit 1; }

printf '\n\033[1m  Flowly DEVELOPMENT setup\033[0m\n'
printf '  For hacking on Flowly itself. Everything stays in this directory —\n'
printf '  your installed Flowly (if any) is not touched. Users install with\n'
printf '  https://useflowlyapp.com/install.sh instead.\n\n'

# ── Layout ──────────────────────────────────────────────────────────────────
# Run from an empty/new directory → everything under it (flowly/, home/).
# Run from inside a checkout (scripts/dev-install.sh) → the checkout is the
# repo, and the dev home lives at .dev-home/ inside it (gitignored).
if [[ -f "pyproject.toml" ]] && grep -q '^name = "flowly-ai"' pyproject.toml 2>/dev/null; then
  DEV_ROOT="$(pwd)"
  REPO="$DEV_ROOT"
  DEV_HOME="$REPO/.dev-home"
  IN_REPO=1
else
  DEV_ROOT="$(pwd)"
  REPO="$DEV_ROOT/flowly"
  DEV_HOME="$DEV_ROOT/home"
  IN_REPO=0
fi
LAUNCHER="$DEV_ROOT/flowly-dev"
VENV="$REPO/.venv"

# ── Prerequisites ───────────────────────────────────────────────────────────
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
}

ensure_uv
command -v git >/dev/null 2>&1 || die "git is required — install it and re-run."

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

# ── Repo ────────────────────────────────────────────────────────────────────
if [[ "$IN_REPO" == "1" ]]; then
  log "Using this checkout: $REPO"
elif [[ -d "$REPO/.git" ]]; then
  log "Updating existing checkout at ${REPO} ..."
  git -C "$REPO" fetch --prune origin "$FLOWLY_BRANCH"
  git -C "$REPO" checkout "$FLOWLY_BRANCH" 2>/dev/null || true
  # Never touch a dirty tree or a feature branch — a dev checkout is the
  # developer's to manage. Only fast-forward a clean main.
  if [[ -z "$(git -C "$REPO" status --porcelain)" \
     && "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" == "$FLOWLY_BRANCH" ]]; then
    git -C "$REPO" merge --ff-only "origin/${FLOWLY_BRANCH}" || true
  else
    warn "Checkout has local work — leaving it exactly as it is."
  fi
else
  log "Cloning ${FLOWLY_REPO_URL} (branch ${FLOWLY_BRANCH}) into ${REPO} ..."
  git clone --branch "$FLOWLY_BRANCH" "$FLOWLY_REPO_URL" "$REPO"
fi

# ── Environment ─────────────────────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python" ]]; then
  log "Creating virtualenv (Python ${FLOWLY_PYTHON})…"
  (cd "$REPO" && uv venv --python "$FLOWLY_PYTHON")
fi
log "Installing Flowly editable (with dev tooling)…"
uv pip install --python "$VENV/bin/python" -e "$REPO[dev]" --quiet
"$VENV/bin/flowly" --version >/dev/null || die "The editable install did not produce a working flowly."

# ── Dev home ────────────────────────────────────────────────────────────────
mkdir -p "$DEV_HOME"
REAL_CONFIG="$HOME/.flowly/config.json"
DEV_CONFIG="$DEV_HOME/config.json"

# Applied on every run: port + workspace are the isolation invariants.
# Channel state is NOT touched here — signing in from a dev Desktop enables
# the dev instance's own relay channel, and a re-run must not sever it.
patch_invariants() {
  "$VENV/bin/python" - "$DEV_CONFIG" "$DEV_HOME" "$FLOWLY_DEV_PORT" <<'EOF'
import json, sys
from pathlib import Path
cfg_path, dev_home, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
cfg = json.loads(cfg_path.read_text())
cfg.setdefault("gateway", {})["port"] = port
cfg.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(Path(dev_home) / "workspace")
cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n")
EOF
}

if [[ ! -f "$DEV_CONFIG" && -f "$REAL_CONFIG" ]]; then
  log "Copying your real config (providers and keys work immediately)…"
  cp "$REAL_CONFIG" "$DEV_CONFIG"
  # Once, at copy time: the copied channel credentials belong to the REAL
  # bot. Two bots sharing one Telegram token or one relay identity steal
  # each other's messages, so everything copied starts disabled. The dev
  # instance earns its own relay identity (a separate "<machine>-dev"
  # server) the moment you sign in — from ./flowly-dev login, or inside a
  # FLOWLY_HOME-pointed Desktop — and that state is never touched again.
  "$VENV/bin/python" - "$DEV_CONFIG" <<'EOF'
import json, sys
from pathlib import Path
cfg_path = Path(sys.argv[1])
cfg = json.loads(cfg_path.read_text())
for channel in (cfg.get("channels") or {}).values():
    if isinstance(channel, dict) and channel.get("enabled"):
        channel["enabled"] = False
cfg_path.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n")
EOF
  patch_invariants
  ok "Dev config ready: port ${FLOWLY_DEV_PORT}, isolated workspace, copied channels disabled."
elif [[ -f "$DEV_CONFIG" ]]; then
  patch_invariants
  ok "Dev config refreshed: port ${FLOWLY_DEV_PORT}, isolated workspace (channel state untouched)."
else
  log "No existing Flowly config found — running the normal setup wizard…"
  printf '\n'
  warn "You are configuring the DEVELOPMENT instance (its own home, port ${FLOWLY_DEV_PORT})."
  warn "Signing in with a Flowly account here registers a separate \"<machine>-dev\""
  warn "server — your real Flowly, if you install one later, is unaffected."
  printf '\n'
  if [[ -t 0 || -c /dev/tty ]] && FLOWLY_HOME="$DEV_HOME" "$VENV/bin/flowly" setup </dev/tty; then
    :
  else
    warn "No terminal for the wizard — run ./flowly-dev setup later to pick a provider."
  fi
  # Whatever setup wrote (or didn't), enforce the isolation invariants.
  [[ -f "$DEV_CONFIG" ]] && patch_invariants
fi

FLOWLY_HOME="$DEV_HOME" "$VENV/bin/flowly" bootstrap >/dev/null 2>&1 || true

# ── Launcher ────────────────────────────────────────────────────────────────
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Runs the flowly checkout in this directory against its isolated dev home.
export FLOWLY_HOME="$DEV_HOME"
exec "$VENV/bin/flowly" "\$@"
EOF
chmod +x "$LAUNCHER"

# ── Done ────────────────────────────────────────────────────────────────────
printf '\n'
ok "Development install ready."
printf '\n'
printf '  %-34s %s\n' "./flowly-dev"                 "terminal UI (starts the dev gateway on demand)"
printf '  %-34s %s\n' "./flowly-dev gateway"         "dev gateway in the foreground (port ${FLOWLY_DEV_PORT})"
printf '  %-34s %s\n' "./flowly-dev doctor"          "config + runtime health"
printf '  %-34s %s\n' "(cd ${REPO##"$DEV_ROOT"/} && uv run pytest)" "test suite"
printf '\n'
printf 'Your real Flowly install is untouched: this instance has its own home\n'
printf '(%s),\nits own port (%s), and no channels enabled.\n' "$DEV_HOME" "$FLOWLY_DEV_PORT"
printf '\n'
printf 'Working on Flowly Desktop too? Point it at this home:\n'
printf '  FLOWLY_HOME=%s npm run dev\n' "$DEV_HOME"
printf 'and sign in inside that Desktop — the composer then chats with this bot.\n'
