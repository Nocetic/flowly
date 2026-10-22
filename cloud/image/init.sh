#!/bin/sh
# PID 1 of the Flowly cloud machine, started by Fly's /fly/init.
#
# 1. Hand the machine's settings and secrets (Fly env and secrets) to systemd's
#    services through root-only files on the root filesystem, never the volume.
# 2. Apply the user's time zone (D17) before anything starts.
# 3. Give systemd a pure cgroup v2 tree and its own PID namespace (Fly's
#    /fly/init keeps a hybrid layout and stays PID 1 outside).
# 4. Bind the volume over the state that must outlive a stop or a redeploy
#    (the root filesystem is reset then; a suspend keeps everything).
set -eu

umask 077
mkdir -p /etc/flowly/cloud
chmod 700 /etc/flowly/cloud

# Only these names leave init's environment, each value from a safe charset so
# an environment file can never be bent into another assignment.
write_env() {
  target=$1; shift
  : > "$target.tmp"
  for name in "$@"; do
    value=$(printenv "$name" 2>/dev/null || true)
    [ -n "$value" ] || continue
    case "$value" in
      *[!A-Za-z0-9._~:/+=@-]*) echo "flowly-init: ignoring $name: unexpected characters" >&2; continue ;;
    esac
    printf '%s=%s\n' "$name" "$value" >> "$target.tmp"
  done
  mv "$target.tmp" "$target"
}
# Read once at boot by flowly-cloud-setup, which writes Core's config.
write_env /etc/flowly/cloud/setup.env FLOWLY_ACCOUNT_KEY FLOWLY_GATEWAY_TOKEN FLOWLY_TZ \
  FLOWLY_SERVER_URL FLOWLY_AGENT_SECRET FLOWLY_AGENT_HOST_ID
# The gateway's own environment: Core reads the Flowly server channel's
# connection from these when its config fields are empty, and FLY_APP_NAME
# names this machine's public screen address (screen tickets and
# display.observe's streamUrl, server PROTOCOL.md C). Without it every
# screen ticket failed with SCREEN_TICKET_UNAVAILABLE.
write_env /etc/flowly/cloud/gateway.env FLOWLY_SERVER_URL FLOWLY_AGENT_SECRET FLOWLY_AGENT_HOST_ID FLY_APP_NAME

zone=${FLOWLY_TZ:-Etc/UTC}
case "$zone" in
  *..*|/*|*[!A-Za-z0-9_+/-]*) echo "flowly-init: invalid FLOWLY_TZ, using Etc/UTC" >&2; zone=Etc/UTC ;;
esac
if [ -f "/usr/share/zoneinfo/$zone" ]; then
  ln -sf "/usr/share/zoneinfo/$zone" /etc/localtime
  echo "$zone" > /etc/timezone
else
  echo "flowly-init: unknown time zone $zone, keeping $(cat /etc/timezone)" >&2
fi

for d in /sys/fs/cgroup/*; do
  [ "$d" = /sys/fs/cgroup/unified ] && continue
  if mountpoint -q "$d"; then umount "$d" || echo "flowly-init: could not unmount $d" >&2; fi
done
umount /sys/fs/cgroup/unified 2>/dev/null || true
umount /sys/fs/cgroup 2>/dev/null || true
mount -t cgroup2 -o nsdelegate cgroup2 /sys/fs/cgroup

if ! mountpoint -q /data; then
  echo "flowly-init: /data is not a volume; state will not survive a stop" >&2
fi
umask 022
for pair in "root:/root" "screen-runtime:/var/lib/flowly-screen-runtime"; do
  name=${pair%%:*}; target=${pair#*:}
  if [ ! -d "/data/$name" ]; then
    # First boot of this volume: seed it from the image, then switch atomically.
    rm -rf "/data/.$name.seed"
    mkdir -p "/data/.$name.seed"
    cp -a "$target/." "/data/.$name.seed/"
    chmod --reference="$target" "/data/.$name.seed"
    mv "/data/.$name.seed" "/data/$name"
  fi
  mkdir -p "$target"
  mount --bind "/data/$name" "$target"
done

exec unshare --pid --fork --mount-proc /lib/systemd/systemd --unit=multi-user.target
