#!/usr/bin/env bash
# Install Flowly CLI/TUI on macOS and Linux from a git checkout.
#
# The installer clones the Flowly repository, builds an isolated virtualenv with
# uv (which downloads and manages Python itself — no system Python required), and
# installs Flowly into it as an editable checkout. Because the install lives in a
# real git checkout, `flowly update` can fast-forward it with `git pull` instead
# of waiting for a PyPI release. The installed command is the console script:
# flowly.

set -euo pipefail

FLOWLY_REPO_URL="${FLOWLY_REPO_URL:-https://github.com/Nocetic/flowly.git}"
FLOWLY_BRANCH="${FLOWLY_BRANCH:-main}"
FLOWLY_SRC="${FLOWLY_SRC:-${HOME}/.local/share/flowly/repo}"
FLOWLY_VENV="${FLOWLY_VENV:-${HOME}/.local/share/flowly/venv}"
FLOWLY_PYTHON="${FLOWLY_PYTHON:-3.12}"
FLOWLY_SKIP_BOOTSTRAP="${FLOWLY_SKIP_BOOTSTRAP:-0}"
FLOWLY_NO_PATH_UPDATE="${FLOWLY_NO_PATH_UPDATE:-0}"
FLOWLY_SKIP_SYSTEM_DEPS="${FLOWLY_SKIP_SYSTEM_DEPS:-0}"
FLOWLY_TOOL_BIN_DIR="${FLOWLY_TOOL_BIN_DIR:-}"

usage() {
  cat <<'EOF'
Install Flowly CLI/TUI from a git checkout with uv.

Usage:
  curl -fsSL https://useflowlyapp.com/install.sh | bash
  bash scripts/install.sh [options]

Options:
  --branch NAME         Git branch to track (default: main)
  --src PATH            Where to clone the checkout (default: ~/.local/share/flowly/repo)
  --python VERSION      Python version managed by uv (default: 3.12)
  --bin-dir PATH        Install the flowly launcher into this PATH directory
  --skip-bootstrap      Do not run "flowly bootstrap"
  --skip-system-deps    Do not install optional tools (ffmpeg, ripgrep)
  --no-path-update      Do not edit shell profile files
  -h, --help            Show this help

Environment:
  FLOWLY_REPO_URL         Git remote to clone (default: GitHub)
  FLOWLY_BRANCH           Branch to track (default: main)
  FLOWLY_SRC              Checkout directory (default: ~/.local/share/flowly/repo)
  FLOWLY_VENV             Virtualenv directory (default: ~/.local/share/flowly/venv)
  FLOWLY_PYTHON           Python version for the uv managed venv (default: 3.12)
  FLOWLY_SKIP_SYSTEM_DEPS Set to 1 to skip optional tool install
  FLOWLY_TOOL_BIN_DIR     Directory to place the flowly launcher in
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      FLOWLY_BRANCH="$2"
      shift 2
      ;;
    --branch=*)
      FLOWLY_BRANCH="${1#--branch=}"
      shift
      ;;
    --src)
      FLOWLY_SRC="$2"
      shift 2
      ;;
    --src=*)
      FLOWLY_SRC="${1#--src=}"
      shift
      ;;
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

# Where to drop the flowly launcher: an explicit override, else uv's bin dir
# (usually ~/.local/bin), else ~/.local/bin.
launcher_bin_dir() {
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

# ── Git ─────────────────────────────────────────────────────────────────────
#
# Unlike the optional tools above, git is REQUIRED: the install is a checkout and
# `flowly update` fast-forwards it. Install it via the platform package manager
# when missing; bail with a clear hint if we can't.
ensure_git() {
  if command -v git >/dev/null 2>&1; then
    log "Using git: $(git --version)"
    return 0
  fi

  log "git not found — installing it (required for the git-checkout install)..."
  local pm
  pm="$(detect_pkg_manager)"
  if [[ -z "$pm" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      err "git is required. Install the Xcode Command Line Tools first:"
      err "  xcode-select --install"
    else
      err "git is required and no supported package manager was found."
      err "Install git with your package manager, then re-run this installer."
    fi
    exit 1
  fi

  if ! pkg_install "$pm" git; then
    err "Could not install git automatically. Install it manually: ${pm} install git"
    exit 1
  fi
  refresh_path
  if ! command -v git >/dev/null 2>&1; then
    err "git was installed but is not on PATH. Open a new shell and retry."
    exit 1
  fi
  ok "Installed git: $(git --version)"
}

# ── Migration ───────────────────────────────────────────────────────────────
#
# Older installs used `uv tool install flowly-ai` (a PyPI package). Remove it so
# its launcher doesn't shadow the new git-checkout launcher and to free the
# package name. Best-effort — a missing/old uv must never abort a fresh install.
migrate_uv_tool() {
  if uv tool list 2>/dev/null | grep -q '^flowly-ai\b'; then
    log "Removing the previous PyPI install (uv tool flowly-ai)..."
    uv tool uninstall flowly-ai >/dev/null 2>&1 || true
  fi
}

# ── Source checkout ─────────────────────────────────────────────────────────
#
# Clone the repo, or fast-forward an existing checkout to the tip of the tracked
# branch. The clone is a full single-branch clone (NOT --depth 1) so that
# `flowly update`'s `git rev-list --count HEAD..origin/<branch>` can measure how
# far behind the checkout is.
clone_or_update_repo() {
  if [[ -d "${FLOWLY_SRC}/.git" ]]; then
    log "Updating existing checkout at ${FLOWLY_SRC}..."
    git -C "$FLOWLY_SRC" remote set-url origin "$FLOWLY_REPO_URL" 2>/dev/null || true
    git -C "$FLOWLY_SRC" fetch --prune origin "$FLOWLY_BRANCH"
    git -C "$FLOWLY_SRC" checkout "$FLOWLY_BRANCH" 2>/dev/null \
      || git -C "$FLOWLY_SRC" checkout -B "$FLOWLY_BRANCH" "origin/${FLOWLY_BRANCH}"
    git -C "$FLOWLY_SRC" reset --hard "origin/${FLOWLY_BRANCH}"
    return 0
  fi

  if [[ -e "$FLOWLY_SRC" && -n "$(ls -A "$FLOWLY_SRC" 2>/dev/null)" ]]; then
    err "Install directory ${FLOWLY_SRC} exists but is not a git checkout."
    err "Move it aside (or set FLOWLY_SRC to another path), then re-run."
    exit 1
  fi

  log "Cloning ${FLOWLY_REPO_URL} (branch ${FLOWLY_BRANCH}) into ${FLOWLY_SRC}..."
  mkdir -p "$(dirname "$FLOWLY_SRC")"
  git clone --branch "$FLOWLY_BRANCH" --single-branch "$FLOWLY_REPO_URL" "$FLOWLY_SRC"
}

# Build the isolated venv and install Flowly into it as an editable checkout.
# Editable + a venv OUTSIDE the checkout is what keeps `detect_install_mode()`
# reporting "source" (sys.prefix is this venv, not uv/tools or pipx/venvs), which
# is what routes `flowly update` to the git-pull path.
install_from_source() {
  log "Creating Flowly virtualenv at ${FLOWLY_VENV} (Python ${FLOWLY_PYTHON})..."
  # --clear: the installer is meant to be re-run (it's the documented way to
  # force-refresh an install), so a venv already existing at this path from a
  # prior run must not abort it.
  uv venv --clear --python "$FLOWLY_PYTHON" "$FLOWLY_VENV"

  log "Installing Flowly (editable) from ${FLOWLY_SRC}..."
  uv pip install --python "${FLOWLY_VENV}/bin/python" -e "$FLOWLY_SRC"
}

# Symlink the venv's flowly entry point into a PATH directory. The symlink keeps
# the venv's interpreter shebang, so `flowly` always runs against this venv.
install_launcher() {
  local bin_dir="$1"
  local venv_flowly="${FLOWLY_VENV}/bin/flowly"

  if [[ ! -x "$venv_flowly" ]]; then
    err "The editable install did not produce ${venv_flowly}."
    exit 1
  fi
  mkdir -p "$bin_dir"
  ln -sf "$venv_flowly" "${bin_dir}/flowly"
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
  ensure_git

  clone_or_update_repo
  install_from_source

  # Prove the new venv works before touching any previous install, so a failed
  # clone/build can never leave the machine with no working flowly.
  "${FLOWLY_VENV}/bin/flowly" --version >/dev/null

  # Only now retire the old PyPI/uv-tool install — and BEFORE we create our own
  # launcher, so uv's uninstall (which deletes the launchers it created in the
  # same bin dir) can't clobber the symlink we're about to write.
  migrate_uv_tool

  local bin_dir flowly_bin
  bin_dir="$(launcher_bin_dir)"
  install_launcher "$bin_dir"
  export PATH="${bin_dir}:${PATH}"

  if ! flowly_bin="$(resolve_flowly "$bin_dir")"; then
    err "Flowly was installed, but the flowly launcher was not found."
    err "Try opening a new terminal, or add this directory to PATH: ${bin_dir}"
    exit 1
  fi

  update_path "$(dirname "$flowly_bin")"

  "$flowly_bin" --version >/dev/null

  install_system_deps

  ok "Flowly CLI installed (git checkout: ${FLOWLY_SRC})."
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
