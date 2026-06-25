# Flowly Security Policy

This document describes Flowly's trust model, names the boundaries the
project treats as load-bearing, and defines the scope for vulnerability
reports.

## 1. Reporting a Vulnerability

Report privately by emailing **contact@nocetic.com**. Do not open
public issues for security vulnerabilities.

A useful report includes:

- A concise description and severity assessment.
- The affected component identified by file path and line range
  (e.g. `flowly/plugins/loader.py:46-76`).
- Environment details (Flowly version, commit SHA, OS, Python version).
- A reproduction against `main` or the latest release.
- A statement of which trust boundary in §2 is crossed.

Please read §2 and §3 before submitting. Reports that demonstrate the
limits of an in-process heuristic this policy does not treat as a
boundary will be closed as out-of-scope under §3 — but they are still
welcome as regular issues or pull requests, just not through the
private security channel.

---

## 2. Trust Model

Flowly is a single-tenant personal agent. The desktop application runs
on the operator's local machine and acts on their behalf across
channels (Telegram, Web, Desktop, iOS). Its security posture is
layered, and the layers are not equally load-bearing. Reporters and
operators should reason about them in the same terms.

### 2.1 Definitions

- **Agent process.** The Python interpreter running the Flowly agent,
  including any Python modules it has loaded (skills, plugins, hook
  handlers, tool implementations).
- **Desktop process.** The Electron main + renderer processes that
  orchestrate the agent subprocess, mediate OS permissions (TCC,
  Accessibility, Screen Recording), and host the user interface.
- **Input surface.** Any channel through which content enters the
  agent's context: operator input, web fetches, email, gateway
  messages, file reads, MCP responses, tool results.
- **Trust envelope.** The set of resources the operator has implicitly
  granted Flowly access to by running it — typically, whatever the
  operator's own user account can reach on the host.
- **External surface.** Any channel through which a caller outside the
  local agent process can dispatch agent work, resolve approvals, or
  receive agent output: gateway WebSocket, Telegram, Web channel via
  the `useflowlyapp.com` relay, paired iOS device, etc.

### 2.2 The Boundary: OS-Level Isolation

**The only security boundary against an adversarial LLM or a malicious
plugin is the operating system.** Nothing inside the agent process
constitutes containment — not the approval gate, not output redaction,
not any pattern scanner, not any tool allowlist. Any in-process
component that screens LLM output is a heuristic operating on an
attacker-influenced string, and this policy treats it as such.

Flowly Desktop wraps the agent process in an OS-native sandbox by
default. The exact mechanism depends on the host platform:

- **macOS.** `sandbox-exec(1)` with a generated SBPL profile.
- **Linux.** `bubblewrap (bwrap)` with namespace flags derived from
  the same policy object as macOS.
- **Windows.** Native sandboxing is on the roadmap; current Windows
  builds run the agent without OS-level isolation. This is documented
  here and surfaced in the UI on first run.

The profile is generated from a single `SandboxPolicy` object so the
threat model is consistent across platforms. Categories enforced by
default:

- **Filesystem reads denied** for `~/.ssh`, `~/.aws`, `~/.gcp`,
  `~/Library/Keychains`, browser storage directories, and a curated
  list of credential paths.
- **Filesystem writes denied** outside `~/.flowly`, the active
  workspace, and OS-blessed temp directories.
- **Network egress** is open by default in v1; v2 will tighten this
  to a provider-domain allowlist derived from the operator's
  configured providers.
- **Process exec** restricted to standard system bin paths.

What this confines: anything the agent process does directly, plus
any subprocess it spawns (the agent's `exec` tool, MCP subprocesses,
etc.) — they inherit the sandbox by default.

What this does **not** confine: the Desktop (Electron) process,
which orchestrates TCC and Accessibility permissions on macOS and
holds the screenshot delegation HTTP server. The Desktop process is
trusted as part of the application.

Operators who need access to a normally-denied path (`~/.aws` for
cloud development, `~/.ssh` for SSH key-driven workflows) can grant
it persistently via Settings → Security → Agent Capabilities. These
are durable consents recorded in `~/.flowly/config.json`, distinct
from per-action approvals (§2.4).

### 2.3 Credential Scoping

Provider API keys, channel bot tokens, and the gateway JWT secret
live in `~/.flowly/config.json` and are loaded into the agent
process memory at startup. Any component running inside the agent
process — including skills, plugins, hook handlers, and tool
implementations — can read whatever the agent itself can read,
including these in-memory credentials.

The mitigation against in-process credential read is not preventing
the read (it can't be prevented in a Python interpreter without
process isolation, which Flowly's plugin runtime does not provide;
see §2.5). The mitigation is **denying the exfiltration path**:

- Sandbox network policy (§2.2) limits where the agent process can
  send data.
- Sandbox filesystem write rules limit where the agent process can
  persist data.
- Subprocess environment is filtered: shell, code-execution, and MCP
  subprocesses receive only operator-declared variables, not the
  agent's full secret-bearing environment.

This reduces casual exfiltration. It does not eliminate determined
exfiltration through covert channels (timing, error messages
flushed to operator-visible logs, etc.). Operators who treat the
host as a hostile environment should run Flowly under a stronger
posture (a virtual machine, a dedicated user account, etc.).

### 2.4 In-Process Heuristics

The following components screen or warn about LLM behaviour. They are
useful. They are not boundaries.

- The **exec approval gate** (`flowly/exec/approvals.py`) detects
  destructive shell patterns and prompts the operator before
  execution, with fan-out to Telegram, paired iOS devices, and the
  desktop UI. Shell is Turing-complete; a denylist over shell strings
  is structurally incomplete. The gate catches cooperative-mode
  mistakes, not adversarial output. Approval has **no effect on
  sandbox enforcement** — even an approved command runs inside the
  sandbox, intentionally, so that a misjudged approval cannot escape
  to system resources the operator has not separately granted.
- **Output redaction** strips secret-like patterns from rendered
  output. A motivated output producer will defeat it.
- **Plugin risk classification** in the desktop UI surfaces declared
  hooks and tools before install/enable. It is a review aid; the
  manifest is a self-declaration, and a misbehaving plugin can
  subscribe to hooks it did not declare at install time.

### 2.5 Plugin Trust Model

Plugins load into the agent process via `importlib.util.exec_module`
and run with full agent privileges: they can read the same
credentials, call the same tools, register the same hooks, and
import the same modules as anything shipped in-tree.

The boundary for third-party plugins is **operator review before
install** plus the OS-level sandbox (§2.2). A malicious plugin
cannot reach SSH keys, browser cookies, or arbitrary network
destinations beyond what the sandbox allows the agent process
itself to reach — but it can read whatever the agent currently has
in memory, including provider API keys.

A malicious or buggy plugin is not a vulnerability in Flowly Agent
itself. Bugs in Flowly's plugin-install or plugin-discovery path
that prevent the operator from seeing what they're installing are
in scope under §3.1.

### 2.6 External Surfaces

An **external surface** is any channel outside the local agent
process through which a caller can dispatch agent work, resolve
approvals, or receive agent output. Each surface has its own
authorisation model.

**Surfaces in Flowly:**

- **Gateway WebSocket** (`flowly/gateway/server.py`). Local
  IPC for the Desktop UI; loopback-only bind.
- **Web channel via relay** (`useflowlyapp.com`). Pushes events to
  paired iOS devices and browser clients via APNs / SSE; resolves
  inbound RPC over the same channel. Pairing is mediated by the
  relay and gated by an operator-issued pairing code.
- **Telegram channel.** Direct webhook from the Telegram Bot API;
  caller authorisation is the bot token + chat ID allowlist.
- **Discord, Slack, Email channels.** Analogous; each maintains
  its own authorisation list.

**Uniform rules:**

1. **Authorisation is required at every surface that crosses a trust
   boundary.** For local IPC the boundary is the host's user account;
   for network surfaces the boundary is the network. Code paths that
   fail open are bugs in scope under §3.1.
2. **Session identifiers are routing handles, not authorisation
   boundaries.** Knowing another caller's session ID does not grant
   access to their approvals or output.
3. **Within the authorised set, all callers are equally trusted.**
   Flowly does not model per-caller capabilities inside a single
   channel.

---

## 3. Scope

### 3.1 In Scope

- Escape from the OS-level isolation posture (§2.2): an
  attacker-controlled code path reaching state the sandbox claimed
  to confine.
- Unauthorised external-surface access: a caller outside the
  configured allowlist dispatching work, receiving output, or
  resolving approvals (§2.6).
- Credential exfiltration: leakage of operator credentials or
  session authorisation material to a destination outside the trust
  envelope, via a mechanism that should have prevented it
  (environment scrubbing bug, adapter logging, transport error that
  flushes credentials upstream, etc.).
- Plugin-install or plugin-discovery bugs that prevent the operator
  from seeing what they are installing (§2.5).
- Trust-model documentation violations: code behaving contrary to
  what this policy states.

### 3.2 Out of Scope

- Demonstrations that an in-process heuristic (§2.4) can be evaded.
  These are review aids, not boundaries. Pull requests improving
  them are welcome.
- Demonstrations that a plugin running inside the sandbox can read
  agent process memory. This is §2.5 behaviour, intentional, and
  the mitigation is at the exfiltration path (§2.3).
- Issues that require a malicious user with terminal access to the
  host. The host user is part of the trust envelope.
- Windows-specific isolation gaps in versions where Windows native
  sandboxing is documented as not-yet-shipped (§2.2).

---

## 4. Deployment Hardening

The most important hardening decision is matching the OS-level
isolation posture (§2.2) to the trust of the content the agent
ingests. Beyond that:

### 4.1 Network Egress

Flowly does **not** implement application-level network egress
filtering. The reasoning: reliable hostname-based filtering at the
application boundary is harder than it looks and provides false
security if done poorly.

Concretely:

- **macOS `sandbox-exec`** — SBPL's `(remote ip "...")` directive
  only accepts `*` or `localhost`. Apple intentionally does not
  expose hostname-based outbound filtering at this layer. We
  evaluated emitting `(allow network-outbound (remote-host ...))`
  rules and found the syntax is rejected by the runtime.

- **Linux `bubblewrap`** — has no per-host network filter; the
  primitive is namespace isolation (`--unshare-net`), which is
  all-or-nothing. A hostname-aware proxy in front would work but
  is a separate component we don't ship.

- **In-application proxy** — could theoretically intercept all
  outbound HTTP(S) for hostname filtering, but would still be
  bypassable by direct-IP connections, DNS-over-HTTPS, or abuse
  of allowed upload hosts (gists, package registries, S3 buckets).
  Defence in depth at best, not a boundary.

What operators concerned about credential exfiltration should
deploy instead:

- **macOS**: [Little Snitch](https://www.obdev.at/products/littlesnitch/)
  or [Hands Off](https://www.oneperiodic.com/products/handsoff/)
  for per-application outbound rules.
- **Linux**: `iptables` / `nftables` / `firewalld` rules scoped to
  the agent's user account; or `OpenSnitch` for an interactive UI.
- **Cross-platform**: a VPN or DNS service with egress filtering
  (NextDNS, Pi-hole, ControlD), or routing through a corporate
  proxy.

The agent's filesystem isolation (§2.2) and subprocess credential
scrubbing (§2.3) still apply regardless of network configuration.
Plugins reading agent memory are §2.5; nothing in §4.1 changes that.

### 4.2 Channels & Plugins

- Configure a caller allowlist for every network-exposed channel
  (Telegram chat IDs, Discord allowFrom, Slack groupPolicy, …).
  Adapters that fail open when no allowlist is configured are
  bugs in scope under §3.1.
- Review third-party plugins before install (§2.5). The desktop
  marketplace surfaces the manifest's declared `provides_hooks` /
  `provides_tools` with risk tagging on hooks that read
  conversation content — that's a review aid, not a boundary;
  read the plugin source for anything you don't already trust.
- Keep `~/.flowly/config.json` permissions at `0600`. The default
  install creates it that way; do not loosen.

### 4.3 Operational

- Run the agent as a non-root user. The desktop bundle does this
  by default; the CLI inherits the operator's shell user.
- Do not expose the gateway (`flowly gateway`, default port 18790)
  to the public internet without VPN, Tailscale, or firewall
  protection. The default bind is `127.0.0.1`.
- The gateway's WebSocket and the relay (`useflowlyapp.com`) carry
  authorisation tokens; treat both as sensitive transport. The
  relay is currently the only externally-reachable surface in the
  default install.

---

## 5. Versioning

This policy applies to Flowly Desktop ≥ 1.0 and the bundled `flowly`
Python package shipped with it. Earlier versions are unsupported.
