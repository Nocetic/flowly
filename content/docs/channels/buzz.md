---
title: Buzz
eyebrow: Channels
description: Connect your agent to a Buzz community with a Nostr identity, automatic joined-channel discovery, and default-deny member access.
---

Buzz connects a Flowly agent to the channels and direct-message conversations available to a Nostr identity on a Buzz community relay. The adapter receives live events over an authenticated Nostr WebSocket when possible and uses the official `buzz` CLI for identity discovery, channel discovery, message history, sending, and polling fallback.

## What the integration supports

- Automatic discovery of the identity and its joined Buzz channels.
- Watching every channel joined at adapter startup, or an explicit subset.
- Direct messages discovered from the identity's Buzz conversations.
- Channel access rules based on Nostr public keys.
- Mention-only or open channel behavior.
- Replies, outbound local image attachments, remote-media links, and best-effort read reactions.
- Authenticated Nostr WebSocket delivery with CLI polling fallback and shared de-duplication.
- An adapter-level home-channel fallback for outbound Buzz events that do not carry a conversation ID.

## Requirements

- A Buzz community relay URL.
- A Nostr private key (`nsec1…` or 64-character hex) for an identity that is already a community member.
- At least one joined group channel. The adapter establishes its watched group set before it discovers DMs.
- The official `buzz` CLI installed on the **machine running the Flowly gateway**.

### Install the Buzz CLI

The upstream CLI is a Rust crate in the
[Block Buzz repository](https://github.com/block/buzz/tree/main/crates/buzz-cli).
With a current Rust/Cargo toolchain installed, build and install it from a Buzz
checkout:

```bash
git clone https://github.com/block/buzz.git
cd buzz
cargo install --path crates/buzz-cli
```

Cargo normally installs the binary at `~/.cargo/bin/buzz`. Ensure that
directory is on the gateway process's `PATH`, or set an absolute `cliPath`.
Flowly does not install or update this external binary itself.

Confirm that the gateway's user account can find the executable:

```bash
command -v buzz
buzz --help
```

If `command -v` cannot find it, either install the binary on the service user's
`PATH`, set `channels.buzz.cliPath`, set `BUZZ_CLI_PATH`, or place it at
`~/bin/buzz`.

Flowly expects a CLI version that supports the following command surface and returns machine-readable JSON:

| Operation | CLI command used by Flowly |
| --- | --- |
| Resolve the active identity | `buzz users get` |
| Resolve a sender profile | `buzz users get --pubkey <hex>` |
| Discover joined channels | `buzz channels list` |
| Discover direct messages | `buzz dms list` |
| Seed or poll messages | `buzz messages get --channel <id> --limit 50 [--since <unix-time>]` |
| Send text/files/replies | `buzz messages send --channel <id> --content - [--reply-to <event-id>] [--file <path>]` |
| Add the read reaction | `buzz reactions add --event <event-id> --emoji 👀` |

If an older CLI lacks one of these commands or changes its JSON fields, identity/channel testing or the affected runtime operation fails with a gateway-log entry.

The upstream contract writes JSON results to stdout, JSON errors to stderr,
and uses exit codes `0` (success), `1` (user input), `2` (network), `3`
(authentication), `4` (other), and `5` (write conflict). Flowly consumes the
machine-readable output. Connection-test errors are redacted against the
submitted private key; runtime failures include the structured CLI message and
exit code in gateway logs.

> [!IMPORTANT]
> The identity must already belong to the Buzz community. Flowly does not create an identity, join a community, or join channels on the owner's behalf.

### Do users need to operate the CLI?

No. The binary is a runtime dependency, not the normal setup interface. Flowly
invokes it on the gateway host.

- **Desktop:** connection testing discovers the identity and joined channels,
  displays channel names, and stores their IDs behind the UI.
- **Terminal setup:** leave **Watched channels** and **Home channel** empty to
  use all joined channels and an automatic fallback. No UUID lookup is needed.
- **Headless/manual config:** raw IDs are needed only when you deliberately pin
  an explicit subset or explicit home channel.

The `buzz users get` and `buzz channels list` commands remain useful for
diagnostics and manual deployments, but they are not required for the normal
Desktop or all-joined terminal path.

## Security model

Desktop and `flowly setup channels` store the private key as
`BUZZ_PRIVATE_KEY` in the active Flowly profile's owner-only `.env` file.
Headless deployments may use a supported credentials file instead. The key is
not written to `config.json`, included in discovery results, or returned to
Desktop after saving.

Flowly also keeps the key out of process arguments: it launches the CLI without a shell, passes message bodies through standard input, and supplies the key only in the child process environment. This prevents the key and message body from appearing in a normal process listing.

The optional owner-attestation tag is separate from the identity key.
`channels.buzz.authTag` is stored in owner-only `config.json`, masked when
returned to Desktop, and used for Flowly's NIP-42 WebSocket authentication.
`BUZZ_AUTH_TAG` in the profile `.env` overrides it for the WebSocket and is also
inherited by the official CLI. Use the environment form when the community
requires owner attestation for CLI/REST identity, discovery, or send calls.

Inbound access is **default-deny**. A new Buzz connection cannot invoke the agent until you do one of the following:

- add at least one sender's `npub` or 64-character hex public key to `allowFrom`; or
- explicitly set `allowAllUsers` to `true`.

This rule applies to both channel messages and direct messages. Buzz does not use Flowly's pairing-code store.

## Desktop setup

1. Open **Dashboard → Channels → Buzz**.
2. Enter the community relay URL and the identity's private key.
3. Select **Test connection & load channels**. Flowly verifies the identity and retrieves the joined-channel list from the agent host.
4. Keep **Automatically watch all joined channels** enabled, or turn it off and select an explicit subset.
5. Optionally choose a **Home channel** as the adapter fallback for an outbound event with no conversation ID.
6. Choose the access policy:
   - keep **Allow every community member** off and enter one or more allowed `npub` values; or
   - deliberately allow all community members.
7. Choose **Mention only** or **Open** channel behavior.
8. Expand **Advanced settings** only if you need a custom CLI path, credentials file, transport, poll interval, or NIP-42 owner-attestation tag.
9. Save. Desktop persists the settings and restarts the gateway.

Desktop calls the agent's read-only `connections.discover` RPC. The agent runs the identity and channel lookups on its own host, then returns channel names and IDs. Desktop displays names and keeps IDs in the background; users do not need to run `buzz channels list` or copy UUIDs.

When Desktop is connected to a remote Flowly host, the `buzz` executable and identity credentials must be available on that remote host—not on the computer running Desktop.

If a private key is already saved, Desktop shows a masked stored-secret state. Leaving the key field empty keeps the saved key; entering a new value replaces it.

## Terminal setup

Run:

```bash
flowly setup channels
```

Open the Buzz card and enter the relay URL and private key. Both channel fields are optional:

- Leave **Watched channels** empty to watch every channel the identity has joined when the adapter starts.
- Leave **Home channel** empty to let the adapter use the first watched or joined channel when an outbound event has no conversation ID.

Normal replies always return to the channel where the message arrived.

## Headless and manual setup

Write the identity key to the active profile's `.env`:

```dotenv
BUZZ_PRIVATE_KEY=<nsec-or-64-character-hex-private-key>
```

For the default profile this file is `~/.flowly/.env`. A named profile stores it under `~/.flowly/profiles/<profile>/.env`.

Then configure `channels.buzz` in the same profile's `config.json`:

```json
{
  "channels": {
    "buzz": {
      "enabled": true,
      "relayUrl": "https://community.communities.buzz.xyz",
      "channels": [],
      "homeChannel": "",
      "groupPolicy": "mention",
      "allowAllUsers": false,
      "allowFrom": ["npub1..."],
      "transport": "auto",
      "pollIntervalSeconds": 4,
      "cliPath": "",
      "credentialsFile": "",
      "authTag": ""
    }
  }
}
```

Restart the gateway after changing either file:

```bash
flowly restart
```

If no gateway is running yet, start one with `flowly gateway`; an installed service can also be restarted with `flowly service restart`. See [Service](../using-flowly/service.md).

## Identity and credential resolution

The adapter resolves the private key with these precedence rules:

1. Use `BUZZ_PRIVATE_KEY` from the process environment or active profile `.env` when present.
2. Otherwise, choose one explicit credentials path: `BUZZ_CREDENTIALS_FILE` takes precedence over `channels.buzz.credentialsFile`.
3. If no explicit path is configured, try `<FLOWLY_HOME>/credentials/buzz.json`, then files matching `*credentials*.json` under `~/.config/buzz/`.

An explicit credentials path shadows the automatic fallback locations. If that file is missing, malformed, or contains no supported key, Flowly reports the key as unavailable rather than silently selecting a different identity.

A credentials JSON file may contain any one of these keys:

```json
{
  "nsec": "nsec1..."
}
```

The accepted property names are `nsec`, `private_key_hex`, and `private_key`.

> [!TIP]
> Prefer the profile `.env` written by Desktop or `flowly setup channels`. Credential-file discovery exists for headless deployments and compatibility with an existing Buzz CLI setup.

The CLI path is resolved separately:

1. `BUZZ_CLI_PATH`, if set.
2. `channels.buzz.cliPath`, if set.
3. `buzz` on the gateway process's `PATH`.
4. `~/bin/buzz`.

An explicit path may include `~`. If an explicit path does not exist or a command name cannot be resolved, the adapter reports the CLI as unavailable instead of silently choosing a different binary.

## Channel selection and the home channel

`channels.buzz.channels` stores Buzz channel IDs, not display names:

- `[]` watches every channel the identity has joined when the adapter starts.
- A non-empty list watches only those channel IDs.

Flowly also discovers direct-message conversations for the identity. Direct messages are not listed in the `channels` setting and do not need to be selected in Desktop.

New direct-message conversations are refreshed while the adapter runs. After
joining a new group channel, reload Desktop discovery if you want to select it,
then restart the gateway. With `channels=[]`, a restart is enough to include the
newly joined group channel.

`homeChannel` is the Buzz adapter's fallback when an outbound Buzz event has no
conversation ID:

1. the configured `homeChannel`;
2. otherwise, the first explicitly watched channel;
3. otherwise, the first joined channel returned by Buzz.

Normal replies, Board results, and cron jobs created inside a Buzz conversation
retain that conversation ID and return there. They do not use `homeChannel`.
Callers that deliberately publish a Buzz outbound event without a conversation
ID use the fallback above.

## Access-control matrix

| `allowAllUsers` | `allowFrom` | Result |
| --- | --- | --- |
| `false` | `[]` | Nobody can invoke the agent. This is the secure default. |
| `false` | One or more valid public keys | Only those senders can invoke the agent. |
| `true` | Any value | Every community member can invoke the agent. |

`allowFrom` accepts Nostr `npub` values and 64-character hexadecimal public keys. Flowly normalizes both formats before comparing them; malformed entries are ignored.

Both sender authorization and channel mention policy must pass:

- **Direct messages:** an allowed sender can invoke the agent without a mention.
- **Channel + `groupPolicy="mention"`:** the sender must be allowed and address the identity by display name, `npub`, or hex public key.
- **Channel + `groupPolicy="open"`:** every message from an allowed sender can invoke the agent.

For mention-only messages, Flowly removes a leading identity mention before passing the text to the agent. In channel conversations it prefixes ordinary inbound text with the sender's display name so the agent can distinguish participants; slash commands keep their command form.

> [!WARNING]
> Pairing commands such as `flowly pairing approve` do not grant Buzz access. Set `allowFrom` or `allowAllUsers` in the Buzz connection itself.

## Transport modes

| Mode | Behavior | Recommended use |
| --- | --- | --- |
| `auto` | Tries authenticated Nostr WebSocket delivery. If connection or authentication fails, starts CLI polling and keeps retrying the WebSocket with backoff. Polling stops after WebSocket recovery. | Default for most installations. |
| `websocket` | Requires the authenticated WebSocket path. Does not fall back to polling. | Environments where fallback must be disabled or WebSocket behavior is being tested. |
| `poll` | Uses only the `buzz` CLI polling path. | Relays, proxies, or networks where WebSockets are intentionally unavailable. |

HTTP relay URLs are converted to their WebSocket equivalents (`http` → `ws`, `https` → `wss`) while preserving the path and query. Runtime configuration also accepts an explicit `ws://` or `wss://` URL.

WebSocket subscriptions cover Buzz message events and membership events for the active identity. NIP-42 challenges are signed with the identity key. If the relay requires owner attestation, provide `authTag` or the `BUZZ_AUTH_TAG` environment variable as a JSON-encoded four-string tag:

```json
["auth", "<64-character-owner-pubkey>", "", "<128-character-signature>"]
```

The first value must be `auth`; the public-key and signature fields must have the lengths shown. A malformed value fails WebSocket authentication rather than being sent as an arbitrary Nostr tag.

In `auto` mode, retry delay grows from one second to a maximum of 30 seconds. Polling uses `pollIntervalSeconds`, clamped to a minimum of one second, and periodically refreshes joined channels and direct-message conversations.

Both transports feed the same event-ID cache and per-conversation watermark. Switching between WebSocket and polling therefore does not intentionally deliver the same event twice.

### Runtime bounds and timeouts

| Operation | Bound |
| --- | --- |
| Desktop identity discovery | 6 seconds |
| Desktop joined-channel discovery | 8 seconds |
| Normal runtime CLI operation | 30 seconds |
| NIP-42 challenge/response receive | 20 seconds per awaited frame |
| Initial WebSocket readiness wait | 25 seconds |
| WebSocket frame size | 2,000,000 bytes maximum |
| Startup history | Latest 50 events per conversation |
| In-memory de-duplication cache | Latest 500 event IDs per conversation |
| Poll interval | Minimum 1 second |
| DM refresh in polling mode | Every fifth polling sweep |
| WebSocket reconnect backoff | 1 second, growing to 30 seconds |

The gateway-level CLI timeout is the outer bound even if the upstream CLI's
own HTTP timeout environment variables are configured to wait longer.

## Startup, history, and membership changes

At startup the adapter:

1. resolves the relay, CLI, and private key;
2. verifies the Buzz identity with a read-only identity lookup;
3. acquires an identity lock for the relay and public key;
4. loads joined channels and determines the watched set;
5. discovers direct-message conversations;
6. fetches up to the latest 50 messages per conversation to seed event IDs and timestamps; and
7. starts the selected inbound transport.

Seeding prevents old messages from being treated as new after a restart. The fetched startup history is **not** sent to the agent.

Direct-message discovery is refreshed from membership events and periodically
while polling. Group-channel membership is loaded at startup: restart after
joining a new group channel. An explicit `channels` list remains fixed until
you change it.

Only one Flowly process may use the same Buzz identity against the same relay at a time. The adapter writes this cross-profile lock under the canonical `~/.flowly/locks/buzz/` directory, even when a named profile or custom `FLOWLY_HOME` is active. A second profile or gateway using that relay/identity pair fails clearly instead of processing every message twice.

## Sending, replies, and media

Outbound Buzz messages use the official CLI:

- Text is passed through standard input.
- A reply carries the original Buzz event ID as `--reply-to`.
- Existing local file paths are repeated as `--file` attachments.
- HTTP(S) media URLs are appended to the message as links.
- An empty event with neither text nor a usable file is ignored.

The upstream CLI currently accepts JPEG, PNG, GIF, and WebP uploads up to 50
MB, plus MP4 up to 500 MB. The agent-facing `message` tool applies its own
stricter 10 MB validation before the adapter and does not currently expose MP4,
so normal tool-driven Buzz attachments are the shared image subset. Files such
as PDF, text, and ZIP pass the generic message-tool type check but are rejected
by Buzz's current upload allowlist; send those as external HTTPS links instead.

After accepting an inbound message, Flowly sends a best-effort `👀` reaction. A failure to add the reaction does not fail message processing.

## Configuration reference

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Start the Buzz adapter at gateway boot. |
| `relayUrl` | string | `""` | Buzz community relay URL. HTTP(S) is recommended; the upstream CLI also normalizes WS(S), and Flowly derives/uses WS(S) for live subscriptions. |
| `channels` | string[] | `[]` | Watched channel IDs. Empty discovers and watches every joined channel. |
| `homeChannel` | string | `""` | Adapter fallback for an outbound event without a conversation ID. Empty selects the first watched or joined channel. |
| `groupPolicy` | `"mention"` \| `"open"` | `"mention"` | Require the identity to be addressed in channels, or accept every allowed channel message. |
| `allowAllUsers` | bool | `false` | Allow every community member. |
| `allowFrom` | string[] | `[]` | Allowed Nostr `npub` or 64-character hex public keys. Empty denies everyone unless `allowAllUsers` is true. |
| `transport` | `"auto"` \| `"websocket"` \| `"poll"` | `"auto"` | Authenticated WebSocket with polling fallback, WebSocket only, or polling only. |
| `pollIntervalSeconds` | number | `4` | CLI polling interval; values below one second are clamped to one. |
| `cliPath` | string | `""` | Optional explicit CLI path or command. Empty searches `PATH`, then `~/bin/buzz`. |
| `credentialsFile` | string | `""` | Optional JSON fallback containing `nsec`, `private_key_hex`, or `private_key`. |
| `authTag` | string | `""` | Optional owner-attestation tag sent during NIP-42 authentication. |

## Environment overrides

Buzz-specific environment variables take precedence over the corresponding
`channels.buzz` values. Variables placed in the active profile `.env` are
loaded automatically. For string/list settings, a non-empty environment value
overrides config; use the setup UI or edit `config.json` to clear a saved value.
The two boolean policy overrides are interpreted whenever they are present.

| Variable | Overrides / purpose |
| --- | --- |
| `BUZZ_PRIVATE_KEY` | Identity private key. There is intentionally no matching `config.json` key. |
| `BUZZ_RELAY_URL` | `relayUrl` |
| `BUZZ_CHANNELS` | `channels`, as a comma-separated list of channel IDs |
| `BUZZ_HOME_CHANNEL` | `homeChannel` |
| `BUZZ_TRANSPORT` | `transport`: `auto`, `websocket`, or `poll` |
| `BUZZ_POLL_INTERVAL` | `pollIntervalSeconds` |
| `BUZZ_REQUIRE_MENTION` | Channel policy override: a truthy value selects `mention`; a false value selects `open` |
| `BUZZ_ALLOWED_USERS` | `allowFrom`, as a comma-separated list of `npub` or hex public keys |
| `BUZZ_ALLOW_ALL_USERS` | `allowAllUsers` |
| `BUZZ_CLI_PATH` | `cliPath` |
| `BUZZ_CREDENTIALS_FILE` | `credentialsFile` |
| `BUZZ_AUTH_TAG` | Overrides `authTag` for Flowly WebSocket auth and is inherited by CLI/REST calls |

Use environment overrides deliberately: they may make the running behavior differ from what Desktop displays from `config.json`. See [Environment variables](../reference/environment-variables.md).

## Desktop and gateway RPC contract

Buzz uses the same connection RPC over a direct gateway WebSocket and the
Flowly relay:

| Method | Buzz behavior |
| --- | --- |
| `connections.list` | Returns the Buzz card, field schema, saved non-secret values, masked password fields, live probe state, and `discoverable: true`. |
| `connections.discover` | Combines supplied unsaved fields with saved values, runs read-only identity/channel lookups on the agent host, and returns named choices. It does not write config or restart the gateway. |
| `connections.set` | Persists the selected settings and private-key environment value. Its restart-aware result lets the client restart the gateway after save. |

Desktop discovery sends snake-case integration field names:

```json
{
  "key": "buzz",
  "values": {
    "relay_url": "https://community.communities.buzz.xyz",
    "private_key": "nsec1...",
    "cli_path": ""
  }
}
```

A successful discovery result has this shape:

```json
{
  "ok": true,
  "status": "ok",
  "detail": "Connected as Flowly. Found 2 joined channels.",
  "identity": {
    "pubkey": "<64-character-hex-public-key>",
    "npub": "npub1...",
    "displayName": "Flowly"
  },
  "channels": [
    {
      "id": "<channel-uuid>",
      "name": "general",
      "description": "Community-wide discussion"
    }
  ]
}
```

Failure statuses are `not_configured`, `auth_failed`, or `down`, with a
user-facing `detail`; `identity` is `null` and `channels` is empty. The result
never contains the private key. Blank or masked password fields in a discovery
request keep the saved secret, and unknown incoming fields are ignored.

## Validate the connection

Run:

```bash
flowly doctor
```

When Buzz is enabled, Doctor checks for:

- a configured relay URL;
- a private-key value in one of the supported sources;
- a resolvable `buzz` executable; and
- an effective sender policy.

Doctor warns when `allowAllUsers` is false and `allowFrom` is empty because that configuration is valid but denies every inbound sender.

These are structural checks. Doctor does not prove community membership or
relay reachability; Desktop's connection test performs the live, read-only
identity and channel lookups.

For a live end-to-end check:

1. restart the gateway;
2. send a new message from an allowed identity;
3. mention the agent identity if `groupPolicy` is `mention`;
4. confirm the message receives the best-effort `👀` reaction and a reply.

`flowly channels status` currently reports WhatsApp only; use Desktop's connection test, `flowly doctor`, and gateway logs for Buzz.

## Troubleshooting

### `buzz` CLI not found

- Run `command -v buzz` as the same OS user that runs the gateway.
- Installed services may have a smaller `PATH` than an interactive shell.
- Set an absolute `cliPath` or `BUZZ_CLI_PATH`, or install the binary at `~/bin/buzz`.
- Restart the gateway after changing the path.

### Identity test or channel discovery fails

- Confirm the relay URL belongs to the intended Buzz community.
- Confirm the key is an `nsec` or 64-character hex private key.
- Confirm the identity is already a member of the community.
- Check that the CLI version is compatible with the community.
- If Desktop controls a remote agent, perform these checks on the remote host.

Connection testing is read-only: it runs identity and channel lookups and does not send a message or modify membership.

### The connection is healthy but the agent is silent

- Check the sender policy first. The default `allowAllUsers=false` plus `allowFrom=[]` denies everyone.
- Add the sender's `npub` or hex public key; pairing approval does not apply to Buzz.
- In a channel, mention the agent identity when `groupPolicy="mention"`.
- Confirm the channel is joined by the configured identity.
- If an explicit `channels` list is configured, confirm it contains the channel's ID.
- Send a **new** message after restart; startup history is intentionally seeded without replay.

### WebSocket fails or repeatedly reconnects

- Keep `transport="auto"` to retain CLI polling while WebSocket reconnects.
- Use `transport="poll"` when an upstream proxy or firewall intentionally blocks WebSockets.
- If `transport="websocket"` is forced, no polling fallback occurs.
- Confirm the relay accepts NIP-42 authentication for the configured identity.
- Configure `BUZZ_AUTH_TAG` in the active profile `.env` if CLI calls also
  require owner attestation. The `authTag` config field covers Flowly's
  WebSocket auth path.

### Messages appear twice

Flowly de-duplicates by Buzz event ID across both transports. Persistent duplicates usually indicate two gateway processes, profiles, or hosts using the same identity. Stop the extra process and inspect `~/.flowly/locks/buzz/`. Do not delete an active lock merely to bypass the single-owner guard.

### An outbound message without a target goes to the wrong channel

Set `homeChannel` to an explicit channel ID. When it is empty, selection falls back to the first watched or joined channel, whose order may not express operational intent.

### Private key changes do not take effect

- Confirm you edited the active profile's `.env`, not another profile.
- Check whether the gateway process already exports `BUZZ_PRIVATE_KEY`; process environment takes precedence over profile files.
- Restart the gateway after rotating the key.
- Re-run Desktop discovery or `flowly doctor`.

See [Troubleshooting](../using-flowly/troubleshooting.md) for log locations and general gateway checks.

## Current boundaries

- Flowly requires the external CLI even when WebSocket inbound delivery works;
  identity lookup, channel/DM discovery, startup history, sends, files, replies,
  and reactions still use it.
- Flowly does not create identities, join communities, join/leave channels, or
  manage Buzz membership.
- The adapter handles messages, replies, files/links, and its best-effort read
  reaction. Buzz workflows, canvas, message edits/deletes, forum voting, and
  repository operations are outside the channel integration.
- Inbound agent turns are triggered by kind-9 chat messages. Forum
  posts/comments and other Buzz event kinds are ignored, even when the identity
  belongs to that channel.
- Outbound local files are uploaded by the CLI. Flowly does not separately
  download inbound Buzz media metadata; media URLs present in message content
  remain links visible to the agent.
- Startup history seeds de-duplication state and is not replayed to the agent.
- The TUI can avoid channel IDs by using all joined channels and automatic home
  selection. Named channel picking and discovery reload are currently the
  Desktop experience; headless config and explicit TUI subsets use raw IDs.
- Joining a new group channel while the adapter is already running requires a
  gateway restart; new DM discovery is refreshed at runtime.
- A DM-only identity with no joined group channel cannot currently start the
  adapter, because group-channel preparation happens before DM discovery.
- `channels.buzz.authTag` applies to the Flowly WebSocket. Owner-attested
  CLI/REST calls require `BUZZ_AUTH_TAG` in the gateway environment/profile
  `.env`.
- `flowly channels status` is still WhatsApp-specific.

## Related

- [Channels overview](./overview.md)
- [Installation](../getting-started/installation.md#optional-buzz-dependency)
- [Configuration](../using-flowly/configuration.md)
- [Environment variables](../reference/environment-variables.md)
- [Troubleshooting](../using-flowly/troubleshooting.md)
- [Cron](../features/cron.md)
- [Service](../using-flowly/service.md)
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
