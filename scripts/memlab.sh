#!/usr/bin/env bash
# Isolated memory-governance sandbox.
#
# Why FLOWLY_PROFILE (not FLOWLY_HOME): the CLI's entry.py runs set_profile() on
# every invocation, which OVERWRITES any FLOWLY_HOME you export. FLOWLY_PROFILE
# survives (entry.py reads it and points FLOWLY_HOME at ~/.flowly/profiles/<name>).
# But a profile alone does NOT isolate the workspace — workspace_path defaults to
# the absolute ~/.flowly/workspace. So we also drop a config.json in the profile
# whose workspace points INSIDE the profile dir. Now config + workspace +
# governance db + KG are all under ~/.flowly/profiles/<name>, never the real one.
#
# Usage:
#   scripts/memlab.sh seed                 # write a sample legacy MEMORY.md
#   scripts/memlab.sh migrate|list|stats|review|refresh
#   scripts/memlab.sh accept <id> | reject <id> | correct <id> "text" | undo <id>
#   scripts/memlab.sh py "<python>"        # run python in the SAME isolated home
#   scripts/memlab.sh reset                # wipe the sandbox
set -euo pipefail
PROFILE="${FLOWLY_MEMLAB_PROFILE:-memlab}"
HOME_DIR="$HOME/.flowly/profiles/$PROFILE"
export FLOWLY_PROFILE="$PROFILE"      # makes the CLI use this home
export FLOWLY_HOME="$HOME_DIR"        # makes raw `python` (no entry.py) agree
mkdir -p "$HOME_DIR/workspace/memory"
[ -f "$HOME_DIR/config.json" ] || printf '{"agents":{"defaults":{"workspace":"%s/workspace"}}}\n' "$HOME_DIR" > "$HOME_DIR/config.json"

case "${1:-}" in
  seed)
    printf '<!-- 2026-06-01 10:00 -->\nprefers dark mode\n<!-- 2026-06-02 11:00 -->\nuses zsh on macOS\n<!-- 2026-06-03 09:00 -->\nmy email is demo@example.com\n' \
      > "$HOME_DIR/workspace/memory/MEMORY.md"
    echo "seeded $HOME_DIR/workspace/memory/MEMORY.md" ;;
  reset) rm -rf "$HOME_DIR"; echo "wiped $HOME_DIR" ;;
  py) shift; exec uv run --extra dev python -c "$1" ;;
  ids)  # print just the item ids (newest store), so you can copy one
    exec uv run --extra dev python - <<'PY'
from flowly.config.loader import get_data_dir
from flowly.memory.governance import GovernanceStore
g = GovernanceStore(get_data_dir() / "memory_governance.sqlite3")
for i in g.list_items():
    print(i.id, i.status, i.kind, repr(i.text))
PY
    ;;
  demo)  # one-shot live lifecycle, no id copying needed
    "$0" reset >/dev/null 2>&1 || true
    echo "── seed ──";    "$0" seed
    echo "── migrate ──"; "$0" migrate
    echo "── list ──";    "$0" list
    IDS=$(uv run --extra dev python - <<'PY'
from flowly.config.loader import get_data_dir
from flowly.memory.governance import GovernanceStore
g = GovernanceStore(get_data_dir() / "memory_governance.sqlite3")
print(" ".join(i.id for i in g.list_items()))
PY
)
    set -- $IDS
    echo "── accept $1 ──";              "$0" accept "$1"
    echo "── correct $2 ──";             "$0" correct "$2" "uses zsh + tmux on macOS"
    echo "── reject $3 ──";              "$0" reject "$3"
    echo "── stats ──";                  "$0" stats
    echo "── refresh → MEMORY.md ──";    "$0" refresh
    echo "── generated MEMORY.md ──";    cat "$HOME_DIR/workspace/memory/MEMORY.md"
    ;;
  "") echo "isolated home: $HOME_DIR
subcommands: demo | seed | migrate | list | ids | review | stats | refresh | reset
             accept <id> | reject <id> | correct <id> \"text\" | undo <id>" ;;
  *) exec uv run --extra dev flowly memory "$@" ;;
esac
