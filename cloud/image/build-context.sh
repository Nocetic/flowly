#!/usr/bin/env bash
# Prepare the Docker build context for the Flowly cloud machine image.
#
#   cloud/image/build-context.sh <out-dir> [--ref <tag or commit>]
#
# With --ref (a release: `--ref v3.4.0`) the context holds that revision's
# tracked sources (`git archive`), the same source the runtime Desktop ships
# is built from; tag the image with its version. Without it, the working
# tree's tracked files (local uncommitted edits to tracked files included,
# untracked files not), for trials. The image files (Dockerfile, init.sh, …)
# always come from this checkout. build-info.json records the commit and
# Core's version.
#
# The image installs Core from these sources into a virtualenv with
# `uv sync --frozen` (the same uv.lock as the runtime) rather than unpacking
# the runtime archive: the screen's optional Python packages are installed
# into the agent's virtualenv (flowly/desktop_runtime/python_installer.py).
set -euo pipefail

out=${1:?usage: build-context.sh <out-dir> [--ref <tag or commit>]}
shift
ref=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) ref=${2:?--ref needs a tag or commit}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
paths=(flowly bridge workspace pyproject.toml uv.lock README.md PYPI_README.md LICENSE NOTICE)

rm -rf "$out"
mkdir -p "$out/src"
if [ -n "$ref" ]; then
  commit=$(git -C "$repo" rev-parse --verify "$ref^{commit}")
  git -C "$repo" archive --format=tar "$commit" -- "${paths[@]}" | tar -xf - -C "$out/src"
  dirty=false
else
  (cd "$repo" && git ls-files -z -- "${paths[@]}" | tar --null -T - -cf -) | tar -xf - -C "$out/src"
  commit=$(git -C "$repo" rev-parse HEAD)
  dirty=false
  git -C "$repo" diff --quiet HEAD -- "${paths[@]}" cloud/image || dirty=true
fi
cp "$here/Dockerfile" "$here/init.sh" "$here/flowly-cloud-setup" "$here/flowly_cloud_config.py" "$out/"
cp -R "$here/systemd" "$out/systemd"

version=$(sed -nE 's/^version = "([^"]+)"/\1/p' "$out/src/pyproject.toml" | head -1)
printf '{"commit":"%s","version":"%s","ref":"%s","dirty":%s,"builtAt":"%s"}\n' \
  "$commit" "$version" "${ref:-working tree}" "$dirty" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$out/build-info.json"
echo "$out (Core $version, commit ${commit:0:12}, ${ref:-working tree}, dirty=$dirty)"
