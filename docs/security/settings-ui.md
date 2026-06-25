# Settings UI — Sandbox Toggle

The user-facing knob for the OS sandbox: where it lives, how the
toggle persists, and why a single master switch instead of per-
category controls.

## Where

`flowly-desktop/src/renderer/src/pages/Dashboard/SettingsTab.tsx`.

Dashboard → Settings tab → "Security" card at the bottom (after AI
Configuration and Maintenance).

The toggle lives **in the Dashboard tab**, not the app-level
Settings page (`Settings.tsx`). Reasoning in commit `c9f5284`: the
Dashboard is where operators already manage the running bot
(restart, model selection, repair). Sandbox isolation fits there
with the rest of "things you do to your agent". The app-level
Settings page hosts OS-preferences-style settings (launch at login,
appearance, billing, privacy) — burying sandbox there would make it
hard to find for the audience that cares.

## Shape

```
┌─ Security ──────────────────────────────────────────┐
│ 🛡  OS-level isolation for the agent process        │
│                                                      │
│  Sandbox                                       [ON]  │
│  Run the agent inside an OS sandbox so it           │
│  cannot read your SSH keys, cloud credentials,      │
│  browser data, or write outside your workspace      │
│  and Flowly's own folder. Recommended.              │
│  ⟳ Restarting gateway…    (during transition)       │
└──────────────────────────────────────────────────────┘
```

The card is self-contained — `SandboxCard()` component at line 80 of
`SettingsTab.tsx`. It reads `electronAPI.flowlyai.readConfig()` on
mount, writes via `writeConfig({ security: { sandbox: bool } })`,
and triggers `restartGateway()` on every toggle.

## Toggle flow

```
1. User taps the switch
2. Optimistic state flip (UI shows new state immediately)
3. writeConfig({ security: { sandbox: next } }) writes to
   ~/.flowly/config.json
4. restartGateway() — stops the Python agent process and starts a
   fresh one. ~3-5 seconds. Spinner + "Restarting gateway…" shown.
5. Restart completes. Toggle re-enabled. New gateway is wearing
   (or not wearing) the sandbox profile.
6. On error: optimistic flip reverted; inline error shown.
```

The auto-restart is essential because **sandbox profile is applied
at process spawn time**. macOS / Linux don't allow attaching or
detaching a sandbox profile from a running process — the kernel
won't let a process escape its own sandbox at runtime (the whole
point). So toggling sandbox on / off requires a fresh spawn. Doing
the restart automatically matches the user's mental model of a
switch that "just works"; without it the toggle would persist a
config change but the running agent would still be in its old
sandbox state until the next manual restart.

## Why one master switch instead of per-category

Earlier design (commit `fc49561` reverted it) had per-category
capability toggles:

```
☐ AWS credentials   [Off]
☐ SSH keys          [Off]
☐ GCP credentials   [Off]
☐ macOS Keychain    [Off]
☐ Homebrew install  [Off]
```

Idea: an operator who needs `aws s3 ls` to work could allow
`~/.aws` without turning the whole sandbox off.

We dropped it for two reasons:

1. **Most operators don't know what `~/.aws` is.** Especially the
   non-developer audience that Flowly Desktop targets. Asking them
   to reason about credential paths individually leads to:
   - Flip-all-on out of caution → defeats the deny list.
   - Skip the screen confused → arrive at the desired-by-default
     state but by inattention.

2. **The master switch is the right granularity.** Operators who
   hit a real friction (`aws s3 ls` fails) make one deliberate
   choice — accept the unsandboxed baseline. The deny list itself
   stays fixed in code (`sandbox/policy.ts`); operators don't manage
   credential categories.

If a more granular UI ever becomes warranted (enterprise audience
with admins comfortable with credential paths), the
`buildDefaultPolicy()` function in `policy.ts` would need to grow
back its `capabilities` parameter. We removed it cleanly in
`fc49561` so the path is still tractable, just not present in main.

## Config field

```json
{
  "security": {
    "sandbox": true
  }
}
```

Single boolean. Absence or any non-`false` value means enabled
(default-on). Only an explicit `false` disables.

The shape mirrors the env-var semantics — `FLOWLY_SANDBOX` unset
also means enabled, only explicit `"0"` / `"false"` / `"off"` /
`"no"` disables. Two layers, same precedence rule. See
[`sandbox-architecture.md`](sandbox-architecture.md) for how the
env var and config field are unioned in `wrapWithSandbox()`.

## IPC wiring

Three files for the type chain:

```
flowly-desktop/src/main/local/flowlyai-service.ts:267
  → FlowlyAIConfig.security?: { sandbox?: boolean }
  → readConfig() returns it
  → writeConfig() merges updates.security into config.security

flowly-desktop/src/preload/index.ts:117
  → mirrors the type so renderer typecheck passes

flowly-desktop/src/renderer/src/types/electron.d.ts:164
  → same shape on the renderer side
```

No new IPC handler needed. The existing `flowlyai:write-config` and
`flowlyai:read-config` handlers carry the security sub-object on
the way through. The Settings UI just calls
`window.electronAPI.flowlyai.writeConfig({ security: { sandbox: false } })`
and the existing plumbing handles it.

The restart uses the existing `flowlyai:restart-gateway` IPC at
`flowlyai-handlers.ts:508` — same one bound to the "Restart" button
elsewhere in the UI.

## What used to be at Settings → Security

There was briefly a "Security" entry in the app-level Settings
sidebar (`Settings.tsx`), with a `SecuritySection` component that
mirrored what's now in `SandboxCard`. Commit `61a185a` added it;
commit `c9f5284` moved it to the Dashboard and removed it from
Settings.

The move was the user's call: "ayarlardan kaldıralım… dashboard'da
settings içinden". The dashboard placement turned out to be the
better fit — co-located with the rest of the bot-management UI and
small enough not to warrant its own sidebar entry.

## Known UX wart on Windows

The toggle renders on every platform, including Windows. On Windows
the underlying launcher is `NoSandboxLauncher` (because
`WindowsSandboxLauncher.isSupported()` returns `false` — native
sandboxing is on the roadmap, not shipped). So:

- Toggle is "On" by default → looks like sandbox is active.
- User can flip it → config writes, gateway restarts.
- After restart → agent runs **exactly as before**, unsandboxed,
  because `wrapWithSandbox()` short-circuits when the launcher's
  `platformName === 'none'`.

The toggle is **silently a no-op on Windows.** No crash, no visible
error, but the user thinks sandbox is enforced when it isn't.

Decision (intentional, not pending): leave it as-is. Rationale:

- Windows is a small slice of the Flowly user base today; native
  sandbox is roadmap.
- Hiding the toggle would surface the gap; we'd rather not draw
  attention to it before we have a fix.
- The honest framing lives in `SECURITY.md` §2.2 and
  [`sandbox-architecture.md`](sandbox-architecture.md) for any
  operator who reads the policy.

If we ever ship Windows native sandboxing (AppContainer or Job
Object + Restricted Token route — see
[`sandbox-architecture.md`](sandbox-architecture.md#windows--stub)),
this wart disappears automatically because the launcher will start
reporting `isSupported() === true`.

If the decision to leave the toggle visible ever feels wrong (e.g.
a Windows user files a "sandbox isn't working" issue), the fix is
~15 lines of code in `SandboxCard`:

```tsx
const isWindows = window.electronAPI?.platform === 'win32'

// Option A — hide the card entirely on Windows
{!isWindows && <SandboxCard />}

// Option B — keep it visible but disable with explanation
<button disabled={loading || busy || isWindows} ... />
{isWindows && (
  <p className="text-xs text-amber-600">
    Sandbox is not yet available on Windows.
    For stronger isolation, run Flowly under WSL2.
  </p>
)}
```

## Failure modes

| Scenario | Behaviour |
|---|---|
| Config read fails on mount | Toggle defaults to "on" (matches the fail-safe spawn-side behaviour) |
| `writeConfig` returns false | Optimistic flip reverted; inline error: "Failed to update sandbox setting" |
| `restartGateway` throws | Optimistic flip reverted; inline error with the exception message |
| User toggles during restart | `busy` state disables the toggle until restart completes |

## Testing

The toggle behaviour isn't covered by automated tests today —
`SandboxCard` is React component logic depending on
`electronAPI.flowlyai.*` IPC, which would need either a mock-heavy
unit test or a Playwright end-to-end run. The boundary tests
([`testing.md`](testing.md)) cover the **policy** that the toggle
flips between, not the toggle UI itself.

The interactive verification is documented in commit `c9f5284`:

1. Open Dashboard → Settings → Security card.
2. Toggle off; observe "Restarting gateway…" spinner.
3. After ~3-5 seconds, run `ps -ef | grep "sandbox-exec.*flowly"` —
   should show nothing (sandbox disabled).
4. Have the agent try a denied path (`cat ~/.ssh/known_hosts`) —
   succeeds (unsandboxed).
5. Toggle back on, restart, repeat — `~/.ssh` access denied.

## Related commits

| SHA | What |
|---|---|
| `61a185a` | Original Settings → Security sidebar entry (later moved) |
| `c9f5284` | Move to Dashboard SettingsTab + auto-restart on toggle |
| `9d8dd73` | Config field + IPC plumbing (the layer the UI writes to) |
| `fc49561` | Drop per-category capability toggles (kept the master switch) |
