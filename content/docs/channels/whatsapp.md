---
title: WhatsApp
eyebrow: Channels
description: Connect your agent to WhatsApp through a local Node.js bridge that speaks the WhatsApp Web protocol (`@whiskeysockets/baileys`). The Python adapter talks to the bridge over WebSocket; authentication is a QR-code scan handled by the bridge.
---

## Requirements

- **Node.js ≥ 18** (the bridge is built and run from the CLI).
- A phone with WhatsApp to scan the QR code.

## Configuration

Set under `channels.whatsapp` in `~/.flowly/config.json`:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "bridgeUrl": "ws://localhost:3001",
      "allowFrom": []
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the WhatsApp adapter at gateway boot. |
| `bridgeUrl` | string | `ws://localhost:3001` | WebSocket URL of the Node bridge. |
| `allowFrom` | string[] | `[]` | Allowed phone numbers. Empty = allow all. |

> [!NOTE]
> There is no token in config — WhatsApp auth is a QR-code scan performed by the bridge.

## Connect method

The adapter connects to the bridge at `bridgeUrl` and exchanges JSON frames: inbound `message` frames are turned into agent messages, and it also handles `status`, `qr`, and `error` frames. Outbound replies are sent as `{"type":"send","to":<chat_id>,"text":<content>}`. The adapter reconnects on drop with a 5-second backoff. Access is enforced via `allowFrom`; the WhatsApp adapter does **not** use the pairing store.

## Bridge setup and login

The bridge is provisioned, built, and run by the CLI:

```bash
flowly channels login
flowly channels status
```

`flowly channels login` builds the bridge if needed (`npm install && npm run build`) and runs it, printing a QR code to scan with WhatsApp on your phone. The bridge lives at `~/.flowly/bridge`.

> [!NOTE]
> `flowly channels status` shows WhatsApp's enabled state and bridge URL. `channels status` currently reports only WhatsApp.

## Setup steps

1. Ensure Node.js ≥ 18 is installed.
2. Set `channels.whatsapp.enabled` to `true`.
3. Run `flowly channels login` and scan the QR code with WhatsApp.
4. Optionally restrict access via `allowFrom`.
5. Start the gateway: `flowly gateway` (the bridge must be running for messages to flow).

## Related

- [Channels overview](./overview.md)
- [Telegram](./telegram.md)
- [Web](./web.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [Service](../using-flowly/service.md)
- [Slash commands](../reference/slash-commands.md)
