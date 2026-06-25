# CLI Sandbox — Python self-wrap

How `flowly` invocations from the terminal (no Electron in front)
get themselves wrapped under a platform sandbox. The desktop's
spawn-side wrapping in `flowlyai-service.ts` only fires when Electron
is the parent; for `uv run flowly gateway`, `flowly skills install`,
`pipx run flowly`, distro packages, etc., the CLI must wrap itself.

## File

`flowly/sandbox/cli_wrap.py` — the only module that matters here.

Public entry point: `maybe_reexec_sandboxed()` (line 52). Called at
the very top of `flowly/cli/entry.py:main()` (line 58), before any
other initialisation:

```python
def main() -> None:
    from flowly.sandbox.cli_wrap import maybe_reexec_sandboxed
    maybe_reexec_sandboxed()  # may not return

    _configure_bundled_ssl_ca()
    # ... profile resolution, typer app launch
```

## What it does

`maybe_reexec_sandboxed()` either returns normally (no wrap needed)
or **never returns** — it `os.execve`s the current process under
the platform sandbox primitive. The new process re-enters `main()`
with the recursion guard set and falls through to the rest of
startup, but now inside the sandbox.

The gate sequence (line 71):

1. **Recursion guard.** `FLOWLY_SANDBOX_WRAPPED=1` in env → return
   immediately. Critical: without this, the re-execed child would
   re-execute itself forever.
2. **Explicit env opt-out wins.** `FLOWLY_SANDBOX` set to
   `0`/`false`/`off`/`no` → return.
3. **Config fallback when env unset.** Reads
   `~/.flowly/config.json` `security.sandbox`. Explicit `false`
   skips. Missing / corrupt / read-error → default-on (fail-safe
   per SECURITY.md §2.2).
4. **Platform dispatch.** macOS → `_reexec_macos`. Linux →
   `_reexec_linux`. Windows / other → return.
5. **Primitive check.** `/usr/bin/sandbox-exec` exists on macOS?
   `bwrap` on the path on Linux? If not, return.
6. **Build profile + re-exec.** Any I/O failure → return (fail
   open).

## macOS re-exec

`_reexec_macos()` at line 116. Calls `_build_sbpl_profile(home)`
(line 191), writes to `/tmp/flowly-agent-<pid>-<ts>.sb`, then:

```python
argv = [_SANDBOX_EXEC, "-f", profile_path, sys.executable, *sys.argv]
os.execve(_SANDBOX_EXEC, argv, new_env)
```

**Important `ps` artifact.** After `os.execve`, the python process
becomes `sandbox-exec`, which then `exec`s into python with the
wrapped command. PID stays the same throughout. `ps -ef | grep
sandbox-exec` shows nothing — `sandbox-exec` has already `exec`ed
away. The agent process appears as `python3 .../flowly gateway`
just like before, but it's wearing the sandbox profile. Confirmed
working in commit `2e15bb6` by demonstrating `~/.ssh` reads fail
inside the sandboxed flowly while succeeding outside.

The SBPL profile is **the same** as the desktop's policy.ts output
— same deny list, same allow-write list, same `(allow network*)`
default. The two implementations exist in parallel because we want
the agent to be sandboxed regardless of which entry point the
operator used.

## Linux re-exec

`_reexec_linux()` at line 142. Mirror of the desktop's `linux.ts`
launcher.

```python
argv = [bwrap, *bwrap_args, "--", sys.executable, *sys.argv]
os.execve(bwrap, argv, new_env)
```

`_build_bwrap_args(home)` at line 207 generates the same flag set
the TypeScript `buildBwrapArgs()` does: `--ro-bind / /`, `--proc`,
`--dev`, `--tmpfs /tmp`, read-only home with bind-try punches,
tmpfs masks for existing deny paths, `--share-net`,
`--unshare-{pid,uts,ipc,cgroup-try}`, `--die-with-parent`,
`--new-session`.

**Linux desktop doesn't ship yet**, but the CLI path is the real
power-user surface on Linux (operators running `flowly gateway`
directly under systemd or a distro package). This is also the only
Linux exercise of the sandbox until a desktop build lands.

## Why not just rely on Electron's wrapping?

Three reasons the CLI path needs its own wrap:

1. **Many invocations bypass Electron entirely.** `uv run flowly
   skills install foo/bar` is a one-shot CLI command that doesn't
   go through the gateway. If a plugin's install-time module-level
   code is malicious, Electron isn't in the loop to wrap it.

2. **Linux has no Electron build today.** Power users on Linux run
   `flowly gateway` from systemd / supervisord / a terminal.
   Electron-side wrapping would never apply.

3. **CI / scripts run the CLI.** Tests, automation, headless
   deployments — none of them spawn through Electron. The CLI
   wrap is the only way they get sandboxed.

## Why does the env var work this way?

Default-on with explicit-falsy opt-out, not default-off with
explicit-truthy opt-in. The lookup at line 78:

```python
explicit = env.get(_ENV_TOGGLE, "").strip().lower()
if explicit in _FALSE_VALUES:
    return  # operator turned it off
# env unset / truthy → continue with config / default-on
```

Reasoning: a future contributor (or a future you) who removes the
env handling code by accident shouldn't accidentally weaken the
default. The truthy interpretation is the absence of an explicit
deny, which means "ship without env handling at all" still results
in sandbox-on.

## Configuration

`config.security.sandbox` is the operator-facing knob. Wiring:

- Desktop Settings UI (Dashboard → Settings → Security toggle)
  writes here. See [`settings-ui.md`](settings-ui.md).
- CLI reads it directly via `_config_sandbox_enabled()` at line
  131.
- Both default to enabled if the field is absent.

The CLI does **not** read `security.strictNetwork` or
`security.allowedHosts` — those don't exist. The strict-network
mode was explored and removed. See [`network-egress.md`](network-egress.md).

## Failure modes

| Condition | Behaviour |
|---|---|
| `bwrap` not installed (Alpine, minimal images) | Returns, agent runs unsandboxed |
| `sandbox-exec` missing (unusual macOS) | Returns, agent runs unsandboxed |
| Kernel without user-namespace support | `os.execve(bwrap, ...)` fails, returns, unsandboxed |
| SIP-restricted environment breaking sandbox-exec | `os.execve` raises `OSError`, returns, unsandboxed |
| Profile write to `/tmp` fails (disk full, permission) | Returns before execve, unsandboxed |
| Config JSON corrupted | `_config_sandbox_enabled()` returns `True` (default-on), continues |

In every case, the principle is: **don't break the user's agent
because of a sandbox problem.** Log the issue if relevant, fall
through to the pre-sandbox baseline. The operator opted in to a
security improvement; they don't lose their agent because we
couldn't write a profile file.

## Testing

Boundary tests live at `flowlyai/tests/test_sandbox_cli.py`:

- `TestGateEnvVar` — env-var precedence cases.
- `TestGateConfig` — config fallback + corrupt-JSON + missing-config.
- `TestSBPLProfile` — profile string-level invariants (ssh deny
  present, write block ordering, etc).
- `TestSBPLEscaping` — quote / backslash / newline handling.
- `TestSandboxExecIntegration` — *actually* runs `/usr/bin/sandbox-exec`
  against the generated profile, verifies `~/.ssh` listing fails
  with `Operation not permitted`. Skipped on non-macOS hosts.
- `TestBwrapArgs` — argv shape (bound to run on macOS dev hosts too;
  pins intent without exec).

See [`testing.md`](testing.md) for the regression catch matrix.

## Related commits

| SHA | What |
|---|---|
| `2e15bb6` | macOS CLI self-wrap |
| `a1237d4` | Linux CLI self-wrap |
| `9221cc9` | CLI test suite |
