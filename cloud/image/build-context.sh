#!/usr/bin/env bash
# Prepare the Docker build context for the Flowly cloud machine image.
#
#   cloud/image/build-context.sh <out-dir>
#
# The context holds this checkout's tracked sources (working-tree contents, so
# local uncommitted edits to tracked files are included and untracked files are
# not) plus the image files. build-info.json records the commit.
set -euo pipefail

out=${1:?usage: build-context.sh <out-dir>}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)

rm -rf "$out"
mkdir -p "$out/src"
(cd "$repo" && git ls-files -z -- flowly bridge workspace pyproject.toml uv.lock README.md PYPI_README.md LICENSE NOTICE \
  | tar --null -T - -cf -) | tar -xf - -C "$out/src"
cp "$here/Dockerfile" "$here/init.sh" "$here/flowly-cloud-setup" "$here/flowly_cloud_config.py" "$out/"
cp -R "$here/systemd" "$out/systemd"

commit=$(git -C "$repo" rev-parse HEAD)
dirty=false
git -C "$repo" diff --quiet HEAD -- flowly bridge workspace pyproject.toml uv.lock cloud/image || dirty=true
printf '{"commit":"%s","dirty":%s,"builtAt":"%s"}\n' "$commit" "$dirty" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$out/build-info.json"
echo "$out (commit ${commit:0:12}, dirty=$dirty)"
