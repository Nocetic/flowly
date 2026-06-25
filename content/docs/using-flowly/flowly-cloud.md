---
title: Flowly Cloud & Account
eyebrow: Using Flowly
description: The optional hosted account, relay, and apps — and exactly what it adds on top of the fully self-hostable OSS core.
---

Flowly Cloud is an **optional** hosted account that layers cross-device apps, sync, and a managed relay on top of the open-source agent. None of it is required to use Flowly.

> [!NOTE]
> Everything in the Flowly repo works **without** a Flowly account. Bring your own LLM key (BYOK), point it at a provider, and run the gateway on your own machine. Signing in only adds the hosted features described below — it never gates the core agent, tools, channels, or memory.

## Self-host vs Cloud

Flowly's agent core is Apache 2.0. You can run it on a laptop, a VPS, or a Mac mini with your own keys and your own data — no account, no relay, no sign-in.

| | Self-host (OSS, BYOK) | Flowly Cloud (optional) |
| --- | --- | --- |
| LLM provider | Your own key (OpenRouter, Anthropic, OpenAI, Gemini, Groq, xAI, Zhipu, vLLM, …) | Hosted `flowly` provider — no API key required |
| Reach | Terminal, Telegram, Discord, Slack, Teams, email, voice | All of those **plus** browser extension and Mac / iOS / Android apps |
| Cross-device sync | — | Conversations sync across your devices |
| Relay | — | Managed relay so your bot stays reachable when your laptop sleeps |
| Account | None needed | Flowly account (OAuth sign-in) |

The hosted `flowly` provider is just another entry in the provider list — see [Providers & models](/docs/using-flowly/providers-and-models). When you sign in, Flowly can set it as your default LLM so you can start chatting with zero key configuration.

> [!IMPORTANT]
> Cloud is additive. Signing out (or never signing in) leaves the OSS core fully functional with your BYOK setup. The repo is the same code that ships inside the Flowly Desktop app — there is no separate "lite" build.

## Signing in — `flowly login`

```bash
flowly login
```

On a fresh machine this runs a device-code OAuth flow:

1. Flowly requests a device code from the backend and prints a one-click authorization URL (with the code and your device name pre-filled).
2. Your browser opens that URL. Once you approve, the CLI polls until the backend reports `authorized` (the device code expires after a few minutes).
3. Flowly exchanges the result for account tokens and saves them locally.
4. It then **registers this machine** with the backend and **wires the relay credentials** into your gateway config (see below).

If you are already signed in and everything is healthy, `flowly login` is a no-op that prints your account email. If your tokens are present but the relay config is incomplete (e.g. someone edited `config.json`), it does **not** silently change anything — it tells you to run `flowly login --repair`.

### Flags

| Flag | What it does |
| --- | --- |
| `--no-browser` | Prints the authorization URL instead of trying to open a browser automatically. Useful over SSH or on headless machines. |
| `--repair` | Re-registers the machine and re-writes the relay config using the tokens already on disk — no browser, no OAuth. Use it when sign-in succeeded but the relay wiring is broken. Exits non-zero if the saved token can't be refreshed (run a full `flowly login` to recover). |
| `--repair --dry-run` | Prints exactly what `--repair` would change without writing anything to config, keychain, or the backend. |

> [!NOTE]
> Sign-in tokens are stored via your OS keychain when available, falling back to a `0600` file at `~/.flowly/credentials/account.json`. The id token is refreshed automatically before it expires.

## What signing in registers — your machine

Flowly identifies your machine with a stable `machineId`. It reuses the same identifier the Flowly Desktop app writes (a UUID at the desktop app's data path), so the same physical machine de-duplicates to a single server entry across Desktop and CLI installs. If that path isn't writable, it falls back to a hash of the hardware UUID.

On login, Flowly calls the backend to **get-or-create a server** for this machine:

- The call is idempotent — the same `machineId` always maps to the same `serverId`. Logging in repeatedly never creates duplicate servers.
- The backend returns a `serverId` and a `gatewayAuthToken` (and a JWT secret). These are the credentials your gateway uses to reach the relay.

These are then written into your gateway config under `channels.web`.

## What Cloud adds

Grounded in the README and the bot code, signing in adds:

- **Apps & extension** — a browser extension and Mac / iOS / Android apps reach your agent through the relay.
- **Cross-device sync** — conversations flow through the relay into the hosted store, so the same chat shows up on every signed-in device.
- **Hosted LLM** — the `flowly` provider lets you run without supplying your own API key.
- **Managed relay** — your self-hosted gateway dials *out* to a relay, so clients can reach it without exposing an inbound port, SSH tunnel, or public IP — and your bot stays reachable when your laptop is asleep (when the gateway runs as a [background service](/docs/using-flowly/service)).

> [!NOTE]
> Pricing and plan tiers are not part of the open-source repo and are not documented here. See [useflowlyapp.com](https://useflowlyapp.com) for current Cloud offerings.

## How the relay works

The relay is an outbound WebSocket. Your gateway connects *to* the relay (the way a Telegram bot polls Telegram), so there's nothing inbound to open. Clients (browser, desktop, iOS) connect to the relay and their messages are routed through your gateway's connection.

After `flowly login`, your config's web channel is wired:

```json
{
  "channels": {
    "web": {
      "enabled": true,
      "serverId": "<from registration>",
      "authToken": "<gatewayAuthToken>",
      "jwtSecret": "<signing secret>",
      "relayUrl": "wss://relay.useflowlyapp.com/relay"
    }
  }
}
```

On the next gateway start, the web channel:

1. Reads `serverId`, `authToken` (the `gatewayAuthToken`), `jwtSecret`, and `relayUrl` from config.
2. Builds a short-lived agent JWT (HS256) with claims `type: "agent"`, `serverId`, `gatewayAuthToken`, `iss: "flowly"`, `aud: "moltbot-proxy"`, valid for 24 hours.
3. Connects outbound to the relay URL with that token and reconnects automatically with backoff if dropped.
4. Receives chat messages forwarded from clients, runs the agent, and streams responses back through the same connection. Outbound replies that can't be sent (transient drops) are queued and replayed on reconnect.

This is the mechanism behind iOS / desktop / browser access — clients talk to the relay, the relay routes to your gateway over this connection. For the channel-level detail, see [Web (Flowly Cloud relay)](/docs/channels/web).

> [!IMPORTANT]
> The wiring is idempotent — writing the same credentials twice is a no-op. If the credentials change while a gateway is already running, restart the gateway so it picks them up.

> [!NOTE]
> A self-hosted relay or regional endpoint is supported: if you set a custom `relayUrl` yourself, Flowly preserves it across logins and only backfills the default when the field is empty.

## How a message flows

```
Your iOS / desktop / browser client
        │  (Firebase JWT)
        ▼
   Flowly relay  (wss://relay.useflowlyapp.com/relay)
        │  (routed over your gateway's outbound WebSocket)
        ▼
   Your self-hosted Flowly gateway
        │
        ▼
   Agent loop → your LLM provider (hosted flowly or BYOK)
        │
        ▼  (response streams back the same path)
   Relay → client
```

## Signing out — `flowly logout`

```bash
flowly logout
```

Logout performs three clean-ups and prints what it cleared:

- **Keychain tokens** — removes the account tokens (id token, refresh token, `gatewayAuthToken`) from the keychain and any legacy file.
- **`channels.web` relay config** — sets `enabled` to `false` and clears `serverId`, `authToken`, and `jwtSecret`. This disables iOS / desktop relay access; without it the gateway would keep dialing the relay with revoked credentials.
- **`providers.active`** — cleared **only if** it currently points at `flowly`. Your BYOK provider keys are preserved, and the provider cascade resumes. (If your default was a BYOK provider, this is left untouched.)

`flowly logout` is idempotent — if you're not signed in it just says so.

> [!IMPORTANT]
> After logging out, restart a running `flowly gateway` so it stops trying to authenticate to the relay with the now-revoked credentials.

## Relationship to pairing

Signing in **pairs this machine** for app access: registration binds the machine to a `serverId`, and the wired relay credentials are what let your iOS / desktop / browser clients reach the gateway. This is distinct from per-channel pairing for Telegram / WhatsApp / Discord / Slack (the `flowly pairing` commands), which authorizes individual chat users on a messaging channel rather than a device for relay access.

## Privacy & data

- **Without an account**, nothing leaves your machine except the LLM API calls you make with your own key, to your chosen provider.
- **With Flowly Cloud**, conversations routed through the relay flow to the hosted store to enable cross-device sync. Account tokens, relay tokens, and gateway tokens are credential material — keep them private. Use them via official Flowly clients (the [Acceptable Use Policy](https://useflowlyapp.com/terms#acceptable-use) governs Cloud credentials).
- Self-hosted use with your own LLM keys is unrestricted.

## Related

- [Web (Flowly Cloud relay)](/docs/channels/web) — the channel-level detail of the relay WebSocket.
- [Providers & models](/docs/using-flowly/providers-and-models) — the hosted `flowly` provider and BYOK alternatives.
