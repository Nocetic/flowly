#!/usr/bin/env bash
# Build an ISOLATED gateway profile (memchat) to chat-test live memory governance.
# Copies your real ~/.flowly/config.json (provider keys + channels) into the
# profile, points workspace inside the profile, and enables memory_dreaming.
# Your real workspace/memory is never touched. Wipe with: scripts/memlab.sh reset
# (after FLOWLY_MEMLAB_PROFILE=memchat) or: rm -rf ~/.flowly/profiles/memchat
set -euo pipefail
PROFILE="${1:-memchat}"
uv run --extra dev python - "$PROFILE" <<'PY'
import json, sys, pathlib
prof_name = sys.argv[1]
home = pathlib.Path.home() / ".flowly"
real = home / "config.json"
prof = home / "profiles" / prof_name
prof.mkdir(parents=True, exist_ok=True)
data = json.loads(real.read_text()) if real.exists() else {}
ad = data.setdefault("agents", {}).setdefault("defaults", {})
ad["workspace"] = str(prof / "workspace")
ad.setdefault("memoryDreaming", {})["enabled"] = True
(prof / "config.json").write_text(json.dumps(data, indent=2))
(prof / "workspace" / "memory").mkdir(parents=True, exist_ok=True)
print(f"profile ready: {prof}")
print(f"  workspace : {ad['workspace']}")
print(f"  memoryDreaming.enabled = True")
print(f"  copied provider/channels from {real if real.exists() else '(none — real config missing)'}")
PY
echo
echo "Next:"
echo "  1) stop your normal gateway (so channels don't double-bind)"
echo "  2) FLOWLY_PROFILE=$PROFILE uv run --extra dev flowly gateway"
echo "  3) in chat: 'hafızana ekle: koyu tema severim'  /  'e-postam demo@nocetic.com'"
echo "  4) FLOWLY_PROFILE=$PROFILE uv run --extra dev flowly memory list"
echo "  5) cat ~/.flowly/profiles/$PROFILE/workspace/memory/MEMORY.md"
