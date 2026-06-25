# Sandbox Architecture

How Flowly wraps the Python agent process in a platform-native OS
sandbox. This is the load-bearing piece of Flowly's security model
(`SECURITY.md` §2.2 — "the only security boundary against an
adversarial LLM or malicious plugin is the operating system").

## What the sandbox actually prevents

The agent process runs Python with provider API keys and channel
tokens in memory. A compromised plugin loaded into that process has
access to that memory by virtue of being in-process — that's a
property of the runtime, not something the sandbox changes. The
sandbox addresses two adjacent vectors:

1. **Filesystem reads outside the agent's legitimate scope.** A
   plugin (or an LLM-emitted tool call) trying to `open('~/.ssh/id_rsa')`
   or read the macOS Keychain database gets `Operation not permitted`
   from the kernel — the call never reaches the file.

2. **Filesystem writes outside the workspace.** Writing to `/etc`,
   `/Library`, system paths, or persisting outside `~/.flowly` and
   the operator's home is denied. A plugin can't drop a launchd plist
   or modify the user's `.zshrc` for persistence.

What it doesn't prevent (intentionally):

- In-process memory reads — `from flowly.config.loader import load_config`
  works fine inside the sandbox. Nothing stops it.
- Network egress to arbitrary hosts. See
  [`network-egress.md`](network-egress.md) for why we don't filter.
- Subprocess env inheritance — but those subprocesses go through
  [`subprocess-env-scrub.md`](subprocess-env-scrub.md) first, which
  closes the env-leak half.

## Code layout

```
flowly-desktop/src/main/local/sandbox/
├── policy.ts        — SandboxPolicy type + buildDefaultPolicy()
├── launcher.ts      — SandboxLauncher interface + env-var gate
├── no-sandbox.ts    — Passthrough fallback
├── macos.ts         — sandbox-exec(1) launcher + SBPL profile
├── linux.ts         — bubblewrap launcher
├── windows.ts       — Stub (isSupported always false)
└── index.ts         — Public exports + createLauncher() factory
```

Single import point — `flowlyai-service.ts` only knows about
`./sandbox`, never the individual files. Swap implementations without
touching the spawn path.

## The SandboxPolicy object

Defined at `flowly-desktop/src/main/local/sandbox/policy.ts:42`. The
shape:

```ts
interface SandboxPolicy {
  denyReadPaths: readonly string[]      // takes precedence over reads
  allowWritePaths: readonly string[]    // default-deny outside this list
  allowNetworkHosts: readonly string[]  // empty = unrestricted (Phase A/B default)
  allowProcessExec: boolean             // always true in v1
  workspacePath: string                 // currently $HOME until UI gives a picker
  homePath: string                      // for path substitution
}
```

`buildDefaultPolicy({homePath, workspacePath})` at `policy.ts:88` is
the only producer. The deny list is **fixed in code** — see the file
header comment for the rationale on why we don't expose per-category
toggles to users.

Deny list (everyone gets these denied, no UI to flip):

| Category | Paths |
|---|---|
| SSH | `~/.ssh` |
| AWS | `~/.aws` |
| GCP | `~/.config/gcloud`, `~/.gcp` |
| macOS Keychain | `~/Library/Keychains` |
| Browser storage | Chrome / Firefox / Brave / Edge `Application Support` dirs, `~/Library/Cookies`, `~/.mozilla`, `~/.config/{google-chrome,BraveSoftware}` |

Allow-write list:

| Path | Purpose |
|---|---|
| `~/.flowly` | All Flowly state (config, artifacts, memory, audit, plugins) |
| `$workspace` | Currently `$HOME`; future Settings picker |
| `/tmp`, `/private/tmp`, `/private/var/folders` | OS-blessed temp dirs |

Network: `allowNetworkHosts: []` (empty array). See
[`network-egress.md`](network-egress.md) for why this stays empty
and what the launchers do with that.

## The SandboxLauncher interface

Defined at `flowly-desktop/src/main/local/sandbox/launcher.ts:21`:

```ts
interface SandboxLauncher {
  readonly platformName: string  // "macos" / "linux" / "windows" / "none"
  isSupported(): Promise<boolean>
  wrap(innerShellCommand: string, policy: SandboxPolicy): Promise<string>
}
```

`wrap()` is async because the launcher may write a profile file to
`/tmp` before returning the wrapped command. The returned string is
ready for `child_process.spawn(returned, { shell: true })`.

**Failure stance is fail-open.** If the launcher can't write its
profile, can't find its primitive, or any step fails, it falls back
to returning the inner command unwrapped and logs a warning. Failing
closed (refusing to spawn) would mean the user's agent doesn't start
because of a sandbox problem — worse outcome than running with the
pre-sandbox baseline they had yesterday.

## Factory: `createLauncher()`

At `flowly-desktop/src/main/local/sandbox/index.ts:36`. The
selection logic:

```
1. sandbox disabled via env var?           → NoSandboxLauncher
2. process.platform === 'darwin'           → MacOSSandboxLauncher
3. process.platform === 'linux'            → LinuxSandboxLauncher
4. process.platform === 'win32'            → WindowsSandboxLauncher
   (which returns isSupported=false → fall through)
5. otherwise                               → NoSandboxLauncher
```

Caller never sees null. Always gets *something* with a working
`wrap()` method. The factory is the only entry point from outside
the sandbox module — `flowlyai-service.ts` calls
`createSandboxLauncher({env: spawnEnv})` and treats the result
uniformly.

## macOS — sandbox-exec(1)

File: `flowly-desktop/src/main/local/sandbox/macos.ts`.

`sandbox-exec` ships with every macOS version since 10.5. Apple has
marked it deprecated for years but it remains the mechanism behind
Xcode, Safari, and most of `/usr/libexec`. SBPL (sandbox profile
language) is undocumented in the public SDK but stable in practice.

**Profile shape** — generated dynamically from the SandboxPolicy:

```scheme
(version 1)
(allow default)

;; Sensitive paths the agent must not read.
(deny file-read*
  (subpath "/Users/x/.ssh")
  (subpath "/Users/x/.aws")
  ...
)

;; Writes default-deny outside the explicit allow list.
(deny file-write*)
(allow file-write*
  (subpath "/Users/x/.flowly")
  (subpath "/Users/x")
  (subpath "/tmp")
  ...
  (literal "/dev/null")
  (literal "/dev/dtracehelper")
)

;; Phase A/B: outbound network unrestricted.
(allow network*)

;; Subprocesses inherit this profile.
(allow process-exec*)
(allow process-fork)
```

Strategy: **start from `(allow default)`** and carve out deny rules.
Comparable upstream sandboxes use the same baseline. Starting from
deny-all would require enumerating every mach service, IOKit
endpoint, dyld interposer, and so on that a Python interpreter
touches at startup — pragmatically infeasible.

**Wrap mechanism**:

```
sandbox-exec -f /tmp/flowly-agent-<pid>-<ts>.sb /bin/sh -c '<inner command>'
```

`sandbox-exec` is special — it `exec`s into the child program after
applying the profile. The string `sandbox-exec` never appears as a
running process; `ps -ef` shows the agent as `python3 ...`, but it's
the sandboxed python. Confused many people including the engineer
writing this doc. Use `sandbox_check_by_pid()` from
`/usr/lib/libsystem_sandbox.dylib` or just try a denied operation to
verify enforcement.

**Profile files** are written to `os.tmpdir()` and not explicitly
cleaned up. macOS prunes `/tmp` on reboot; filename includes the pid
+ timestamp so concurrent starts don't collide.

**String escaping** is at `macos.ts:142`. SBPL strings use Lisp-
style double quotes with `\"` and `\\` escapes. Newlines in paths
are rejected outright — no legitimate macOS path contains one and
the SBPL tokenizer gets confused.

## Linux — bubblewrap (bwrap)

File: `flowly-desktop/src/main/local/sandbox/linux.ts`.

`bwrap` is the Linux primitive Flatpak uses under the hood. It builds
a fresh mount namespace, bind-mounts the host root with the bits we
want exposed, then runs the target process inside.

bwrap is `bubblewrap` package on Debian/Ubuntu/Fedora/Arch — installed
by default on most desktop distros, opt-in on minimal server images.
Probed at `linux.ts:46` in order:
`/usr/bin/bwrap`, `/usr/local/bin/bwrap`, `/opt/homebrew/bin/bwrap`.

**Translation strategy** from SandboxPolicy to bwrap argv:

- Filesystem reads: bwrap has no per-path deny primitive. Workaround:
  read-only bind the rootfs (`--ro-bind / /`) and **mask** denied
  paths with empty tmpfs mounts (`--tmpfs ~/.ssh`). The agent sees
  an empty directory in place of the secret.
- Filesystem writes: rootfs is read-only bound, then `--bind-try`
  punches read-write holes for `allowWritePaths`.
- Network: `--share-net` always. `--unshare-net` would kill all
  outbound; per-host filtering would need slirp4netns + a userspace
  proxy we don't ship.
- Other isolation: `--unshare-{pid,uts,ipc,cgroup-try}`,
  `--die-with-parent`, `--new-session`.

**Edge case** at `linux.ts:75`: the default policy is generated
platform-agnostically and carries macOS-flavoured deny paths like
`~/Library/Keychains` which don't exist on Linux. bwrap's `--tmpfs`
errors on a missing mount target, so `wrap()` filters
`denyReadPaths` to entries that actually exist before generating the
args. Nothing to leak from a directory that isn't there.

**No Flowly Linux desktop build** ships today, but the launcher is
exercised by the CLI self-wrap path described in
[`cli-sandbox.md`](cli-sandbox.md). Tests pin the argv shape so the
isolation intent matches the macOS side even though we can't run an
end-to-end behavioural test from a macOS dev host.

## Windows — stub

File: `flowly-desktop/src/main/local/sandbox/windows.ts`.

`isSupported()` returns `false`. The factory falls through to
`NoSandboxLauncher` on Windows. **Windows agent runs unsandboxed**
— documented as roadmap in `SECURITY.md` §2.2.

Two viable implementations for future work, sketched in the file
header:

- **AppContainer + Job Object** (2-3 weeks). The "right" Windows
  answer. Process runs with an AppContainer SID; capabilities
  declared (`internetClient`, `picturesLibrary`, …); filesystem
  isolated via per-container namespace. Some Win32 APIs behave
  differently inside AppContainer and would need shimming.

- **Job Object + Restricted Token** (1-2 weeks). Pragmatic, weaker.
  SE_* privileges stripped, process tree constrained. No filesystem
  namespace, no network filtering without a WFP filter.

Pick when Windows market share or enterprise demand justifies the
work. The factory entry exists so the platform-detection branch is
exhaustive — no implicit "what if" fall-through when a new platform
shows up.

## Integration into the agent spawn

File: `flowly-desktop/src/main/local/flowlyai-service.ts`.

The historical spawn site at line 721 was:

```ts
this.process = spawn(command, { shell: true, ... })
```

Now (commit `a0a86ac`) it goes through `wrapWithSandbox()` at line
738. The wrap method:

1. Reads `FLOWLY_SANDBOX` from the spawn env. Explicit setting wins
   (operator override / CI).
2. If unset, reads `config.security.sandbox` from
   `~/.flowly/config.json`. `false` triggers `FLOWLY_SANDBOX=0`
   injection; otherwise the default-on env behaviour applies.
3. `createLauncher({env: effectiveEnv})` picks the platform launcher.
4. `buildDefaultPolicy({homePath, workspacePath})` builds the policy.
5. `launcher.wrap(innerCommand, policy)` returns the wrapped shell
   string.

Any exception in steps 1-5 falls back to the unwrapped command and
logs a warning. The principle is: a sandbox bug must not break the
user's agent. They opted in to a security improvement, not a
regression.

## Default-on behaviour

`launcher.ts:53` — `sandboxEnabledFromEnv()`:

```
FLOWLY_SANDBOX unset                           → enabled (default-on)
FLOWLY_SANDBOX = "0" / "false" / "off" / "no"  → disabled
FLOWLY_SANDBOX = anything else                 → enabled
```

Previously (Phase A) the default was disabled with opt-in. Flipped
to default-on in commit `b4356a1` once the env scrub + sandbox
combination was proven to keep legitimate workflows working.

## Boundary verification

The exact behaviours documented above are pinned in tests:

- Policy contents, SBPL emission, bwrap argv → `flowly-desktop/tests/main/sandbox-policy.test.ts`
- CLI gate, profile generation, end-to-end sandbox-exec → `flowlyai/tests/test_sandbox_cli.py`

See [`testing.md`](testing.md) for the test class breakdown and what
regression each test catches.

## Related commits

| SHA | What |
|---|---|
| `8f63859` | `SandboxLauncher` abstraction + `NoSandboxLauncher` |
| `4ea0322` | macOS sandbox-exec launcher with SBPL profile |
| `5d6cb1c` | Linux bubblewrap launcher |
| `d648969` | Windows stub |
| `a0a86ac` | Spawn-path integration |
| `fc49561` | Drop per-category capability toggles |
| `b4356a1` | Default-on flip |
| `9d8dd73` | Config field + IPC gating |
