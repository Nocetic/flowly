# Testing — Boundary-Aligned Test Suite

How the security boundaries are pinned by automated tests. What
regression each test catches. How to run them locally.

## Philosophy

The tests are not generic unit tests. They are **boundary cover**
— specific assertions tied to the security properties documented in
`SECURITY.md`. A regression that drops a credential from the
blocklist, weakens the GHSA passthrough guard, or empties the
filesystem deny list passes typecheck and runtime; only these tests
catch it.

Properties pinned (not implementation details). A refactor that
keeps the contracts green passes without churn; a refactor that
silently weakens a boundary fails immediately.

## Coverage by file

### `flowlyai/tests/test_env_scrub.py` — 44 tests

Covers `flowly/exec/env_scrub.py` (subprocess env scrubbing) and
`flowly/exec/env_passthrough.py` (GHSA-guarded passthrough).
Detailed walk-through in [`subprocess-env-scrub.md`](subprocess-env-scrub.md#testing).

| Class | Catches |
|---|---|
| `TestBlocklistStrips` | A blocklist entry that disappears (someone removed `OPENAI_API_KEY` from the set). Parameterised over 18 names. |
| `TestUserOwnedPreserved` | An over-zealous "let's add `_KEY$` regex" PR that would accidentally strip `AWS_SECRET_ACCESS_KEY`, `GH_TOKEN`, etc. 11 commonly-used user-owned names. |
| `TestForcePrefix` | Force-prefix escape hatch breaks (bypass doesn't work) OR leaks (prefix appears in child env). |
| `TestGHSAGuard` | A plugin can register `OPENAI_API_KEY` as passthrough and defeat the scrub. Pins the GHSA-rhgp-j443-p4rf upstream precedent. |

### `flowlyai/tests/test_sandbox_cli.py` — 30 tests

Covers `flowly/sandbox/cli_wrap.py` (Python CLI self-wrap).

| Class | Catches |
|---|---|
| `TestGateEnvVar` | A wrong env-var value gets through (e.g. `FLOWLY_SANDBOX=No` should disable, parameter sensitivity). Plus the recursion guard. |
| `TestGateConfig` | Config corruption / missing-file should fail-safe to enabled. Catches a "let's default to off on error" regression. |
| `TestSBPLProfile` | Deny list emptied, write block ordering swapped, version line removed, ssh / aws / Keychain not in the deny block. |
| `TestSBPLEscaping` | A path with `"` or `\` not escaped → SBPL parse fails. Newline in path accepted → tokenizer crash. |
| `TestSandboxExecIntegration` | The whole pipeline. **Actually runs `/usr/bin/sandbox-exec`** with the generated profile and verifies `~/.ssh` listing is denied. macOS only. |
| `TestBwrapArgs` | Linux argv shape changed (someone swapped `--share-net` for `--unshare-net`, removed `--die-with-parent`, …). |

### `flowly-desktop/tests/main/sandbox-policy.test.ts` — 29 tests

Covers `flowly-desktop/src/main/local/sandbox/policy.ts`, `macos.ts`,
`linux.ts` (desktop-side launcher).

| Group | Catches |
|---|---|
| `buildDefaultPolicy` | Deny list emptied, allow-write list missing `~/.flowly`, `allowNetworkHosts` accidentally populated. |
| `SBPL profile` | Mirror of `TestSBPLProfile` but on the desktop side — different code path emits the same profile shape. |
| `sbplString` | TypeScript escaping correctness (parallel to Python tests). |
| `shellSingleQuote` | POSIX single-quote escaping for the inner command. |
| `buildBwrapArgs` | bwrap argv shape — same checks as Python `TestBwrapArgs`. |

### `flowly-desktop/src/renderer/src/pages/Dashboard/SkillsTab.risk.test.ts` — 18 tests

Covers the plugin marketplace risk classifier.

| Group | Catches |
|---|---|
| `classifyPluginRisk — high` | `pre_llm_call` / `post_llm_call` / `transform_tool_result` no longer escalate to high. Plus escalation rules (high beats medium, tool override beats nothing). |
| `classifyPluginRisk — medium` | `pre_tool_call` / sensitive built-in tool overrides no longer flagged. |
| `classifyPluginRisk — low` | Innocuous hooks get false-positive flagged. |
| `classifyPluginRisk — reasons` | Reason strings stop mentioning the specific hook / tool name (UI would show generic warnings). |

## Total

**121 tests** across the four files, all green on every commit.

```
$ uv run pytest tests/test_env_scrub.py tests/test_sandbox_cli.py -v
=========== 74 passed in 0.81s ===========

$ npx vitest run
Test Files  6 passed (6)
     Tests  106 passed (106)
```

(The TypeScript total is 106 because Flowly Desktop has 59 pre-
existing tests in other files. The new sandbox tests are 47 of
those 106.)

## Running locally

### Python tests

```
cd ~/flowlyai
uv run pytest tests/test_env_scrub.py tests/test_sandbox_cli.py -v
```

The `TestSandboxExecIntegration` class is automatically skipped on
non-macOS hosts via `pytest.mark.skipif`. On macOS, it actually
exec'es `/usr/bin/sandbox-exec`, so the result reflects real kernel
behaviour, not a model.

### TypeScript tests

```
cd ~/flowly-desktop
npx vitest run
```

Or watch mode:

```
npx vitest
```

The vitest config at `vitest.config.ts` runs Node environment by
default; tests that touch React components would need a
`// @vitest-environment jsdom` pragma, but the sandbox tests don't
— they're pure logic.

## Running on CI

There is no CI workflow yet for the sandbox tests specifically. The
intent is:

- macOS runner — runs Python + TypeScript suites, exercises the
  `TestSandboxExecIntegration` class against real `sandbox-exec`.
- Linux runner — runs Python + TypeScript suites; `bwrap` integration
  is skipped (no end-to-end test, just argv-shape checks). Could be
  upgraded to a real bwrap exec test on a Linux CI image that has
  user-namespace support.

Adding the workflow is one of the remaining items in the security
roadmap (see [`README.md`](README.md)). It's not blocking — the
suite runs cleanly locally and any developer working on sandbox
code can run it before pushing.

## What the tests don't cover

Honest disclosure of gaps:

- **Settings UI toggle behaviour.** The `SandboxCard` component's
  optimistic flip / restartGateway sequence isn't unit-tested.
  Would need IPC mocking. Manual verification documented in
  [`settings-ui.md`](settings-ui.md).

- **Plugin loading + sandbox interaction.** Tests cover the policy
  generation and the env scrub primitives separately, not the
  end-to-end "plugin tries to do something, sandbox denies it"
  scenario. The disk-cleanup bug (commit `b6077dd`) was caught by
  manual exercise, not a test.

- **End-to-end Linux bwrap.** Argv shape is pinned. The actual
  bwrap exec is not, because the dev environment is macOS. Would
  need a Linux CI runner.

- **Windows.** `WindowsSandboxLauncher` returns
  `isSupported() === false` — there's nothing to test until a real
  implementation lands.

- **Network egress.** Not implemented (see
  [`network-egress.md`](network-egress.md)), so no tests for it.

- **Approval gate.** The exec approval flow is mature and pre-
  existed this security work; it has its own tests
  (`tests/test_*` in flowlyai). Not enumerated here.

## Adding new boundary tests

When you add a new security property, the test pattern is:

1. **Pin observable behaviour.** Assert what an attacker sees, not
   what internal data structures look like. Example: assert that
   `is_env_passthrough("OPENAI_API_KEY") is False` after attempted
   registration — not that the internal set doesn't contain the
   name.

2. **Parameterise over the surface.** If the property says "every
   X in the blocklist is stripped", parameterise the test over
   every blocklist entry. A regression that removes one entry
   should be a specific test failure, not a generic "the strip
   doesn't work" failure.

3. **Test both directions.** "X is stripped" AND "Y is preserved"
   — both halves of the contract. If you only test the strip
   direction, an over-zealous regression that strips everything
   passes.

4. **End-to-end test if cheap.** Where a real OS primitive can be
   exec'd in CI (sandbox-exec on macOS, bwrap on a Linux runner),
   prefer that over a "we generate the right string" test. The
   right string against a wrong kernel is the worst failure mode.

5. **Document what the test catches.** A test class docstring
   should say "this catches X regression", so a future engineer
   looking at the failure knows what they broke.

## Related commits

| SHA | What |
|---|---|
| `f4d05b5` | `test_env_scrub.py` — env scrub + GHSA passthrough |
| `9221cc9` | `test_sandbox_cli.py` — CLI gate + SBPL + bwrap + e2e |
| `d8af2af` | TypeScript vitest — policy + risk classifier |
