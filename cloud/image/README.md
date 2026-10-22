# Flowly cloud machine image

One machine per user on Fly Machines (docs/cloud/DECISIONS.md D20), suspended when
idle (D22). The image runs this repository's Core (not GitHub main, not the public
installer), its screen with Google Chrome (D17), and nothing user-specific: every
user setting arrives at boot from the machine's env and Fly secrets.

Machines are created and driven by `server/src/machines/fly.ts`
(`provisionCloudMachine`, `FlyMachineProvider`).

## Files

| File | Role |
|---|---|
| `Dockerfile` | Ubuntu 24.04, systemd, Core from `src/` (locked deps via `uv sync --frozen`), `flowly screen install` baked in |
| `init.sh` | PID 1 under Fly's `/fly/init`: secrets to root-only env files, time zone, pure cgroup2, `/data` bind mounts, then systemd in its own PID namespace |
| `flowly-cloud-setup` + `systemd/flowly-cloud-setup.service` | Boot step: `flowly screen restore-accounts`, then `flowly_cloud_config.py` |
| `flowly_cloud_config.py` | Writes the machine-owned fields of `~/.flowly/config.json` |
| `systemd/ai.flowly.gateway.service` | The gateway, as a root **user** unit (same label as `flowly service install`), so Core's own restart path (`systemctl --user restart ai.flowly.gateway`, used by `config.set` with restart) works |
| `systemd/user-0-flowly.conf` | Orders root's user manager after the boot step |
| `systemd/flowly-edge.service` | `socat` 8080 → 127.0.0.1:18790 |
| `build-context.sh` | Builds a clean Docker context from tracked files |
| `fly.toml.example` | For building/pushing with `fly deploy --build-only` and manual trials |

## How it boots

1. Fly's `/fly/init` runs `/init.sh` as root with the machine env and the app's secrets.
2. `init.sh` copies only the names below into `/etc/flowly/cloud/setup.env` and
   `gateway.env` (mode 600, root filesystem, never the volume), refusing values with
   characters outside `[A-Za-z0-9._~:/+=@-]`. Links `/etc/localtime` to `FLOWLY_TZ`.
   Replaces Fly's hybrid cgroups with cgroup2. Seeds `/data/root` and
   `/data/screen-runtime` from the image on a fresh volume, then bind-mounts them
   over `/root` and `/var/lib/flowly-screen-runtime`. Execs systemd (PID 1 in a new
   PID namespace).
3. `flowly-cloud-setup.service`:
   - `flowly screen restore-accounts` recreates the shared `flowly-screen` account and
     each per-profile `flowly-s-…` account from the receipts on the volume with the
     same UID/GID (the root filesystem, and `/etc/passwd` with it, is reset on every
     redeploy or cold start). The image itself ships with no screen account.
   - `flowly_cloud_config.py`: gateway on `127.0.0.1:18790` with `requireAuth` and its
     token, the Flowly provider with the account key, the Flowly server channel
     enabled when an agent secret is present; `flowly bootstrap` for the workspace.
4. Root's user manager (lingering) starts `ai.flowly.gateway`. `flowly-edge` relays
   Fly's TLS edge (`<app>.fly.dev`, port 443 → 8080) to the loopback gateway. The relay
   keeps screen sockets loopback (the gateway refuses plain non-loopback screen
   sockets), and `gateway.requireAuth` keeps the token required although every
   proxied client arrives from 127.0.0.1.

A suspend keeps everything in memory (resume ≈ 1.3 s through the API, screen
included). A stop or an image update resets the root filesystem; step 3 restores it.

## Environment and secrets

Set by `provisionCloudMachine`. Secrets are Fly app secrets; never bake them into the image.

| Name | Kind | Used for |
|---|---|---|
| `FLOWLY_TZ` | machine env | IANA zone of the user (D17), e.g. `Europe/Istanbul`. Default `Etc/UTC`. |
| `FLOWLY_ACCOUNT_KEY` | secret | Flowly account key (`flw_…`) → `providers.flowly.accountKey`. Model access through the Flowly proxy without `flowly login`. The gateway does not start without some provider. |
| `FLOWLY_GATEWAY_TOKEN` | secret | `gateway.token`, required by every REST/WS call through the edge. Generated once if absent. |
| `FLOWLY_AGENT_SECRET` | secret | PROTOCOL.md B1 `agentSecret`. Present → `channels.flowlyServer.enabled = true`. Read by Core from the gateway env (not written to disk). The server stores only `agentSecretHash` = lowercase hex SHA-256. |
| `FLOWLY_AGENT_HOST_ID` | secret | PROTOCOL.md B1 `agentHostId` (= the server doc id; the doc also needs `serverId` equal to it). |
| `FLOWLY_SERVER_URL` | machine env | Optional; Core defaults to `wss://cloud.useflowlyapp.com/v1/agent`. |

The Flowly server channel (`channels.flowlyServer.{enabled, serverUrl, agentSecret,
agentHostId}`) comes from the core-server-channel branch; with a Core that lacks it,
the boot step logs that the agent will not dial the server and continues.
`FLOWLY_AGENT_SECRET` is removed from the agent's subprocess environments by
`flowly/exec/env_scrub.py` on that branch.

## Build and publish

```sh
CTX=$(mktemp -d)/ctx
cloud/image/build-context.sh "$CTX"
cp cloud/image/fly.toml.example "$CTX/fly.toml"    # set app = the image's registry app
(cd "$CTX" && fly deploy --build-only --push --remote-only --image-label <tag>)
# → registry.fly.io/<app>:<tag>; machines of other apps in the org can use it.
```

Use a long-lived org token (`fly tokens create org`) as `FLY_API_TOKEN` for the
machine manager. `fly auth token` returns a user session token that flyctl rotates.

## Sizing (measured 2026-10-22, shared-cpu-2x, 2 GB, fra)

| State | Used | Available |
|---|---|---|
| Booted, gateway idle (gateway RSS 140 MB) | 341 MB | 1626 MB |
| Screen started, one blank tab | 700 MB | 1268 MB |
| Screen with X, YouTube (playing), Google Maps, NYTimes, Reddit | 1041 MB | 927 MB |

A screen therefore costs ~360 MB idle and ~700 MB with five heavy sites. The screen's
admission floor (`flowly/desktop_runtime/resources.py` `MIN_MEMORY`) is 1 GiB, so a
2 GB machine runs Core and one screen; 1.5 GiB refused it once the gateway warmed up.
Suspend: ~3 s to `suspended`; resume: ~1.3 s to `started` with the same screen.
