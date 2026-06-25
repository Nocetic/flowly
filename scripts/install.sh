#!/usr/bin/env bash
# Install Flowly CLI/TUI on macOS and Linux with uv.
#
# uv can download and manage Python itself, so this installer does not require
# a system Python installation. The installed command is the PyPI package's
# console script: flowly.

set -euo pipefail

FLOWLY_PACKAGE="${FLOWLY_PACKAGE:-flowly-ai}"
FLOWLY_PYTHON="${FLOWLY_PYTHON:-3.12}"
FLOWLY_SKIP_BOOTSTRAP="${FLOWLY_SKIP_BOOTSTRAP:-0}"
FLOWLY_NO_PATH_UPDATE="${FLOWLY_NO_PATH_UPDATE:-0}"
FLOWLY_SKIP_SYSTEM_DEPS="${FLOWLY_SKIP_SYSTEM_DEPS:-0}"
FLOWLY_TOOL_BIN_DIR="${FLOWLY_TOOL_BIN_DIR:-}"

usage() {
  cat <<'EOF'
Install Flowly CLI/TUI with uv.

Usage:
  curl -fsSL https://useflowlyapp.com/install.sh | bash
  bash scripts/install.sh [options]

Options:
  --python VERSION      Python version managed by uv (default: 3.12)
  --bin-dir PATH        Install the flowly launcher into PATH via UV_TOOL_BIN_DIR
  --skip-bootstrap      Do not run "flowly bootstrap"
  --skip-system-deps    Do not install optional tools (ffmpeg, ripgrep)
  --no-path-update      Do not edit shell profile files
  -h, --help            Show this help

Environment:
  FLOWLY_PACKAGE          PyPI package name (default: flowly-ai)
  FLOWLY_PYTHON           Python version for uv managed install (default: 3.12)
  FLOWLY_SKIP_SYSTEM_DEPS Set to 1 to skip optional tool install
  FLOWLY_TOOL_BIN_DIR     Optional uv tool executable directory
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      FLOWLY_PYTHON="$2"
      shift 2
      ;;
    --python=*)
      FLOWLY_PYTHON="${1#--python=}"
      shift
      ;;
    --bin-dir)
      FLOWLY_TOOL_BIN_DIR="$2"
      shift 2
      ;;
    --bin-dir=*)
      FLOWLY_TOOL_BIN_DIR="${1#--bin-dir=}"
      shift
      ;;
    --skip-bootstrap)
      FLOWLY_SKIP_BOOTSTRAP=1
      shift
      ;;
    --skip-system-deps)
      FLOWLY_SKIP_SYSTEM_DEPS=1
      shift
      ;;
    --no-path-update)
      FLOWLY_NO_PATH_UPDATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "flowly installer: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() { printf '\033[0;36m[flowly]\033[0m %s\n' "$*"; }
ok() { printf '\033[0;32m[flowly]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[flowly]\033[0m %s\n' "$*" >&2; }

detect_platform() {
  case "$(uname -s)" in
    Darwin|Linux) ;;
    *)
      err "Unsupported OS: $(uname -s). Use install.ps1 on Windows."
      exit 1
      ;;
  esac
}

refresh_path() {
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
  if [[ -n "$FLOWLY_TOOL_BIN_DIR" ]]; then
    export UV_TOOL_BIN_DIR="$FLOWLY_TOOL_BIN_DIR"
    export PATH="${FLOWLY_TOOL_BIN_DIR}:${PATH}"
  fi
}

download_uv_installer() {
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    err "curl or wget is required to install uv."
    exit 1
  fi
}

ensure_uv() {
  refresh_path
  if command -v uv >/dev/null 2>&1; then
    log "Using uv: $(uv --version)"
    return 0
  fi

  log "Installing uv package manager..."
  download_uv_installer
  refresh_path

  if ! command -v uv >/dev/null 2>&1; then
    err "uv installed but was not found in PATH. Open a new shell and retry."
    exit 1
  fi
  log "Using uv: $(uv --version)"
}

uv_bin_dir() {
  if [[ -n "$FLOWLY_TOOL_BIN_DIR" ]]; then
    printf '%s\n' "$FLOWLY_TOOL_BIN_DIR"
    return 0
  fi

  if uv tool dir --bin >/dev/null 2>&1; then
    uv tool dir --bin
  else
    printf '%s\n' "${HOME}/.local/bin"
  fi
}

add_path_line_once() {
  local file="$1"
  local line="$2"

  mkdir -p "$(dirname "$file")"
  touch "$file"
  if ! grep -Fqs "$line" "$file"; then
    {
      printf '\n'
      printf '# Flowly CLI\n'
      printf '%s\n' "$line"
    } >>"$file"
  fi
}

update_path() {
  [[ "$FLOWLY_NO_PATH_UPDATE" == "1" ]] && return 0

  local bin_dir="$1"
  local path_line="export PATH=\"${bin_dir}:\$PATH\""
  local shell_name
  shell_name="$(basename "${SHELL:-}")"

  case "$shell_name" in
    zsh)
      add_path_line_once "${HOME}/.zshrc" "$path_line"
      ;;
    bash)
      add_path_line_once "${HOME}/.bashrc" "$path_line"
      add_path_line_once "${HOME}/.bash_profile" "$path_line"
      ;;
    fish)
      add_path_line_once "${HOME}/.config/fish/config.fish" "fish_add_path ${bin_dir}"
      ;;
    *)
      add_path_line_once "${HOME}/.profile" "$path_line"
      ;;
  esac
}

resolve_flowly() {
  local bin_dir="$1"
  if [[ -x "${bin_dir}/flowly" ]]; then
    printf '%s\n' "${bin_dir}/flowly"
    return 0
  fi
  if command -v flowly >/dev/null 2>&1; then
    command -v flowly
    return 0
  fi
  return 1
}

# ── Optional system tools ───────────────────────────────────────────────────
#
# Flowly shells out to a couple of native binaries for certain features:
#   ffmpeg   — voice messages and the video skills (pixel-art, etc.)
#   ripgrep  — fast file search for the agent (it falls back to grep without it)
# These are NICE-TO-HAVE: Flowly degrades gracefully when they're missing. A
# Python wheel can't ship system binaries, so the installer offers to add them
# via the platform package manager. Everything here is best-effort and never
# aborts the install — losing an optional tool must not cost you a working CLI.
SYSTEM_DEPS=(ffmpeg ripgrep)

# The command a package provides (ripgrep installs the `rg` binary).
dep_command() {
  case "$1" in
    ripgrep) echo "rg" ;;
    *) echo "$1" ;;
  esac
}

# Package manager token for this platform (brew/apt-get/dnf/pacman/zypper), or
# empty when none is available.
detect_pkg_manager() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    command -v brew >/dev/null 2>&1 && echo "brew"
    return 0
  fi
  local pm
  for pm in apt-get dnf pacman zypper; do
    if command -v "$pm" >/dev/null 2>&1; then
      echo "$pm"
      return 0
    fi
  done
}

# Decide how to gain privilege for a Linux package install without ever
# hanging a piped `curl | bash`: root needs nothing, passwordless sudo is used
# silently, an available terminal lets sudo prompt once, otherwise we bail so
# the caller can fall back to a printed hint. Echoes "none" when we can't.
_resolve_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    echo ""        # already root
    return 0
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    echo "none"
    return 0
  fi
  if sudo -n true 2>/dev/null; then
    echo "sudo"    # passwordless
    return 0
  fi
  if (: </dev/tty) 2>/dev/null; then
    echo "sudo"    # a terminal exists — sudo may prompt for a password once
    return 0
  fi
  echo "none"
}

# Install the given packages with the resolved manager. Returns its exit code.
pkg_install() {
  local pm="$1"; shift
  if [[ "$pm" == "brew" ]]; then
    brew install "$@"
    return $?
  fi
  local sudo_cmd
  sudo_cmd="$(_resolve_sudo)"
  [[ "$sudo_cmd" == "none" ]] && return 99   # caller prints a manual hint
  case "$pm" in
    apt-get)
      $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null 2>&1 || true
      $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
      ;;
    dnf)    $sudo_cmd dnf install -y "$@" ;;
    pacman) $sudo_cmd pacman -S --noconfirm --needed "$@" ;;
    zypper) $sudo_cmd zypper install -y "$@" ;;
    *) return 1 ;;
  esac
}

install_system_deps() {
  [[ "$FLOWLY_SKIP_SYSTEM_DEPS" == "1" ]] && return 0

  local missing=()
  local dep
  for dep in "${SYSTEM_DEPS[@]}"; do
    if ! command -v "$(dep_command "$dep")" >/dev/null 2>&1; then
      missing+=("$dep")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    log "Optional tools already present: ${SYSTEM_DEPS[*]}"
    return 0
  fi

  local pm
  pm="$(detect_pkg_manager)"
  if [[ -z "$pm" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      log "Optional tools missing (${missing[*]}). Install Homebrew, then: brew install ${missing[*]}"
    else
      log "Optional tools missing (${missing[*]}). Add them with your package manager when convenient."
    fi
    return 0
  fi

  log "Installing optional tools (${missing[*]}) — for voice/audio and fast search..."
  if pkg_install "$pm" "${missing[@]}" >/dev/null 2>&1; then
    ok "Installed optional tools: ${missing[*]}"
  else
    log "Skipped optional tools (${missing[*]}) — Flowly works without them."
    log "  Add later: ${pm} install ${missing[*]}"
  fi
  return 0
}

main() {
  detect_platform
  ensure_uv

  log "Installing ${FLOWLY_PACKAGE} with uv managed Python ${FLOWLY_PYTHON}..."
  uv tool install --python "$FLOWLY_PYTHON" "$FLOWLY_PACKAGE" --force

  local bin_dir flowly_bin
  bin_dir="$(uv_bin_dir)"
  export PATH="${bin_dir}:${PATH}"

  if ! flowly_bin="$(resolve_flowly "$bin_dir")"; then
    err "Flowly was installed, but the flowly launcher was not found."
    err "Try opening a new terminal, or add this directory to PATH: ${bin_dir}"
    exit 1
  fi

  update_path "$(dirname "$flowly_bin")"

  "$flowly_bin" --version >/dev/null

  install_system_deps

  ok "Flowly CLI installed."
  printf '\n'

  # First-run onboarding: when a terminal is available (even under
  # `curl | bash`, by reading /dev/tty), open the interactive account-or-API-key
  # picker right away — it also seeds the workspace. With no terminal
  # (CI / --skip-bootstrap), fall back to the non-interactive workspace seed and
  # print the manual next steps.
  if [[ "$FLOWLY_SKIP_BOOTSTRAP" != "1" ]] && (: </dev/tty) 2>/dev/null; then
    "$flowly_bin" setup </dev/tty || "$flowly_bin" bootstrap || true
  else
    if [[ "$FLOWLY_SKIP_BOOTSTRAP" != "1" ]]; then
      "$flowly_bin" bootstrap || log "bootstrap failed; run 'flowly doctor --fix' after install."
    fi
    printf 'Get started (open a new shell first so PATH is picked up):\n'
    printf '  1. flowly setup                    # choose an account or API key\n'
    printf '  2. flowly service install --start  # run the gateway in the background\n'
    printf '  3. flowly                          # start chatting\n'
  fi
}

main
