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

Only Gmail read/send permissions are requested. Email sending still requires
the existing user approval. This setup does not enable automatic email replies,
register the machine as a cloud server, or request other Google Workspace scopes.

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
replaced. A new managed connection requires explicit removal of an existing
connection first. Managed Gmail does not share credentials across profiles.

The companion web broker must be configured and deployed before live use. Its
`docs/gmail-agent-authorization.md` describes the separate Google OAuth client,
server encryption key, restricted-scope publication, Firestore TTL and rate-limit
requirements. Local fixture tests do not establish production Google consent or
Windows ACL behavior. No deployment is part of this change.
