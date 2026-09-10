# MCP management over SSH — client contract

`POST /api/mcp/manage` is an additive owner-management endpoint. It supports
the version-1 `mcp.*` connection/setup/access/chat-request methods listed in
`flowly/gateway/mcp_management.py`. It is not a generic RPC proxy.

Requests use `Authorization: Bearer <gateway-token>` and JSON:

```json
{"method":"mcp.capabilities","params":{}}
```

An optional `profile` selects an isolated profile through the existing profile
host, without widening the allowed method set. Responses are either
`{"result": ...}` or `{"error":{"code":"...","message":"..."}}`.

Security boundaries:

- A non-empty, valid gateway token is required even on loopback, regardless of
  the ordinary gateway's local-auth setting.
- The socket must use TLS or originate from loopback. Forwarded headers do not
  establish transport trust. A TLS reverse proxy can connect through loopback.
- SSH clients forward only to the remote gateway's loopback port, verify the
  SSH server identity, and send the gateway token inside that encrypted channel.
- The request limit is 256 KiB. Unknown methods/envelope fields are rejected;
  responses are not cacheable. Unexpected failures do not echo exception text.
- There is no SSH password storage or SSH implementation in the agent. SSH
  authentication and host-key verification belong to the client and server sshd.

Deployment prerequisites: an updated gateway with a configured token, reachable
SSH password authentication, and forwarding permitted to its loopback listener.
An old gateway returns 404; clients must request an update, not downgrade to
plaintext management. This does not require a relay or Firestore registration.

Scope: this endpoint does **not** migrate or harden the existing chat, media,
generic WebSocket, or other HTTP endpoints. Public unencrypted gateway traffic
retains its existing risks (including exposure of its shared access token).
SSH-protected MCP management is not a claim that the entire gateway is secure.

`tests/test_mcp_management_transport.py` covers the route boundary. The isolated
`tests/mcp/desktop_oauth_peer.py` also supports `FLOWLY_MCP_TEST_SSH=1` for the
Desktop acceptance suite: real gateway, SSH, OAuth/PKCE, MCP tool discovery,
permission review, revocation, and preservation of unrelated email settings.
