---
title: MCP management API
eyebrow: Reference
description: The authenticated HTTP owner-management route for MCP connections, OAuth setup, chat requests, and scoped external access.
---

`POST /api/mcp/manage` is an **owner administration route** on a running Flowly
gateway. It carries the existing MCP feature methods through TLS or an SSH
loopback forward. It is not a general RPC proxy or a standard MCP protocol endpoint.

## Choose the correct endpoint and credential

| Endpoint | Caller | Credential | Purpose |
|---|---|---|---|
| `/api/mcp/manage` | Owner app or trusted management client | Gateway administration token | Configure MCP connections, complete OAuth/permission review, manage external keys |
| `/mcp` | External compatible agent | Scoped, expiring MCP access key | Standard MCP calls to the tools that key permits |

SSH login is a third, separate credential check. Neither an SSH password nor a
provider OAuth token authenticates this management route. Do not give the gateway
administration token to an external agent as an MCP key. The method allowlist
limits this route; it does not turn a gateway token into a least-privilege key.

## Authentication and transport

Send `Authorization: Bearer <gateway-token>`. The shared gateway extractor also
accepts `X-Flowly-Token`, which takes precedence if both are supplied. Send one
header only; query-string credentials are not used by this route.

Authentication is mandatory even on loopback and even when other local gateway
routes retain tokenless behavior. An empty configured token never authorizes a request.

The request must arrive over a TLS socket or from a loopback socket peer. IPv4,
IPv6, and IPv4-mapped IPv6 loopback peers are recognized. `Forwarded` and
`X-Forwarded-Proto` do not establish transport trust. HTTP/HTTPS browser origins
must pass the shared gateway host/origin check; this route does not grant a
cross-origin browser API.

An SSH client should verify the host key before authentication and forward only
to the selected gateway's remote `127.0.0.1:<port>`. A TLS reverse proxy may connect
to the gateway over loopback; do not publish this owner route as an unauthenticated
proxy. The route is registered automatically; there is no separate SSH service
inside Flowly and no relay/Firestore registration requirement.

## Request and response envelope

Send a JSON object with only these top-level fields:

| Field | Required | Meaning |
|---|---|---|
| `method` | Yes | One exact method from the allowlist below |
| `params` | No | JSON object; defaults to `{}` |
| `profile` | No | Nonblank profile name, at most 128 characters; omit or use `null` for the gateway root |

For a **read-only** capability check on an already authenticated transport:

```http
POST /api/mcp/manage HTTP/1.1
Host: 127.0.0.1:18790
Authorization: Bearer <gateway-token>
Content-Type: application/json
```

```json
{"method":"mcp.capabilities","params":{}}
```

Success returns HTTP 200 and `{"result": ...}`. Read `result.version`,
`connectionSetup`, `nativeOAuth`, `explicitPermissions`, `chatSetup`,
`externalAgentAccess`, `oauthCallbackModes`, and `oauthRedirectUris` as capabilities,
not assumptions. `version: 1` alone does not prove the setup service is available;
the booleans depend on the active runtime. Do not fabricate a mobile callback URL
that the runtime did not advertise.

Failures return `{"error":{"code":"...","message":"..."}}`. All responses
produced by this handler use `Cache-Control: no-store`. This is not JSON-RPC:
do not add top-level `jsonrpc` or `id` fields. An operation `id`, where required,
belongs inside `params`.

## Allowed methods

| Method | Purpose / principal parameters |
|---|---|
| `mcp.capabilities` | Read version and currently available setup/OAuth/access capabilities |
| `mcp.connections.list` | Read configured and catalog entries, permissions and runtime status |
| `mcp.connections.action` | Owner action with `name`, `requestId`, and `action`: `enable`, `disable`, `retry`, or `remove` |
| `mcp.setup.begin` | Begin a catalog/custom or chat-request draft; credential values belong only on this secure owner path |
| `mcp.setup.status` | Read the operation identified by `id` |
| `mcp.setup.pending` | Read retained setup operations; inspect each phase rather than assuming every returned operation is active |
| `mcp.setup.confirm` | Submit `id` and explicit `permissions` after successful discovery and review |
| `mcp.setup.callback` | Submit `id` and a validated OAuth `callback` object to the operation that originated it |
| `mcp.setup.cancel` | Explicitly cancel the operation identified by `id`, subject to its current phase |
| `mcp.setup.cancel_request` | Cancel by `name` and `requestId`, including when the begin acknowledgment was lost |
| `mcp.access.catalog` | Discover eligible external-access tools and conversations; `sessionKey` selects the owning conversation |
| `mcp.access.list` | Read external-key metadata, effective status and endpoint information |
| `mcp.access.create` | Create a bounded, owner-reviewed key record for an existing conversation and exact tools |
| `mcp.access.revoke` | Revoke the external credential identified by `id` |
| `mcp.chat.pending` | Read retained conversation connection requests; `sessionKey` filters to the conversation |
| `mcp.chat.cancel` | Cancel the chat connection request identified by `id` |

These are the existing owner feature methods, not permissions granted to the
model-facing `mcp_connection` tool. The agent can request setup but cannot grant
itself access. `chat.send`, shell commands, arbitrary configuration writes,
`profiles.rpc`, and standard MCP `tools/call` are not allowed as the envelope method.

### Setup lifecycle and retry safety

`mcp.setup.begin` accepts a connection name of 1–64 letters, digits, hyphens or
underscores and a caller-generated `requestId` of 16–128 such characters. Preserve
the same request ID **and exact parameters** when recovering an uncertain begin;
reusing the ID with different parameters fails. Cancellation by request ID blocks
late begin requests for that cancelled attempt; an intentional fresh setup needs
a fresh ID.

Inspect returned snapshots. Setup can progress through `checking`,
`awaiting_authorization`, `review`, `committing`, and a terminal phase. Native
OAuth uses the runtime's state/PKCE handoff. Client target selection, callback
validation, first-use SSH trust and explicit permission UI are still the client's
responsibility; this HTTP adapter does not replace those controls.

Confirm only after review. Confirmation publishes configuration and then applies
it to the live runtime. The final result may be saved but not connected; inspect
`saved` and the runtime result rather than treating HTTP 200 as completed setup.
Do not blindly replay a possibly side-effecting action after a transport failure.

### Profile routing

With `profile` supplied, the gateway dispatches the allowed method through its
profile host as `profiles.rpc` with the specified name. If no profile host exists,
the route returns `PROFILE_HOST_UNAVAILABLE` rather than falling back to root.

The HTTP envelope has no `expectedHostId`, `expectedBotId`, or per-request target
epoch fields. Do not infer the full identity-pinning contract of another transport
from the optional profile name. Apps must explicitly support this route for
profiles and retire stale operations on target changes. Current root-only SSH app
flows must not silently claim profile support.

## Limits and errors

The adapter reads at most **256 KiB of request body**. Unknown top-level fields,
non-object bodies/parameters and unknown methods are rejected. It has no separate
response-size or execution-time limit in this adapter; underlying methods and
client limits still apply. It is not a rate limiter or an OS sandbox.

| HTTP status | Code | Meaning |
|---|---|---|
| 401 | `UNAUTHORIZED` | Missing, wrong, or empty-configured gateway token |
| 403 | `SECURE_TRANSPORT_REQUIRED` | Socket is neither TLS nor loopback, or host/origin validation failed |
| 413 | `TOO_LARGE` | Request exceeds 256 KiB |
| 400 | `INVALID_PARAMS` | Invalid JSON, envelope, parameters or profile selector |
| 400 | `UNKNOWN_METHOD` | Method is outside the MCP management allowlist |
| 400 | Method-specific code | An existing feature method rejected the operation |
| 503 | `PROFILE_HOST_UNAVAILABLE` | A profile was requested from a gateway without a profile host |
| 503 | `UNAVAILABLE` | Unexpected dispatch failure; raw exception text is not returned |

A 404 for this route is not an OAuth rejection. Check the gateway version, running
process, destination port, and proxy path. An older gateway or incorrect route
must trigger an update/configuration diagnosis, never a plaintext management fallback.

Never log request authorization headers, SSH passwords, OAuth callbacks, provider
tokens or setup credential fields. The route does not add protection to unrelated
chat, media or generic gateway endpoints.

## Related

- [Remote MCP setup](../using-flowly/remote-mcp.md)
- [MCP OAuth and permissions](../features/mcp.md#oauth-sign-in)
- [External-agent access](../features/mcp.md#let-another-agent-use-flowly)
