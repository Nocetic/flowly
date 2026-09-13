---
title: Remote MCP setup
eyebrow: Using Flowly
description: Connect services to a self-hosted Flowly through verified SSH, without turning the gateway into a relay-managed installation.
---

Use this guide when you already added a **direct remote gateway** to a Flowly app
and want to connect a service such as Linear to the Flowly running there.

There are two different connections: the app manages your remote Flowly, and that
Flowly connects to the service's MCP server. The new SSH path protects the first
connection's **MCP management requests**. It does not change the provider's URL,
the provider's authentication method, or the location where its tools execute.

## What you need

- A running, updated Flowly gateway with `POST /api/mcp/manage` and a non-empty
  gateway token. Update the installation on the **remote machine**, not only the app.
- A Flowly app version that implements verified SSH MCP management. Core support
  alone does not imply that an installed Desktop, iOS, or Android build has it.
- Reachable SSH on that machine, a server login supported by the client, and
  permission to forward to `127.0.0.1:<gateway-port>` on that machine. The current
  app flow uses password authentication; a key-only or interactive-only SSH policy
  is not automatically supported. Do not enable unrestricted root/password login
  merely to make a test pass; use an account permitted by your server policy.
- The correct **two ports**: SSH is commonly `22`; Flowly defaults to `18790`.
  Use the actual configured values. SSH forwarding does not require publicly
  exposing the gateway's management route.
- A trusted copy of the SSH server's fingerprint from your hosting provider or
  administrator, for the first connection.

No relay, Flowly server registration, or Firestore gateway record is needed for
this management path. Your saved gateway remains a direct gateway.

## Connect from the app

1. Select the remote Flowly and open its MCP connections screen, or open a
   connection request in its chat.
2. If secure access is needed, complete the SSH form in a client that supports
   it. Existing saved SSH details may be reused. The **SSH username** is your
   server login—for example an administrator-provided `ubuntu` account—not your
   Flowly email, gateway name, or MCP provider account.
3. Compare the displayed SHA-256 fingerprint with your trusted source and confirm
   only if it matches. The fingerprint identifies the SSH server; it is not a
   password. A changed previously trusted fingerprint must be investigated,
   not automatically accepted.
4. The client authenticates to SSH and sends the gateway token **inside** the
   encrypted channel. Both authentications must succeed.
5. Choose the MCP service. Complete any provider sign-in on your own device,
   review the discovered tools, and explicitly save the permissions.
6. Check the live connection status. **Saved** does not necessarily mean
   **connected**. Closing a setup panel is not the same as cancelling setup.

The runtime receives MCP configuration and OAuth results, not your SSH login.
Provider OAuth tokens stay on that selected runtime. Newly entered SSH details
belong to the app's credential storage and its save choice; they must not be sent
through chat or added to the runtime's `config.json`. Existing gateway/terminal
credential storage is separate and may have different synchronization behavior.

## What changes—and what does not

| Question | Answer |
|---|---|
| Must I turn on WSS for this MCP path? | No, if the client uses verified SSH management. Working TLS remains an alternative. |
| Does the TLS switch configure my server? | No. It selects TLS on the client; a valid TLS endpoint must already exist. |
| Where does OAuth open? | On the owner's device. The authorization result returns to the original selected runtime. |
| Where do tools and local MCP commands run? | On the selected Flowly runtime's machine, not on the phone displaying the screen. |
| Does SSH consent give the agent all tools? | No. Provider sign-in and explicit MCP permissions remain separate. |
| Does this create a URL for another agent? | No. External-agent access uses a scoped key and the separate `/mcp` protocol endpoint. |
| Does this encrypt all gateway traffic? | No. Existing chat, media, and other gateway routes keep their existing transport. |

> [!WARNING]
> A gateway token is an administration credential. If another part of your
> deployment sends it across public plaintext HTTP/WS, MCP-only SSH cannot make
> that deployment safe. Protect the normal gateway connection as well, using
> a trusted private network path or correctly configured TLS. Do not expose a
> plaintext gateway to the internet simply because MCP setup now uses SSH.

### Profiles and client availability

The core management route accepts an optional profile selector when the gateway
has a profile host. That does **not** mean every app's SSH implementation supports
profile selection. The current dedicated app SSH flow targets the direct gateway
root; do not assume it can manage a selected profile over that same path. Existing
secure profile routing remains separate. A client must never silently substitute
the root for an unsupported profile.

Treat release-specific client support and actual runtime capabilities as the
source of truth. If the SSH form is absent, update the app or use its already
supported secure management path. Do not disable a security gate to force an
older client through.

## Troubleshooting

| Symptom | Next check |
|---|---|
| TLS error immediately after enabling WSS | The saved host/port must really serve TLS with a valid certificate. A plaintext gateway port cannot become TLS through an app toggle. |
| SSH connection failed | Check the SSH host, SSH port, server username, password, firewall, and allowed authentication method. These are not MCP-provider credentials. |
| First-use SHA-256 prompt | Verify it against the server fingerprint supplied through a trusted channel before continuing. |
| Previously trusted fingerprint changed | Stop. Ask the administrator whether the host was replaced or its keys rotated. Reset trust only after independent verification. |
| SSH login succeeds, forwarding fails | Ensure Flowly is running on the expected remote loopback port and the SSH account may forward there. Do not work around it by sending management requests to public plaintext HTTP. |
| Gateway token rejected | Check the saved gateway token. SSH authentication does not replace it; a provider API key or external-agent access key will not work. |
| Update required / route not found | Update and restart the selected remote gateway, then confirm the correct port/path. Updating local source does not replace a running remote process. |
| Secure transport required | The management request did not arrive through TLS or a loopback peer, or it failed the browser-origin check. Forwarded headers alone do not prove TLS. |
| OAuth succeeds, but saved connection is offline | Inspect the provider connection and live apply result. SSH only establishes the owner management path; it cannot guarantee provider availability. |

## Related

- [MCP features, OAuth and permissions](../features/mcp.md)
- [MCP management API](../reference/mcp-management-api.md)
- [Self-hosting](self-hosting.md)
- [Gateway configuration](configuration.md#gateway)
