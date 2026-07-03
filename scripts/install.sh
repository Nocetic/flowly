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

# The user's PATH as it was before we touch it. Under `curl | bash` the script
# runs in a child shell, so any PATH we export here never reaches the parent —
# we use this snapshot at the end to tell whether `flowly` is actually reachable
# from the user's shell, and print an activation hint when it isn't.
FLOWLY_INBOUND_PATH="${PATH}"

FLOWLY_REPO_URL="${FLOWLY_REPO_URL:-https://github.com/Nocetic/flowly.git}"
FLOWLY_BRANCH="${FLOWLY_BRANCH:-main}"
FLOWLY_SRC="${FLOWLY_SRC:-${HOME}/.local/share/flowly/repo}"
FLOWLY_VENV="${FLOWLY_VENV:-${HOME}/.local/share/flowly/venv}"
FLOWLY_PYTHON="${FLOWLY_PYTHON:-3.12}"
FLOWLY_SKIP_BOOTSTRAP="${FLOWLY_SKIP_BOOTSTRAP:-0}"
FLOWLY_NO_PATH_UPDATE="${FLOWLY_NO_PATH_UPDATE:-0}"
FLOWLY_SKIP_SYSTEM_DEPS="${FLOWLY_SKIP_SYSTEM_DEPS:-0}"
FLOWLY_TOOL_BIN_DIR="${FLOWLY_TOOL_BIN_DIR:-}"
FLOWLY_VERBOSE="${FLOWLY_VERBOSE:-0}"
FLOWLY_PROGRESS_TAIL_LINES="${FLOWLY_PROGRESS_TAIL_LINES:-12}"
[[ "$FLOWLY_PROGRESS_TAIL_LINES" =~ ^[0-9]+$ ]] || FLOWLY_PROGRESS_TAIL_LINES=12
FLOWLY_PROGRESS_RENDERED_LINES=0
FLOWLY_PROGRESS_LAST_FRAME=""
FLOWLY_PROGRESS_CURSOR_HIDDEN=0
FLOWLY_COLOR_BLUE=""
FLOWLY_COLOR_BLUE_SOFT=""
FLOWLY_COLOR_BLUE_MUTED=""
FLOWLY_COLOR_GREEN=""
FLOWLY_COLOR_RED=""
FLOWLY_COLOR_DIM=""
FLOWLY_COLOR_RESET=""

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
  FLOWLY_VERBOSE          Set to 1 to show raw command output
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

init_colors() {
  [[ -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]] || return 0
  [[ -t 1 ]] || return 0
  FLOWLY_COLOR_BLUE=$'\033[38;5;45m'
  FLOWLY_COLOR_BLUE_SOFT=$'\033[38;5;81m'
  FLOWLY_COLOR_BLUE_MUTED=$'\033[38;5;75m'
  FLOWLY_COLOR_GREEN=$'\033[38;5;49m'
  FLOWLY_COLOR_RED=$'\033[38;5;203m'
  FLOWLY_COLOR_DIM=$'\033[2m'
  FLOWLY_COLOR_RESET=$'\033[0m'
}

log() { printf '%s[flowly]%s %s\n' "$FLOWLY_COLOR_BLUE" "$FLOWLY_COLOR_RESET" "$*"; }
ok() { printf '%s[flowly]%s %s\n' "$FLOWLY_COLOR_GREEN" "$FLOWLY_COLOR_RESET" "$*"; }
err() { printf '%s[flowly]%s %s\n' "$FLOWLY_COLOR_RED" "$FLOWLY_COLOR_RESET" "$*" >&2; }

print_banner() {
  local cols
  cols="$(progress_columns)"
  if (( cols < 56 )); then
    printf '%sFLOWLY%s\n\n' "$FLOWLY_COLOR_BLUE_SOFT" "$FLOWLY_COLOR_RESET"
    return 0
  fi

  printf '%s ███████╗██╗      ██████╗ ██╗    ██╗██╗  ██╗   ██╗%s\n' "$FLOWLY_COLOR_BLUE_SOFT" "$FLOWLY_COLOR_RESET"
  printf '%s ██╔════╝██║     ██╔═══██╗██║    ██║██║  ╚██╗ ██╔╝%s\n' "$FLOWLY_COLOR_BLUE_SOFT" "$FLOWLY_COLOR_RESET"
  printf '%s █████╗  ██║     ██║   ██║██║ █╗ ██║██║   ╚████╔╝ %s\n' "$FLOWLY_COLOR_BLUE" "$FLOWLY_COLOR_RESET"
  printf '%s ██╔══╝  ██║     ██║   ██║██║███╗██║██║    ╚██╔╝  %s\n' "$FLOWLY_COLOR_BLUE" "$FLOWLY_COLOR_RESET"
  printf '%s ██║     ███████╗╚██████╔╝╚███╔███╔╝███████╗██║   %s\n' "$FLOWLY_COLOR_BLUE_MUTED" "$FLOWLY_COLOR_RESET"
  printf '%s ╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝╚═╝   %s\n\n' "$FLOWLY_COLOR_BLUE_MUTED" "$FLOWLY_COLOR_RESET"
}

animation_available() {
  [[ "$FLOWLY_VERBOSE" != "1" ]] || return 1
  [[ -t 1 ]] || return 1
  [[ -r /dev/tty && -w /dev/tty ]] || return 1
  [[ "${TERM:-}" != "dumb" ]] || return 1
  return 0
}

format_elapsed() {
  local seconds="$1"
  printf '%02d:%02d' "$((seconds / 60))" "$((seconds % 60))"
}

progress_bar() {
  local fill="$1"
  local width=10
  local bar=""
  local i

  [[ "$fill" =~ ^[0-9]+$ ]] || fill=0
  (( fill < 0 )) && fill=0
  (( fill > width )) && fill=$width

  for ((i = 0; i < width; i++)); do
    if (( i < fill )); then
      bar+="#"
    else
      bar+="-"
    fi
  done
  printf '%s' "$bar"
}

progress_bar_render() {
  local fill="$1"
  local raw filled empty
  raw="$(progress_bar "$fill")"
  filled="${raw%%-*}"
  empty="${raw#"$filled"}"
  printf '%s%s%s%s%s' \
    "$FLOWLY_COLOR_BLUE" "$filled" \
    "$FLOWLY_COLOR_BLUE_MUTED" "$empty" \
    "$FLOWLY_COLOR_RESET"
}

progress_columns() {
  local cols="${COLUMNS:-}"
  [[ "$cols" =~ ^[0-9]+$ ]] || cols=80
  (( cols < 40 )) && cols=80
  printf '%s\n' "$cols"
}

count_lines() {
  local text="$1"
  if [[ -z "$text" ]]; then
    printf '0'
  else
    printf '%s\n' "$text" | wc -l | tr -d ' '
  fi
}

clear_progress() {
  if (( FLOWLY_PROGRESS_RENDERED_LINES > 0 )); then
    printf '\033[%dA\033[J' "$FLOWLY_PROGRESS_RENDERED_LINES"
    FLOWLY_PROGRESS_RENDERED_LINES=0
    FLOWLY_PROGRESS_LAST_FRAME=""
  fi
}

hide_progress_cursor() {
  if (( FLOWLY_PROGRESS_CURSOR_HIDDEN == 0 )); then
    printf '\033[?25l'
    FLOWLY_PROGRESS_CURSOR_HIDDEN=1
  fi
}

show_progress_cursor() {
  if (( FLOWLY_PROGRESS_CURSOR_HIDDEN == 1 )); then
    printf '\033[?25h'
    FLOWLY_PROGRESS_CURSOR_HIDDEN=0
  fi
}

render_progress() {
  local fill="$1"
  local action="$2"
  local elapsed="$3"
  local logs_open="$4"
  local log_file="$5"
  local tail_output=""
  local tail_count=0
  local cols max_line max_action line block rendered_lines action_text

  cols="$(progress_columns)"
  max_action=$((cols - 24))
  (( max_action < 20 )) && max_action=20
  action_text="${action:0:max_action}"

  if [[ "$logs_open" == "1" ]]; then
    tail_output="$(tail -n "$FLOWLY_PROGRESS_TAIL_LINES" "$log_file" 2>/dev/null || true)"
    tail_count="$(count_lines "$tail_output")"
    (( tail_count == 0 )) && tail_count=1
  fi

  printf -v block '%s[flowly]%s Installing Flowly\n' "$FLOWLY_COLOR_BLUE" "$FLOWLY_COLOR_RESET"
  printf -v block '%s         [%s] %s\n' "$block" "$(progress_bar_render "$fill")" "$action_text"
  if [[ "$logs_open" == "1" ]]; then
    printf -v block '%s         %s%s elapsed | press o/Ctrl+O to hide logs%s\n' \
      "$block" "$FLOWLY_COLOR_BLUE_MUTED" "$(format_elapsed "$elapsed")" "$FLOWLY_COLOR_RESET"
  else
    printf -v block '%s         %s%s elapsed | press o/Ctrl+O for logs%s\n' \
      "$block" "$FLOWLY_COLOR_BLUE_MUTED" "$(format_elapsed "$elapsed")" "$FLOWLY_COLOR_RESET"
  fi
  rendered_lines=3

  if [[ "$logs_open" == "1" ]]; then
    max_line=$((cols - 9))
    (( max_line < 20 )) && max_line=20
    printf -v block '%s\n%s--- live log ---%s\n' "$block" "$FLOWLY_COLOR_BLUE_MUTED" "$FLOWLY_COLOR_RESET"
    rendered_lines=$((rendered_lines + 2 + tail_count))
    if [[ -z "$tail_output" ]]; then
      printf -v block '%s         waiting for log output\n' "$block"
    else
      while IFS= read -r line; do
        line="${line//$'\r'/}"
        printf -v block '%s         %s\n' "$block" "${line:0:max_line}"
      done <<< "$tail_output"
    fi
  fi

  [[ "$block" == "$FLOWLY_PROGRESS_LAST_FRAME" ]] && return 0
  clear_progress
  printf '%s' "$block"
  FLOWLY_PROGRESS_RENDERED_LINES="$rendered_lines"
  FLOWLY_PROGRESS_LAST_FRAME="$block"
}

run_flowly_command() {
  local start_fill="$1"
  local end_fill="$2"
  local action="$3"
  shift 3

  local tmp_dir log_file
  tmp_dir="${TMPDIR:-/tmp}"
  tmp_dir="${tmp_dir%/}"
  log_file="$(mktemp "${tmp_dir}/flowly-install.XXXXXX")"

  if [[ "$FLOWLY_VERBOSE" == "1" ]]; then
    log "$action..."
    set +e
    "$@"
    local status=$?
    set -e
    return "$status"
  fi

  if ! animation_available; then
    log "$action..."
    if "$@" >"$log_file" 2>&1; then
      ok "$action complete."
      return 0
    else
      local status=$?
      err "$action failed. Full log: $log_file"
      tail -n 120 "$log_file" >&2 || true
      return "$status"
    fi
  fi

  local start_time now elapsed fill status logs_open key cmd_pid
  start_time="$(date +%s)"
  logs_open=0

  "$@" >"$log_file" 2>&1 &
  cmd_pid=$!

  hide_progress_cursor
  while kill -0 "$cmd_pid" 2>/dev/null; do
    if IFS= read -r -s -n 1 -t 0.05 key </dev/tty; then
      case "$key" in
        o|O|$'\017') logs_open=$((1 - logs_open)) ;;
      esac
    fi

    now="$(date +%s)"
    elapsed=$((now - start_time))
    fill=$((start_fill + elapsed / 3))
    if (( fill >= end_fill )); then
      fill=$((end_fill - 1))
    fi
    (( fill < start_fill )) && fill=$start_fill
    (( fill < 1 )) && fill=1

    render_progress "$fill" "$action" "$elapsed" "$logs_open" "$log_file"
    sleep 0.20
  done

  if wait "$cmd_pid"; then
    status=0
  else
    status=$?
  fi

  now="$(date +%s)"
  elapsed=$((now - start_time))
  if (( status == 0 )); then
    render_progress "$end_fill" "$action" "$elapsed" "$logs_open" "$log_file"
    sleep 0.08
    clear_progress
    show_progress_cursor
    ok "$action complete in $(format_elapsed "$elapsed")."
    return 0
  fi

  clear_progress
  show_progress_cursor
  err "$action failed after $(format_elapsed "$elapsed"). Full log: $log_file"
  tail -n 120 "$log_file" >&2 || true
  return "$status"
}

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

  run_flowly_command 1 2 "Installing uv package manager" download_uv_installer
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
  if [[ -x "${FLOWLY_VENV}/bin/python" ]]; then
    # The installer is meant to be re-run (it's the documented way to
    # force-refresh an install). Reuse a healthy venv instead of recreating it:
    # on Windows a service may have its python.exe open, and --clear deleting
    # an in-use interpreter fails with a sharing violation. `uv pip install -e`
    # below is idempotent against an existing venv, same as `flowly update`.
    log "Reusing existing virtualenv at ${FLOWLY_VENV}..."
  else
    log "Creating Flowly virtualenv at ${FLOWLY_VENV} (Python ${FLOWLY_PYTHON})..."
    # --clear: only reached when (re)creating, to wipe any broken/partial
    # leftovers from an interrupted previous run.
    run_flowly_command 2 4 "Creating Python environment" \
      uv venv --clear --python "$FLOWLY_PYTHON" "$FLOWLY_VENV"
  fi

  log "Installing Flowly (editable) from ${FLOWLY_SRC}..."
  run_flowly_command 4 7 "Installing packages" \
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
  init_colors
  trap show_progress_cursor EXIT
  print_banner
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

  refresh_service "$flowly_bin"

  install_system_deps

  ok "Flowly CLI installed (git checkout: ${FLOWLY_SRC})."
  printf '\n'

  # First-run onboarding: when a terminal is available (even under
  # `curl | bash`, by reading /dev/tty), open the interactive account-or-API-key
  # picker right away — it also seeds the workspace. With no terminal
  # (CI / --skip-bootstrap), fall back to the non-interactive workspace seed.
  if [[ "$FLOWLY_SKIP_BOOTSTRAP" != "1" ]] && (: </dev/tty) 2>/dev/null; then
    "$flowly_bin" setup </dev/tty || "$flowly_bin" bootstrap || true
  elif [[ "$FLOWLY_SKIP_BOOTSTRAP" != "1" ]]; then
    "$flowly_bin" bootstrap || log "bootstrap failed; run 'flowly doctor --fix' after install."
  fi

  print_next_steps "$bin_dir"
}

# ── Background service refresh ──────────────────────────────────────────────
#
# A gateway service installed by a previous install has THAT install's launcher
# path baked into its unit (systemd ExecStart / launchd ProgramArguments).
# After we retire the old install, that binary is gone: `systemctl restart`
# reports ok but the gateway never binds its port again. Rewrite the unit
# against this install and restart it. This also means a re-run-to-update
# actually bounces the running gateway onto the new code.
refresh_service() {
  local flowly_bin="$1"
  local unit=""
  case "$(uname -s)" in
    Darwin) unit="${HOME}/Library/LaunchAgents/ai.flowly.gateway.plist" ;;
    Linux)  unit="${HOME}/.config/systemd/user/ai.flowly.gateway.service" ;;
  esac
  [[ -n "$unit" && -f "$unit" ]] || return 0

  log "Refreshing the background service to point at this install..."
  if "$flowly_bin" service install --start >/dev/null 2>&1; then
    ok "Background service updated and restarted."
  else
    log "Couldn't refresh the service automatically — run: flowly service install --start"
  fi
}

# Closing instructions. The key gotcha: under `curl | bash` the launcher's
# directory was added to your shell *profile*, but the CURRENT shell can't see
# it until it re-reads that profile — so `flowly` is "command not found" right
# after install until you either open a new terminal or run the export below.
# We detect this by testing the user's original (pre-script) PATH.
print_next_steps() {
  local bin_dir="$1"
  printf '\n'

  if ! ( export PATH="$FLOWLY_INBOUND_PATH"; command -v flowly >/dev/null 2>&1 ); then
    ok "One more step — put flowly on your PATH:"
    printf '  • this shell now:  export PATH="%s:$PATH"\n' "$bin_dir"
    if [[ "$FLOWLY_NO_PATH_UPDATE" == "1" ]]; then
      printf '  • new terminals:   add that line to your shell profile yourself\n'
    else
      printf '  • or just open a new terminal (your shell profile was updated)\n'
    fi
    printf '\n'
  fi

  printf 'Get started:\n'
  printf '  flowly service install --start   # run the gateway in the background\n'
  printf '  flowly                           # start chatting\n'
}

main
