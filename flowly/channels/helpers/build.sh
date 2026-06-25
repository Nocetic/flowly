#!/usr/bin/env bash
# Build + code-sign the Flowly iMessage send helper as a .app bundle.
#
# Packaging it as a registered LaunchServices .app (rather than a bare
# Mach-O) is what lets macOS attribute Apple Events to "Flowly iMessage
# Helper" and surface the Automation consent prompt — a bare binary is
# silently denied (-1743) because TCC has no user-facing app to name.
# The helper additionally re-execs itself with responsibility disclaim,
# so even when the gateway launches the inner binary directly it becomes
# its OWN TCC responsible process instead of inheriting the terminal's
# unreliable identity.
#
# Output: ./Flowly iMessage Helper.app  (next to this script, where the
# Python channel looks for it). In packaging, sign with a real Developer
# ID (`CODESIGN_IDENTITY=...`) so the grant survives updates; ad-hoc
# (`-`) is the local-dev default but its cdhash changes per build, which
# revokes the Automation grant on every rebuild.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app="$here/Flowly iMessage Helper.app"
macos_dir="$app/Contents/MacOS"
bin="$macos_dir/flowly-imessage-helper"
identity="${CODESIGN_IDENTITY:--}"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "iMessage helper only builds on macOS" >&2
    exit 0
fi

rm -rf "$app"
mkdir -p "$macos_dir"
cp "$here/Info.plist" "$app/Contents/Info.plist"

swiftc -O "$here/imessage_send.swift" -o "$bin"

codesign --force \
    --sign "$identity" \
    --identifier ai.flowly.imessage-helper \
    --options runtime \
    "$app"

# Register with LaunchServices so TCC can resolve the bundle id → app
# name and prompt. (-f forces a fresh registration of this path.)
lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[[ -x "$lsregister" ]] && "$lsregister" -f "$app" || true

echo "built + signed: $app"
