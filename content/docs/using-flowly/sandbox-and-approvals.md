---
title: Sandbox & exec approvals
eyebrow: Using Flowly
description: Flowly runs shell commands through a security pipeline — structural analysis, a policy gate, and per-command approvals — while an optional OS-level sandbox constrains the agent process. This page covers how exec is gated, how approvals work, the sandbox, and runtime working-directory tracking.
---

## The exec security pipeline

Every command the agent wants to run goes through these stages, in order:

1. **Enabled check** — if the exec tool is disabled, the command is denied.
2. **Analyze** — the command is parsed. A *structurally unusable* command (empty, contains a null byte, unparseable) is hard-rejected here.
3. **Protected-path floor** — if the command touches a protected path (SSH keys, AWS credentials, the macOS Keychain, `/etc/shadow`, and similar), it is **hard-rejected regardless of mode**. This is not whitelistable and no approval can grant access.
4. **Policy gate** — the security mode, ask mode, and allowlist decide whether to allow, deny, or prompt (see below).
5. **Approval** — if a prompt is required, a pending approval is created and the agent waits for your decision.
6. **Execute** — the command runs with the configured timeout.

The policy is re-read from disk if its file changed, so a long-running gateway picks up CLI/TUI edits without a restart.

## Policy: security mode × ask mode

The policy has two independent dimensions.

**Security mode** (`security`):

| Value | Behavior |
| --- | --- |
| `deny` | All exec commands denied. |
| `allowlist` | Only commands whose resolved path matches the allowlist (or are safe bins) run; others miss and are denied or prompted. |
| `full` | Everything is permitted (subject to the ask mode and the protected-path floor). |

**Ask mode** (`ask`):

| Value | Behavior |
| --- | --- |
| `off` | Never prompt (except risky-command force-approval, below). |
| `on-miss` | Prompt on an allowlist miss. **Only has effect in `allowlist` security mode** — in `full` mode `on-miss` does nothing. |
| `always` | Prompt on every command. |

**Defaults:** `security="full"`, `ask="off"`. Out of the box, exec runs without prompting.

### Risky-command force-approval

A command flagged as risky (dangerous patterns, subshells/substitution, redirects, multi-line) always prompts — **unless** the policy is exactly `security=full` + `ask=off`.

> [!WARNING]
> "Full trust with no asking" (`security=full` + `ask=off`) literally means no prompts even for dangerous patterns. Tighten `ask` to `on-miss` or `always`, or use `allowlist` mode, if you want a safety net.

### Allowlist and safe bins

In `allowlist` mode, a command is permitted if it matches the allowlist or is a safe bin:

- **Allowlist entries** are glob patterns matched against the command's **resolved absolute path** (leading `~` is expanded). A command whose executable cannot be resolved can never be allowlisted nor remembered with "Allow always".
- **Safe bins** are a fixed set of stdin-only, read-only tools:

  ```text
  jq grep cut sort uniq head tail tr wc cat echo date whoami pwd hostname uname
  ```

  (On Windows: `findstr where ver type tasklist systeminfo` are added.) A safe bin only stays safe if none of its arguments looks like a path or names an existing file — so they cannot be used to read arbitrary files via args.

## The approval prompt

When a command needs approval, you get a prompt on your active surface showing the command and **why** it prompted (the resolved path and risk reasons). Three decisions:

| Key | Label | Meaning |
| --- | --- | --- |
| `a` | Allow once | Run this command now; do not remember it. |
| `s` | Allow always | Run it now **and** add its resolved path to the allowlist. |
| `d` | Deny | Reject the command. |

`Allow always` is only offered when the command has a resolvable path (pipelines and shell builtins can't be remembered). Press `Esc`/`q` to close without deciding. There is no "for session" button.

> [!NOTE]
> If no decision arrives before the approval timeout, the command is denied.

## Plan mode: approving the work, not the command

Everything above gates **one command at a time**, as the agent reaches it.
[Plan mode](/docs/features/plan-mode) gates the **whole task, up front**: the
agent proposes a plan, and until you approve it, every side-effecting tool —
`exec` included, but also file writes, email, integrations, and subagents — is
refused before it runs.

The two layers are independent and compose:

| | Exec approvals | Plan mode |
| --- | --- | --- |
| Gates | A single shell command | Every side-effecting tool |
| Asks | When the command is reached | Before any work starts |
| You approve | The command | The plan |
| Timeout means | Denied (that command) | Not approved (nothing runs) |
| Configured by | `security` / `ask` policy | `Shift+Tab` to **▣ Plan**, or `/plan` |

Plan mode sets **no** exec policy of its own — your `security`/`ask` settings
keep applying inside an approved plan. It's an extra gate in front of them, not
a replacement. So an approved plan running under `security=full` + `ask=off`
still executes its commands without per-command prompts; you approved the shape
of the work, not each step of it.

## Managing policy from the CLI

The `flowly approvals` command reads and writes the policy:

```bash
flowly approvals status                 # show security / ask / ask_fallback / allowlist count
flowly approvals set --security full --ask off
flowly approvals set -s allowlist -a on-miss
flowly approvals list                   # table of allowlist entries
flowly approvals add "/usr/bin/git"     # add a glob pattern to the allowlist
flowly approvals remove "/usr/bin/git"  # remove a pattern
flowly approvals safe-bins              # list the built-in safe bins
```

`--security` accepts `deny`, `allowlist`, or `full`; `--ask` accepts `off`, `on-miss`, or `always`. Invalid values exit non-zero.

## Managing policy and approvals from the TUI

| Command / key | Opens |
| --- | --- |
| `/approvals` | The pending-request queue. |
| `/approvals permissions` or `/approvals policy` | The permissions / policy editor. |
| `/permissions` (or `/policy`) | The permissions / policy editor. |
| **F3** | The pending-request queue. |

In the pending queue you decide approvals with the same Allow once / Allow always / Deny choices. The policy editor applies security/ask changes and allowlist removals live.

## Where the policy lives

> [!IMPORTANT]
> The exec policy and allowlist do **not** live in `config.json`. They live in the approvals store at `~/.flowly/credentials/exec-approvals.json`.

```text
~/.flowly/credentials/exec-approvals.json
```

The file is written with mode `0600` and serialized by a file lock. Stored defaults: `security="full"`, `ask="off"`, `ask_fallback="deny"`, empty `allowlist`. Legacy `tools.exec.security` / `tools.exec.ask` keys in `config.json` are ignored and migrated once.

Exec **runtime knobs** (not the policy) do live in `config.json` under `tools.exec`:

```json
{
  "tools": {
    "exec": {
      "enabled": true,
      "timeoutSeconds": 300,
      "maxOutputChars": 200000,
      "approvalTimeoutSeconds": 120,
      "cronMode": "deny"
    }
  }
}
```

`cronMode` (`deny` or `approve`) decides what happens when an exec approval is requested from inside a cron run: `approve` auto-allows once, `deny` rejects.

## The OS sandbox

When enabled, Flowly re-executes itself inside an OS sandbox so the whole agent process — including any command it runs — is constrained by kernel rules. The sandbox restricts the **filesystem**; it does not currently restrict network egress.

### Toggle precedence

The sandbox decision is made in this order:

1. **`FLOWLY_SANDBOX` env opt-out** — if set to `0`, `false`, `off`, or `no` (case-insensitive), the sandbox is disabled.
2. **`security.sandbox` config** — read from `~/.flowly/config.json`, **default-on** (a missing field, missing config, or read error all mean on; only an explicit `false` disables).
3. **Platform** — macOS uses `sandbox-exec`, Linux uses `bwrap`. Windows and other platforms run unsandboxed (roadmap).

```json
{
  "security": {
    "sandbox": true
  }
}
```

```bash
FLOWLY_SANDBOX=0 flowly ...   # run this invocation without the sandbox
```

### What it enforces

- **macOS (`sandbox-exec`):** allow by default, but **deny reads** of sensitive home subpaths (`.ssh`, `.aws`, `.config/gcloud`, `Library/Keychains`, browser profiles/cookies, `.mozilla`, …) and **deny writes** except under `~/.flowly`, `$HOME`, and temp dirs. Subprocesses inherit the profile.
- **Linux (`bwrap`):** root mounted read-only, a fresh `/proc`, a tmpfs `/tmp`, read-write only for `~/.flowly`, `$HOME`, and `/tmp`, sensitive paths masked, PID/UTS/IPC namespaces unshared, dies with parent.

> [!WARNING]
> **Network is currently wide open** on both platforms (macOS `(allow network*)`, Linux `--share-net`). The sandbox is a filesystem boundary, not an egress filter — your firewall is responsible for egress.

> [!NOTE]
> The sandbox **fails open**: if `sandbox-exec`/`bwrap` is missing or the wrapper can't start, Flowly runs unsandboxed rather than crashing, and does not surface a warning in this path.

## Runtime working directory

Exec (and codex sessions) run in a **runtime cwd** — your project directory — which is separate from Flowly's internal `workspace_path` (`~/.flowly/workspace`). The runtime cwd is resolved per call, first match wins:

1. An explicit `working_dir` on the tool call (honored verbatim, only `~`-expanded).
2. The **per-session cwd** (see `cd` persistence below).
3. The `FLOWLY_CWD` environment variable.
4. The `agents.defaults.cwd` config value.
5. Workspace fallback → `config.workspace_path` → home.

```json
{
  "agents": {
    "defaults": {
      "cwd": "~/projects/myapp"
    }
  }
}
```

```bash
FLOWLY_CWD=~/projects/myapp flowly ...
```

### `cd` persistence per session

Within a session (non-Windows), a `cd` in one exec call persists to the next. The command is wrapped so its real exit code and stdout/stderr are unchanged; the final working directory is captured to a temp file and saved as the session's cwd. Per-session cwd lives in an in-process, lock-guarded map — there is no global `os.chdir` and no env mutation, so concurrent sessions don't clobber each other. A directory that no longer exists is ignored and re-validated next call. Non-session calls (cron, one-offs) are never wrapped.

## Limitations

> [!WARNING]
> **The `process` and `docker` tools bypass the exec approval/safety pipeline.** Long-running commands launched through the `process` tool and Docker operations through the `docker` tool run with **no command analysis, no approval flow, and no environment scrubbing** — background processes inherit the full environment, and destructive Docker operations (`prune`, `rm -f`, `compose down`) are not gated by the exec allowlist. Treat these tools as more privileged than `exec`.

- **`security=full` + `ask=off` runs risky commands without prompting** (the force-approval carve-out). Set `ask` to `on-miss` or `always`, or use `allowlist` mode, if you want prompts.
- **The sandbox does not filter network egress** in the current phase — it is a filesystem boundary only.
- **The sandbox fails open silently** if its wrapper is unavailable.

## Related

- [Plan mode](../features/plan-mode.md)
- [Codex runtime](../features/codex-runtime.md)
- [Providers & models](./providers-and-models.md)
- [Channels overview](../channels/overview.md)
- [CLI commands](../reference/cli-commands.md)
- [Slash commands](../reference/slash-commands.md)
- [Environment variables](../reference/environment-variables.md)
- [Setup wizard](../getting-started/setup-wizard.md)
