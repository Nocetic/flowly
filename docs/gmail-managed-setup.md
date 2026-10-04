# Managed Gmail setup

The CLI and Desktop use the same profile-local Gmail service. A remote agent
does not need a browser or a public OAuth callback listener. Google credentials
are never requested in chat or returned in management RPC responses.

## CLI

Run `flowly gmail connect` on the intended agent, or select its profile with
`flowly --profile NAME gmail connect`. On an SSH session, the CLI prints the
browser link instead of opening a browser on the server. Open that link on your
own computer, sign in to Flowly, compare the displayed agent and confirmation
code with the terminal, then authorize Gmail in Google's page.

- `flowly gmail connect --no-browser`: explicitly print the browser link.
- `flowly gmail connect --no-wait`: persist the request and return immediately.
- `flowly gmail connect`: resume the pending request after closing the terminal.
- `flowly gmail status`: verify the saved connection against Google.
- `flowly gmail cancel`: cancel the pending setup, not another saved account.
- `flowly gmail disconnect`: confirm removal of this profile's saved connection.

The default CLI command retains Gmail read/send permissions. Desktop offers
Gmail management plus optional Calendar, Drive, Contacts and Tasks before opening
Google consent. To choose services from the CLI:

```sh
flowly gmail connect --services gmail,gmail_manage,calendar,drive,contacts,tasks
flowly gmail connect --extend --services gmail,gmail_manage,tasks
```

Gmail management uses `gmail.modify`: trash/restore, archive/inbox, read/unread,
star/unstar and existing user labels. Immediate permanent deletion is not offered.
Every management operation first previews the exact message IDs and asks for
approval. A batch is limited to 100 explicit messages, not an open-ended query;
partial results distinguish successful, failed/unknown and unattempted IDs.

Sending and Workspace writes also require approval. This setup does not enable
automatic replies or register the machine as a cloud server.

## Desktop and management

The selected agent exposes `gmail.capabilities`, `gmail.status`,
`gmail.setup.begin`, `gmail.setup.pending`, `gmail.setup.status`,
`gmail.setup.cancel`, and `gmail.disconnect`. Direct remote gateways use the
existing authenticated SSH management transport at `/api/gmail/manage`;
plaintext remote WebSocket Gmail management is rejected. Local, TLS, and
authenticated relay clients use the same feature RPC methods.

Pending setup survives panel closure and process restart. A connection is only
reported connected after token acquisition, a Gmail profile check, and local
storage/configuration. A failed disconnect blocks local access immediately and
retains the private grant for a revocation retry. Already issued Google access
tokens are not invalidated globally by a per-agent disconnect.

## Storage and rollout

Files are profile-local under `credentials/gmail-setup.json` and
`credentials/gmail.json`. Atomic writes enforce private directory/file access.
The managed file holds a per-agent grant secret and a cached short-lived access
token, not the Google confidential client secret or refresh token. Protect these
files and backups as credentials; do not print them in support logs.

Existing installed Gmail credentials remain compatible and are never silently
replaced. `--extend` / “Extend Google access” binds a new authorization to the
current connection ID and Google account. The old connection remains usable
until account verification and the new local commit succeed. Cancellation and
wrong-account selection preserve it. Disconnect blocks both current access and
pending upgrades. A durable pending record retains old-grant revocation failures
for retry; no Google-wide revocation occurs. Managed Gmail does not share credentials across profiles.

The companion web broker must be configured and deployed before live use. Its
`docs/gmail-agent-authorization.md` describes the separate Google OAuth client,
server encryption key, restricted-scope publication, Firestore TTL and rate-limit
requirements. Local fixture tests do not establish production Google consent or
Windows ACL behavior. No deployment is part of this change.

## Permissions and conversation requests

`gmail.capabilities` remains version 1 for old clients and advertises
`permissionsVersion: 2`, `services`, `upgrade` and `chatSetup`. `gmail.setup.begin`
accepts an allowlisted `services` array and an optional `connectionId` for upgrade.
Status returns actual `services` separately from `requestedServices`. Tools are
registered dynamically against granted scopes; checking a box alone grants nothing.

The model-facing `google_connection` tool can check status or propose a connection.
It uses the runtime-owned conversation, never a model-supplied session key. A
proposal performs no OAuth/configuration mutation. Desktop polls
`gmail.chat.pending` and presents a review card in the composer slot, alongside
existing MCP requests. Human review calls `gmail.setup.begin` with `chatRequestId`
and `sessionKey`; `gmail.chat.cancel` declines only the corresponding proposal.
The tool waits for the actual result or expiry. Stopping a model turn removes its
proposal but does not revoke an owner-started OAuth flow; it can be resumed in
Connections. iOS and Android UI integration is deferred.

Only `gmail.capabilities` and conversation-scoped `gmail.chat.pending` are safe
for discovery over the existing authenticated direct-gateway transport. Setup,
status and cancellation still require TLS, loopback or authenticated SSH management.

Deploy the web broker before core/desktop versions that send service selections.
Tests use synthetic Google responses and do not prove production OAuth publication.
