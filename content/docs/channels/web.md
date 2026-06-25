---
title: Web (Flowly Cloud relay)
eyebrow: Channels
description: Reach your agent from a browser or mobile client through the Flowly Cloud relay. The self-hosted gateway dials out to the relay over a WebSocket — there is no inbound port, SSH tunnel, or public IP to expose.
---

## How it works

The web channel makes an outbound WebSocket connection to a relay proxy. Your browser/iOS client connects to the same relay separately, and the relay forwards messages between them. The gateway authenticates to the relay with a short-lived HS256 JWT it signs locally.

> [!NOTE]
> Without a relay (empty `relayUrl`/`serverId`), the channel cannot connect and stays dormant — these values come from Flowly Cloud pairing.

## Configuration

Set under `channels.web` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "web": {
      "enabled": true,
      "relayUrl": "wss://relay.example.com/relay",
      "serverId": "...",
      "authToken": "...",
      "jwtSecret": "..."
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the web channel at gateway boot. |
| `relayUrl` | string | `""` | Relay WebSocket URL. Set by Flowly Cloud pairing; leave empty for self-host with no relay. |
| `serverId` | string | `""` | Flowly server ID. |
| `authToken` | string | `""` | Gateway auth token (`gatewayAuthToken`). |
| `jwtSecret` | string | `""` | Secret used to sign the relay-auth JWT. |

## Connect method and auth

On connect, the channel builds an HS256 agent JWT (claims include `type:"agent"`, `serverId`, `gatewayAuthToken`, `iat`, a 24-hour `exp`, plus issuer/audience) and dials `{relayUrl}?token={jwt}`.

JWT-signing-secret resolution order:

1. Env `MOLTBOT_PROXY_JWT_SECRET` (unless it is the placeholder default)
2. `jwtSecret` from config
3. `authToken` from config (used as the signing secret if no `jwtSecret`/env is set)

`serverId` falls back to the env var `FLOWLY_SERVER_ID` if not set in config. `wss://` connections use a `certifi` TLS context.

Once connected, the relay forwards `ready`, `browser-connected`/`disconnected`, `rpc`, and `ping` frames. The RPC surface includes `chat.send`/`chat.abort` with per-run task tracking and cooperative abort. Exec-approval and compaction events are pushed to relay sessions, and the gateway uses the web channel's session for bot-created web crons.

## Populating the config

These fields are normally filled in for you by Flowly Cloud:

```bash
flowly login
```

`flowly login` pairs your gateway with Flowly Cloud and populates `relayUrl`, `serverId`, `authToken`, and `jwtSecret`. After that, set `channels.web.enabled` to `true` and start the gateway.

## Setup steps

1. Run `flowly login` to pair with Flowly Cloud (populates the web config keys).
2. Set `channels.web.enabled` to `true`.
3. Start the gateway: `flowly gateway`.
4. Open your Flowly web/mobile client; it connects through the relay.

> [!NOTE]
> Self-hosting without Flowly Cloud leaves `relayUrl`/`serverId` empty, so the web channel will not connect.

## Related

- [Channels overview](./overview.md)
- [Teams](./teams.md)
- [WhatsApp](./whatsapp.md)
- [Service](../using-flowly/service.md)
- [Cron](../features/cron.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Slash commands](../reference/slash-commands.md)
