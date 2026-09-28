# Linux screen integration runner

This disposable runner exercises the product launcher and supervisor against
Xvnc, Xfce with a three-launcher dock, and sandboxed Chromium. It uses generated local test credentials;
no external accounts or model calls are needed.

```sh
docker build -t flowly-screen-test:bookworm -f tests/integration/screen/Dockerfile tests/integration/screen
docker run --rm --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_LINUX_TEST=1 flowly-screen-test:bookworm \
  python -m pytest tests/integration/screen/test_xvnc_browser.py -q \
    --basetemp=/tmp/screen-tests -p no:cacheprovider
```

Use an isolated Linux VM for this runner. The seccomp setting is specific to
this disposable test container: Docker's default policy denied Chromium's
namespace creation in the verified environment. Chromium's own sandbox stays
enabled. Neither the product nor the test uses `--no-sandbox`. This is not a
production container recipe or a claim that the default Docker policy works.

Verified on 26 September 2026 with a dedicated Colima Linux arm64 VM, Debian 12
container, TigerVNC 1.12, distro Chromium and Playwright 1.58.0:

- Raw RFB pixels and actual RFB mouse/keyboard input reach the headed browser.
- Human login changes the same cookie jar visible through an independent CDP
  attachment; a read-only RFB client's injected key is filtered.
- Two concurrent profile runtimes have different displays/browser directories
  and isolated cookies. This is application separation, not OS isolation.
- A detached runtime survives the process that started it; two controllers
  adopt the same generation instead of starting duplicate browsers.
- Human control survives reconnect and blocks stop; only explicit release
  allows shutdown.
- Explicit stop closes Chromium through CDP before stopping Xvnc so persistent
  cookies survive restart. Killing the browser directly failed this test.

Not yet certified: relay binary display transport, packaged Desktop clients,
root-host runtime provisioning, orphan recovery, host
resource limits/idle policy, x86_64, other distro versions. The launcher requires
an unprivileged Linux account and installed OS dependencies; it does not change
system packages or privilege settings automatically.

The optional integration tests skip unless `FLOWLY_SCREEN_LINUX_TEST=1`. The
normal test suite therefore does not by itself prove live screen behavior.

## Full Core gateway and CLI runner

The second image installs Core's **locked** dependencies, including the optional
`screen` extra, without installing a copied version of Core. The read-only mount
and `PYTHONPATH` keep the test source tied to this checkout.

```sh
docker build -t flowly-screen-gateway-test:bookworm -f tests/integration/screen/Dockerfile.gateway .
docker run --rm --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_LINUX_TEST=1 -e FLOWLY_SCREEN_GATEWAY_TEST=1 \
  -e FLOWLY_HOME=/tmp/test-flowly-home flowly-screen-gateway-test:bookworm \
  python -m pytest tests/test_screen_authority.py tests/test_screen_rfb.py \
    tests/test_screen_tickets.py tests/test_screen_viewers.py tests/test_screen_launcher.py tests/test_screen_execution.py \
    tests/test_screen_gateway_binding.py tests/test_screen_browser_provider.py \
    tests/test_screen_cli.py tests/test_screen_profile_binding.py tests/test_screen_display_gateway.py tests/test_screen_display_watch.py \
    tests/test_screen_input_broker.py tests/test_screen_input_service.py tests/test_screen_observers.py tests/test_screen_status.py \
    tests/test_screen_handoff.py tests/test_screen_run_checkpoint.py tests/test_screen_agent_handoff.py tests/test_screen_resume_rpc.py \
    tests/test_screen_lifecycle.py tests/test_screen_lifecycle_rpc.py \
    tests/integration/screen/test_xvnc_browser.py -q \
    --basetemp=/tmp/screen-tests -p no:cacheprovider
```

This adds the real authenticated Gateway WebSocket → browser tool → headed
Chromium path, wrong-profile rejection, same-run target preservation across
human interaction, and actual `flowly screen start/status/stop` subprocesses.
The gateway test runs the actual agent loop with a deterministic local model
stub. A barrier holds its first response while the user takes control and signs
in through a separate CDP attachment to the same Chromium session. Plain handback
keeps the task waiting. Authenticated `display.run.resume` triggers a fresh page
read, discards the stale planned click and continues the original run/session.
No external model, account or Desktop UI is involved in this Linux test.
The profile routing tests use real authenticated primary and named-gateway
WebSockets with stubbed browser I/O. They verify closed grant validation,
destination generation checks, and original run identity across the broker.
The expanded Linux screen suite passed **291 tests** on the environment above.
The authenticated direct display endpoint also delivers real Xvnc pixels for
the default and named profiles. Its single-use tickets are bound to the host,
profile and runtime generation; gateway credentials and tickets use headers.
Synthetic socket tests cover input filtering, replay rejection, profile identity
changes, observer limits and teardown. New version-two supervisors support basic
public input through the acknowledged broker. Legacy raw RFB streams remain
viewing-only, even when the viewer owns human control. Public direct lifecycle is
now implemented as durable operations, described below; installation and recovery
remain unfinished.

The input broker now uses a separate private RFB connection that never requests
pixels. It negotiates Fence support and requires a matching BlockBefore response
after input and after releasing pressed keys/buttons. The real Linux test holds
Shift and a mouse button in Chromium, then checks their release and preservation
of the original run lease through handback. Synthetic tests delay/drop/change
acknowledgements and verify that no agent action or reclaim can bypass drain.
Clipboard data containing a fence-shaped payload cannot substitute for an ACK.
The durable authority schema is now version two, with exact version-one upgrade
and a persistent input reservation containing no keystrokes or clipboard text.

This proves the Xvnc input processing boundary, not arbitrary asynchronous website
effects or completed production task resume. The supervisor now owns the broker
through a private, bounded Unix request service. Public display control RPCs
delegate to that same service. Gateway worker exit preserves the same input handle and
sequence. Repeating the last acknowledged sequence with the same payload returns
its acknowledgement without reapplying input; changed/older sequences fail.
The service keeps a bounded cache of completed handbacks. Replaying an old
handback cannot release a newer holder; an evicted handle fails without mutation.
The real Linux test exits a separate worker after pressing Shift, adopts its
broker from another client, deduplicates the retry and releases Shift. New
runtime manifests advertise `inputServiceVersion: 3`; an already-running older
supervisor does not gain this capability by gateway reconnect alone.

The supervisor also tracks capture leases across gateway workers. Takeover first
fences agent actions, then revokes other viewers and waits for the gateway to
close both its RFB source and public WebSocket before confirming human control.
A live worker that cannot acknowledge teardown leaves takeover pending. The
holder's original capture remains open. The real default/named-profile gateway
tests exercise this with two Xvnc streams. Public WebSocket close waits are
bounded independently of remote close acknowledgements; an idle client must not
hold a healthy gateway's capture lease open. Runtime manifests now additionally
advertise `observerServiceVersion: 3`. A new attachment for the same viewer
retires the previous input source. It cannot regain input permission after the
newer connection disappears. Reattachment drains the previous broker before
admitting input from the new source, without releasing human ownership.

Authenticated `display.control.acquire/release/reclaim` require a server-issued
viewer capability, pinned host/profile/bot/generation, expected epoch and operation
ID. Hashed resume credentials survive gateway restart and expire after 24 hours
without authenticated use; expiry never releases human control. A new app can
create a fresh capability using `display.viewer.create`, then explicitly reclaim.
Creating that capability does not grant observation while someone else controls
the browser. The supervisor serializes transitions with input and keeps bounded
operation receipts; old replies never reapply transitions. Pending acquisition
needs a fresh operation/current epoch to confirm after an agent action completes.
The default/named-profile real-Xvnc tests now acquire/release through authenticated
public RPC. Broker tests prove release/reclaim drains held input before handback.
Version-three supervisors add authenticated `display.viewer.close`. A durable
close intent revokes new captures, late control requests and input before
waiting for this viewer's capture EOF and draining held input. Only then is
human ownership released. Unresponsive captures retain ownership and return
`CLOSE_PENDING`; a retry must use the same operation/epoch. Bounded hashed
credentials retain close receipts across gateway reconstruction. A lost reply
or failed receipt write never lets a retry release a newer holder. Read-only
viewer closure never releases another owner's control. Closed receipts do not
consume active viewer capacity. Existing viewer databases migrate transactionally.
The real Linux gateway tests close with Shift held, verify key-up and browser
survival, and replay the same close. Synthetic tests also cover delayed admission,
missing capture EOF, stale epochs, receipt eviction and lost persistence replies.
The public status projection now carries a durable per-profile `statusRevision`,
independent of the control revision/epoch. Sampling and assignment are serialized
across gateway workers; unchanged snapshots keep their revision, and gateway
reconstruction resumes the sequence. The real gateway tests verify stable repeated
status and advancement after public control transitions. Phase-only failure and
out-of-order sampling are covered by the synthetic/sequence tests. Authenticated
direct `display.subscribe/unsubscribe` now return a pinned snapshot and follow it
with bounded status/control/runtime-failure events and scoped heartbeats. No
pixels or viewer credentials travel in these events. Subscriptions are bound to
the originating connection and verified profile identity, limited to four per
connection/sixteen per gateway, and removed on disconnect, identity replacement,
state rollback, slow delivery or explicit unsubscribe. Cleanup never releases
human control. Pending snapshots cannot recreate a subscription after shutdown.
The gateway/sequence tests passed **41 tests** on macOS; the expanded Linux runner
with subscriptions passed **259 tests**, including ten real-runtime tests.
Core's full default suite passed **6,382 tests**, with 25 skipped and 12 deselected.
Desktop's direct Screen sidebar, profile-tab navigation and status event
consumption are wired. Its synthetic-host Electron test now covers a pushed
phase-only failure while hidden and subscription cleanup through the actual
preload. Combined real Linux/Desktop, lifecycle, original-run resume and relay
remain incomplete.

The `epoch-u64-v1` input protocol is selected with `X-Flowly-Screen-Input`.
Each client binary frame starts with an unsigned big-endian 64-bit control epoch
(limited to safe integers); zero is observation-only. Server pixel frames remain
ordinary RFB. Input is separated from capture metadata and sent only to the
supervisor. Stale epochs are dropped; partial RFB messages cannot cross an epoch
boundary. Basic input batches retain broker acknowledgement/sequence semantics.
The real gateway tests type into Chromium, hand back with Shift held, reject
input from the previous epoch both before and after reacquisition, then continue
in the same input field through a fresh attachment. The test waits for actual
DOM focus between click and typing: an Xvnc input ACK does not imply completion
of asynchronous browser focus handling.

Desktop uses a constant epoch per attachment. After a control transition it
creates a fresh bridge/noVNC connection, retaining the same viewer capability
and VPS browser. Looking up the latest epoch as old bytes arrive would retag
queued input; this protocol deliberately avoids that. Fullscreen/resize does not
change ownership and must retain the existing connection.

Supervisor death or unknown input outcome retains human ownership and requires explicit recovery;
automatic timeout/disconnect release is intentionally absent. Extended pointer,
QEMU key, resize and extended clipboard messages are not admitted by this basic
input connection. Framed streams strip their negotiation and reject unsolicited
extended input. Plain key/pointer/paste remain supported. See the
[RFB Fence protocol](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst#748clientfence).

The `screen` Python extra adds Playwright without downloading a bundled browser;
the runtime attaches to the installed distro Chromium. CLI lifecycle commands
use the ordinary selected `FLOWLY_HOME`/`--profile` context. They expose no CDP
port, Unix socket path or raw viewer capability. Mac/Windows screen lifecycle is
unsupported; their normal CLI agent functionality remains independent.


## Origin-run checkpoint foundation

`RunHandoffs` now persists a versioned per-profile origin session/run mapping,
verified process incarnation, handoff ID, reason, generation, control epoch and
explicit resume state. Plain control release does not authorize continuation.
Resume receipts are bounded to 128; a historical receipt never authorizes another
handoff. A missing/corrupt initialized store fails closed. A dead/replaced process
requires recovery, and another live gateway worker may request a resume but
cannot execute the original continuation. Unchanged checkpoints do not rewrite
the SQLite state.

`ScreenRunCheckpoint` can bind this state to `ScreenExecution` action admission.
After a takeover, stale planned actions cannot execute even if control has since
returned to the agent. An explicit resume first takes a new `read_page`
observation in the original run, clears prior references through the provider's
epoch transition and verifies the epoch again before returning it. It never
replays a stored write. Cancellation keeps human ownership; commands that outlive
a cancelled waiter still drain before the run lease is retired. An unknown
backend outcome retains its durable action fence for explicit recovery.

The foundation-only suite initially passed **273 tests**, with **62 affected
tests** after checkpoint-schema/persistence refinements. Those tests use actual
SQLite/authority/provider admission with mocked browser operations and process
identity. Production activation and real-browser evidence are described below.


## Production direct-run integration

The gateway now reserves the selected browser and creates its checkpoint before
calling the chat host. The agent loop awaits the checkpoint before a model call
and rechecks it after a model response, preserving usage accounting while
discarding a stale planned response. Cancellation interrupts the wait and marks
the continuation terminal without releasing human ownership. Cancellation during
pending checkpoint creation also retires a late successful registration.
A failed second chat admission cannot release the original run's browser lease.

Authenticated, pinned `display.run.resume` requires a valid viewer capability,
generation, handoff ID, epoch and idempotency key. It takes the origin session/run
from the persisted record; caller-supplied origin targets are rejected. Status
includes a closed public handoff projection. A resume ACK means the request was
recorded, not that execution has resumed. The live origin process must still
complete a fresh, epoch-fenced observation. Process death surfaces recovery;
explicit orphan recovery remains separate unfinished work.

The Linux test exposed that an agent may be container PID 1. Handoff liveness now
allows reading its kernel identity, while runtime signal-target validation still
rejects PID 1. The dedicated test covers both sides of that distinction.

After this wiring, the complete default Core suite passed **6,414 tests**, with
25 skipped and 12 deselected. The expanded real Linux suite passed **291 tests**,
including the actual agent-loop/RPC/Chromium continuation flow described above.
Targeted Ruff and diff checks passed. Desktop integration has its own Electron and unit-test evidence in its worktree.
The direct primary-profile Desktop-to-Linux flow is now verified below. Relay,
additional platform/profile cases and recovery still require verification.


## Actual Desktop-to-Linux login and continuation

`desktop_fixture.py` is a disposable TLS gateway host for Desktop's
`tests/smoke/run-screen-linux.sh`. Run the script from the Desktop worktree with
the Core checkout path; see its smoke README for the command. Build the images
above first. The fixture uses the container user's normal `~/.flowly` directory
so production identity resolution is exercised without patching profile globals.

The passing run uses the actual Electron panel, production preload, trusted-frame
IPC, captured gateway lease, gateway client, public display RPC/stream, Xvnc input
broker and sandboxed Chromium. Electron mouse and keyboard events enter the
noVNC canvas to fill a generated test code and click Sign in. Inspection through
an independent CDP attachment is read-only after setup; it confirms the page and
cookie and provides coordinates. The Desktop test also waits for the changed
signed-in pixels before capturing its screenshot. Plain release keeps model
calls at one; explicit continuation completes the original run/session after a
fresh observation. The stale planned click is never dispatched.

The shell runner creates a short-lived self-signed fixture certificate, keeps TLS
verification enabled, publishes only a loopback port and removes its container
and temporary certificate directory on exit. No system trust is modified. The
expanded Docker security setting is test-only; it is not a deployment recipe.
The generated fixture token is not a product credential.

Scope limits: the local model is deterministic and the page/account is synthetic;
the browser, display, input, TLS and gateway are real. This single-profile gateway
does not enable profile hosting, so Desktop exercises its Main Flowly fallback
when `profiles.list` returns `PROFILE_HOST_UNAVAILABLE`. The upgraded smoke now
uses the production composer: select Server browser, wait for identity discovery,
then click Send. The actual chat hook, preload and main handler send the grant;
opening Screen does not select it or start a task. The screenshot includes the
selected browser in the composer. This does not certify named-profile navigation,
relay, packaged Windows/macOS, crash recovery or third-party authentication.

Browser grants now require immutable `botId` alongside host/profile/runtime
identity. `display.capabilities`/status advertise `browserGrantVersion: 2`;
Desktop refuses an older host for explicit server-browser selection. Core checks
the identity again before claiming the run, closing replacement between chat
admission and execution. Stream and input protocol versions remain unchanged.
The expanded Linux suite passed **294 tests**, with two warnings, after this
change. Complete Core/Desktop suite results are in Desktop's progress document.


## Durable public start and stop

`display.start` and `display.stop` require `profileId`, `expectedHostId`,
`expectedBotId`, `generation` and `operationId`. Only start accepts a null
generation, for a profile that has never created a runtime. The immediate result
is an operation receipt; `display.operation.status` takes the same identity pin
and operation ID (no generation parameter) to read pending/running/succeeded/
failed/recovery-required state. A receipt includes its requested generation and
result generation but never internal process or filesystem details.

Retries return the original receipt. Reusing an ID for another request is a
conflict. Runtime mutation checks identity and generation under the lifecycle
lock; a stale request cannot adopt or stop a replacement. One active operation
per profile and two workers per gateway process bound concurrency. The receipt
log retains 128 terminal operations; generation preconditions still fence an old
mutation after pruning. A dead worker or an outstanding operation past its
120-second observation deadline requires recovery and is never replayed. This
deadline does not assert cancellation or permit another overlapping operation.
Explicit recovery remains an open requirement.

Stop fences authority before graceful process shutdown; public status reports
`stopping` during that interval, then `stopped`. Human ownership, active runs and
input reservations are not forcibly released. CLI behavior remains explicit;
the new optional generation/identity guards serve remote lifecycle admission.

The expanded real Linux package passed **313 tests**, with two warnings. New
primary and named-profile cases start actual Xvnc/Chromium through authenticated
RPC, disconnect/reconnect, query durable completion, stop, replay old receipts,
restart and reject a stale stop without affecting the other profile. Complete
Core suite: **6,434 passed**, 27 skipped, 12 deselected. The Desktop fixture now
waits for the real panel to start its screen; test-only background setup then
navigates the generated login page once. Inspection remains read-only.

## Startup resource diagnostics

`flowly screen doctor` inspects dependencies, available memory and free disk
without creating a runtime. Exit zero means the measured admission requirements
are met; exit one returns a typed reason and fixed resource issues. Authenticated
`display.doctor` adds a pinned host/profile identity to the same snapshot.

New launches measure again under a per-account startup lock. Initial floors are
1.5 GiB available memory and 1 GiB free on both the profile and temporary
filesystems. Memory uses host MemAvailable and the tightest visible cgroup v1/v2
ancestor headroom. Unreadable or inconsistent readings block new starts. These
floors are not a memory reservation, cache quota or calibrated minimum VPS size;
hidden cgroup ancestors and unrelated concurrent allocations remain limits.
A live runtime is adopted without applying the floors again and is never killed
because a later reading is low.

A separate disposable test verifies admission against an actual 1 GiB cgroup
limit, without launching a browser:

```sh
docker run --rm --memory=1g --memory-swap=1g \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_RESOURCE_LIMIT_TEST=1 flowly-screen-gateway-test:bookworm \
  python -m pytest tests/integration/screen/test_resource_limit.py -q \
    --basetemp=/tmp/screen-tests -p no:cacheprovider
```

The regular Linux CLI integration also executes doctor before a real start.
Synthetic proc/cgroup tests cover v1, v2, hybrid membership, ancestor constraints,
namespace/mount roots, missing counters, permissions, over-limit usage and disk
failures. Only the real constrained-container test proves the live kernel limit
path; synthetic tests are not certification of every distribution layout.

Verification on the dedicated Debian 12 arm64 runner: the expanded screen suite
passed 354 tests (four warnings), and the separate real 1 GiB cgroup test passed.
The Core full suite passed 6,475 tests (28 skipped, 12 deselected, 13 warnings).
CLI lock-timeout handling and three regression cases landed after full-suite
collection; the final CLI/launcher/resource subset passed 66 tests, including
concurrent profile startup budgeting. Targeted Ruff and diff checks passed.
The real Electron/TLS/Linux smoke also passed with the readiness card, explicit
start, login, same-run continuation and explicit stop. These results do not
certify the pending distro/architecture, packaging, relay or root-provisioning
matrix.

## Fixed system-package installation

The first installer recipe targets Debian 12 (arm64 and x86_64); the real image
verification currently covers arm64. Ubuntu's Chromium packaging and other
versions are not inferred from Debian. `flowly screen install-plan` is read-only
and returns a fixed package list, a plan fingerprint and a system-package scope.
The reviewed fingerprint must be supplied to `flowly screen install-system
--plan-id <planId> --operation-id <32-hex-id>`. Keep that operation ID to query
`flowly screen install-status --operation-id <same-id>` or safely retry a lost
reply. A new ID means a new explicit operation. `--ask-sudo` reads a hidden
password only from a terminal; there is no password argument or environment
variable. Without it, sudo is noninteractive. Root callers do not use sudo.

The elevated worker runs fixed stdlib source with `/usr/bin/python3 -I`; it never
imports Core or the agent's virtualenv as root. No caller-supplied package names,
commands, shell fragments or paths are accepted. Password input only goes to
sudo stdin, package output is discarded, and receipts contain fixed phase/error
fields. Assigning Python password references to None does not promise secure
memory erasure. An authenticated Desktop password path is still pending and is
not enabled by these CLI commands.

The root-owned `/var/lib/flowly-screen-installer` holds one host-wide flock and
at most 128 receipts, partitioned by installing UID. Receipts are deliberately
not pruned: forgetting an operation could replay it. Reading status never asks
sudo, invokes apt or rewrites an interrupted record. A running record with no
actual held lock is reported as recovery-required. Interrupted or partial
operations block new package work until explicit operator recovery; automatic
recovery/receipt maintenance remains to be implemented.

The worker checks `dpkg --audit`, updates package indexes with a 180-second
limit, then installs the fixed packages with a 600-second limit. Audit has a
30-second limit. Apt package-lock waiting and network retries are bounded;
failed index refresh, package failure and timeout have distinct public codes.
The recipe uses no-remove/no-upgrade and retains existing configuration files.
Distro dependencies can still be installed as needed. A timed-out package group
is terminated while its leader remains unreaped, preventing PID reuse before
the final group signal. An outer observation timeout does not imply cancellation
and is never permission to start another installation. Slow/uninterruptible
kernel I/O is outside a strict wall-clock guarantee.

This command installs **system packages**, not the `screen` Python extra or a
root-host runtime account. Source installations need the optional extra in the
same virtualenv as the agent (for example, the existing installer's uv environment
with the checkout's `[screen]` extra). Do not run the entire user-writable agent
installation with sudo to install packages. Root agents still report
`UNPRIVILEGED_RUNTIME_REQUIRED`; the installer never adds `--no-sandbox`.

The dedicated clean fixture has only Python/sudo and Core's locked Python
environment before the product installer runs. Its sudo password is generated
fixture data, not an external credential. No developer-host packages are changed:

```sh
docker build -t flowly-screen-installer-test:bookworm \
  -f tests/integration/screen/Dockerfile.installer .
docker run --rm --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_INSTALL_TEST=1 flowly-screen-installer-test:bookworm \
  python -m pytest tests/integration/screen/test_installer.py \
    tests/test_screen_installer.py tests/test_screen_install_worker.py tests/test_screen_cli.py \
    -q --basetemp=/tmp/screen-install-tests -p no:cacheprovider
```

The test rejects an incorrect sudo password, starts installation, verifies lock
contention, kills the unprivileged invoking process while the privileged worker
continues, observes the durable result, retries without reinstalling and starts
then stops real sandboxed Chromium. The Docker seccomp exception remains specific
to the disposable test VM, as described above.

Installer validation: the complete Core run passed 6,528 tests, 30 skipped,
12 deselected, 13 warnings. The final child-reaping reset and malformed/oversized
Unicode password checks were added after that run collected its tests; the final
installer/worker/CLI subset passed 62 tests on macOS and, together with the real
Linux process-group timeout case, 63 tests in the Linux fixture. The clean Debian
installation/disconnect/replay/browser-start run plus its then-current unit tests
passed 60 tests in 131 seconds. The earlier direct clean installation also passed.
Targeted Ruff and diff checks passed. The macOS CLI plan/help smoke reported an
unsupported install host without executing the Linux worker. The Desktop changed
only its engineering progress document in this increment; its prior screen UI
verification is not being relabeled as installer UI verification.


## Optional Python dependencies in the agent environment

`flowly screen install-python-plan` previews a fixed, hash-locked recipe without
creating state or launching a subprocess. `install-python --plan-id <planId>
--operation-id <32-hex-id>` installs in the current agent virtualenv;
`install-python-status --operation-id <id>` reads its durable receipt. These
commands do not use sudo, target system Python, restart the agent, or launch a
browser. Frozen builds require their own managed build update instead.

The bundled requirements contain the complete Playwright dependency closure,
with exact versions and Linux/pure wheel hashes checked against `uv.lock` by a
test. The worker disables dependency resolution, source builds, Python downloads,
configuration discovery and persistent uv caching. It uses the explicit PyPI
index and a minimal environment without inherited credentials. Verification
requires both exact package metadata and a fresh Playwright driver start/stop.
A changed environment reports `restartRequired`; restart coordination remains a
separate integration task and must preserve active tasks.

One private lock and at most 128 receipts belong to each virtualenv, shared by
its profiles. Accepted work survives loss of its invoking process. Same-ID
retries return the recorded result; interrupted or partially installed work
requires explicit recovery instead of automatic replay. Recovery and receipt
maintenance are still pending. Installation has a 300-second command timeout,
driver verification 15 seconds, and the caller a 330-second observation timeout;
timeout cleanup preserves the process-group leader until signals are sent.

The opt-in integration tests create disposable virtualenvs inside the Linux
fixture and leave the fixture's existing agent environment unchanged:

```sh
docker run --rm \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_PYTHON_INSTALL_TEST=1 flowly-screen-gateway-test:bookworm \
  python -m pytest tests/integration/screen/test_python_installer.py \
    tests/test_screen_python_install.py tests/test_screen_cli.py \
    -q --basetemp=/tmp/screen-python-install-tests -p no:cacheprovider
```

This final Linux subset passed **41 tests** in 17.87 seconds, including real
installation after caller loss, durable replay, fresh driver startup, and an
intentionally wrong wheel hash rejected with the actual hash-mismatch diagnostic.
The matching local Python installer/CLI subset passed 39 tests. This does not
certify a Desktop installation UI, root-host runtime provisioning or relay install
admission; `installSupported` remains false until the complete flow is connected.

The complete Core suite after this Python installer change passed **6,556 tests**,
with 32 skipped, 12 deselected and 13 warnings in 298.01 seconds. Targeted Ruff
and whitespace checks passed. Opt-in Linux evidence is recorded separately above.


## Authenticated installation review

The direct gateway now exposes read-only `display.install.plan`. Like doctor,
it requires an authenticated protected socket and expected host/bot identity.
Only profile/identity parameters are accepted; passwords, packages, commands and
runtime generations are rejected. The profile identity is checked again after
inspection, so deletion/recreation cannot publish a plan for the old target.
No screen runtime is constructed by this handler.

The combined plan contains system-package and agent-Python recipes plus the
root-runtime-account requirement. Its fingerprint binds both recipe IDs and the
host/bot/profile/screen identity. Live Python `upToDate` observations do not change
that fingerprint. This is a review identifier, not an execution capability.
`installPlanVersion: 1` advertises review support independently of installation:
`installSupported` and the plan's `applySupported` remain false until job admission,
root-runtime provisioning and the password/recovery flow are integrated.

The focused real gateway suite passed 32 tests covering the new plan, doctor and
existing display behavior. Default and named profiles, invalid pins/extra fields,
identity changes during inspection, authentication and transport rejection were
exercised. Desktop provides a dedicated trusted-frame IPC and captured gateway
lease for review; no plan/start button or password input is claimed by this step.

The final default Core suite passed **6,562 tests**, 32 skipped, 12 deselected,
13 warnings in 310.14 seconds. Desktop's full suite passed 3,882 tests (3 skipped),
main/preload and renderer typechecks passed, and the production build passed.
The real Electron/TLS/Linux smoke successfully read the combined plan through
preload/IPC/main and verified supported system/Python recipes without starting a
runtime in its first two runs. **The full smoke did not pass:** both runs timed
out at keyboard entry; a diagnostic follow-up timed out waiting for initial
screen pixels. One failure cleanup also terminated Electron with SIGTRAP.
The fixture now checks window focus, canvas hit testing and remote input focus
separately, and captures a failure screenshot. These failures remain unresolved;
previous successful full smoke evidence does not certify this current revision.


## Follow-up: live input and continuation smoke

The latest real Electron/TLS/Linux flow passed twice consecutively in fresh
containers after the smoke harness waited for actionable buttons. Previously
`HTMLButtonElement.click()` could silently do nothing while a button was disabled
between the ownership reply and stream reattachment. The harness now waits for
the button to be enabled and rejects a state change before clicking. It also
logs stage transitions and synthetic mouse coordinates; the existing explicit
window-focus, canvas-hit and remote-input-focus checks remain in place.

Both runs verified installation review without runtime mutation, explicit start,
real mouse/keyboard login, preserved cookies, plain release leaving the original
task paused, explicit continuation with a fresh observation and no stale-click
replay, terminal viewer acknowledgement and explicit stop. The signed-in screenshot
was visually inspected. Production code and timeout budgets were not changed.
The earlier one-off initial-pixel timeout and Electron failure-cleanup SIGTRAP did
not recur in these runs; their underlying causes are not established by this
harness correction. This evidence covers the direct primary-profile fixture on
this host, not the still-pending relay/platform matrix or installation execution UI.


## Durable installation orchestration

`ScreenInstallJobs` composes the fixed system-package and current-virtualenv Python
installers behind a bounded background worker. It is an internal admission target;
this increment does not enable installation RPCs, accept remote passwords or flip
`installSupported`/`applySupported`. The authenticated capability and UI execution
flow, root-host runtime account, restart coordination and recovery remain pending.

The environment owns a private SQLite job store beneath its existing screen
installer directory. Profiles in the same environment share its single active-job
rule; different environment processes are also fenced by persisted active records.
The privileged package worker still serializes package-manager access host-wide.
One process worker slot is available, with no unbounded executor queue. At most
128 receipts are retained without pruning, so forgotten IDs cannot replay installs.

A job is pinned to host/bot/profile/screen and the combined reviewed recipe. It
records system/Python phases, completed steps and restart requirements. Submission
returns after durable admission; recreating the calling controller does not own or
cancel work. Same-ID retries return the existing result even after package versions
change. A lost gateway process or expired observation deadline reports recovery;
it cannot automatically resume the pipeline. The underlying system/Python worker
receipt can still exist after that process loss. Recovering and reconciling those
receipts is separate required work.

Passwords are validated before admission, passed only in memory to the fixed
system installer, and excluded from the database and public result. Dropping a
Python reference is not a secure-erasure guarantee. Privilege errors only permit a
new attempt when the privileged worker has no accepted receipt; ambiguous output
or a partially completed pipeline remains recovery-required. Identity is rechecked
before mutation and between stages. Deadline expiry prevents beginning a new stage.
No browser or agent restart is performed by the installation job.

The clean orchestration fixture starts without the OS screen packages and without
Playwright/pyee/greenlet in its user-owned agent environment:

```sh
docker build -t flowly-screen-install-jobs-test:bookworm \
  -f tests/integration/screen/Dockerfile.install-jobs .
docker run --rm --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_INSTALL_JOBS_TEST=1 flowly-screen-install-jobs-test:bookworm \
  python -m pytest tests/integration/screen/test_install_jobs.py \
    tests/test_screen_install_jobs.py -q --basetemp=/tmp/screen-install-jobs-tests -p no:cacheprovider
```

The integration test requires prompt asynchronous admission, recreates its status
controller during installation, checks both underlying receipts, verifies replay
without executing another pipeline, checks password absence in the database, and
explicitly starts/stops sandboxed Chromium after installation has completed.


Validation for this increment: real clean Debian orchestration plus its then-current
unit suite passed **23 tests** in 185.62 seconds. The complete Core run passed
**6,584 tests**, 33 skipped, 12 deselected and 13 warnings in 294.28 seconds.
After that run collected tests, final deadline/capacity/private-file checks were
completed. The final job/system/Python installer subset passed **83 tests** on
macOS and **83 tests** in Linux, including 25 job tests. Targeted Ruff and diff
checks passed. The non-POSIX job entry explicitly reports an unsupported host;
these Linux installation tests do not certify a Windows installation runtime.


## Screen application environment boundary

Newly launched Xvnc, Openbox and Chromium processes receive an explicit environment
instead of inheriting the agent environment. It contains a fixed system PATH and
UTF-8 locale, profile-private HOME/config/cache/data directories, X authority/runtime
locations and the private Unix session-bus address. DISPLAY is supplied from Xvnc's
allocated display. Provider keys, cloud credentials, SSH agent sockets, proxy
credentials, Python/Node/loader configuration and unrelated parent variables are
not forwarded. Executables are resolved to absolute paths before sanitizing PATH,
so a host-configured executable location remains usable without inheriting all
agent PATH entries into desktop descendants.

Browser cookies stay in the existing explicit `browser` profile directory. Existing
running supervisors are not restarted by this change; the new environment applies
after an explicit stop/start. Inherited proxy settings are intentionally not a
screen configuration channel. An explicit per-screen proxy configuration, if needed,
requires a separate supported contract. This environment change does not provide
OS-level isolation between profiles running as the same Unix user.

Validation: the environment/launcher unit subset passed 34 tests. The final real
Linux browser/gateway/environment suite passed **47 tests** (2 warnings), including
separate profile cookies, human RFB input, original-run handoff, CLI/adoption and
public lifecycle. The environment test records only variable names and selected
profile paths immediately before exec of each actual application; it does not log
credential values. Reading `/proc/<pid>/environ` after startup was unsuitable for
this assertion because a process no longer exposed HOME there. Real applications
remain unmodified and run with Chromium's sandbox enabled. Root runtime account
provisioning and ownership/layout changes are still pending; root capability is
not enabled by this environment hardening.

Final verification for the application environment change: complete Core suite
**6,594 passed**, 34 skipped, 12 deselected, 13 warnings in 297.88 seconds; final
Linux environment/browser/gateway subset **47 passed**; targeted Ruff and diff
checks passed. The real Electron/TLS/Linux installation-review, start, mouse and
keyboard login, plain handback, explicit original-run continuation and terminal
stop smoke passed with the sanitized application environment. Desktop production
code did not change in this increment.


## Separate application and control sockets

New supervisors use socket layout version 2. Xvnc's RFB socket and X authority
live in a profile-specific application directory; input and observer brokers stay
in a separate agent control directory. Gateway streaming derives the RFB location
from the closed layout version and trusted runtime identity, never the manifest's
arbitrary socket string. Missing version means the legacy layout, so already-running
supervisors remain attachable until an explicit stop/start. Unknown versions fail
closed. Both directories remain current-account-owned and private in this increment;
this does not yet enable a root-owned agent's unprivileged application runtime.

Validation: 87 focused tests on macOS; 100 Linux runtime, input, gateway and
application-environment tests; complete Core suite 6,602 passed, 34 skipped,
12 deselected, 13 warnings in 294.70 seconds. The real Electron/TLS/Linux review,
start, mouse/keyboard login, handback, same-run resume and stop smoke passed.
Targeted Ruff and diff checks passed. Desktop production code is unchanged.


## Root runtime account provenance foundation

The internal fixed account worker can provision `flowly-screen` with a system UID,
a private primary group, locked password, `/nonexistent` home and `nologin` shell.
It accepts no caller-provided account fields or paths. A root-owned private receipt
under `/var/lib/flowly-screen-runtime` binds the UID/GID to this installation.
Pre-existing users/groups without that receipt are rejected. A durable pending
reservation precedes user creation; interrupted/failed creation requires explicit
recovery rather than automatic adoption or replay. A host account lock serializes
provisioners. Worker timeouts kill the unreaped child process group.

The read-only verifier checks trusted ancestors, no-follow private regular receipts,
UID/GID/name/home/shell agreement and absence of supplementary groups or group
members. Changed identities and unsafe files fail closed. These helpers are not yet
connected to CLI/RPC installation or runtime launch: root capability remains disabled
until application ownership, private D-Bus and runtime lifecycle integration are done.
No host accounts were created during development; actual provisioning ran only inside
a fresh disposable Debian container. The final Linux account suite passed 50 tests, including actual provisioning,
receipt replay, account privilege changes, concurrent admission and linked files.
The macOS verifier subset passed 39 tests. Targeted Ruff and diff checks passed.
The previously recorded full Core run preceded these independent helper modules;
no additional full-suite result is claimed for this increment.


## Root-owned agent with unprivileged screen applications

A Linux root agent can now start a screen when the fixed runtime account's private
provenance validates. Missing or changed account state still fails closed. The
supervisor, input/observer brokers and authority remain root-owned; private D-Bus,
Xvnc, Openbox and sandboxed Chromium run with the validated non-root UID/GID, no
supplementary groups, a private umask and `/` as their working directory. Root
applications resolve programs only from system directories and receive an explicit
environment, not the agent's PATH, credentials or root session bus.

Socket layout version 3 maps the profile to application data under the fixed
system runtime directory and application sockets under `/run`. A private root-owned
mapping binds a random storage ID to the source directory and runtime UID/GID.
It remains inaccessible to the application account. Parent directory entries are
root-owned; only home/config/cache/data/browser and socket leaves belong to the
application account. Existing unsafe ownership/modes/links are rejected rather than
recursively repaired. Copying a mapping to another profile is rejected; a recreated
profile gets fresh storage rather than inheriting the previous browser's login.
Browser port reads are bounded, no-follow, regular-file and owner checked. Root
resource admission measures the application-data filesystem. Current root profile
paths require trusted root-owned ancestors; arbitrary writable/symlinked homes are
not accepted. Profiles sharing the service account are not OS-isolated from one
another.

The account worker is still internal: CLI/RPC installation orchestration does not
yet provision it. The disposable root test fixture explicitly provisions the account;
this is not a completed Desktop installation flow. Profile deletion/restore cleanup,
crash recovery and migration of external application storage remain required before
production readiness. Existing normal-user runtimes keep their version-1/2 layouts.
Desktop diagnostics now accept account-integrity failures and the private D-Bus
dependency, showing the existing host-recovery message without private paths.

Validation: 115 focused Core tests; 64 real Debian account/layout/runtime tests,
including real UID boundaries, agent-directory denial, immutable parent entries,
link/FIFO/oversized port records, root agent PATH rejection, real RFB mouse/keyboard,
control handback, restart and persistent cookies. The normal-user Linux runtime and
gateway regression passed 100 tests. The real Electron/TLS/root-Linux smoke passed
installation review, explicit start, composer target, login, handback, same-run
continuation, terminal viewer ACK and stop, including the rebuilt Desktop. The
first root test's `/proc/<other-uid>/environ` probe was rejected by container process
permissions; environment construction is checked directly, while actual process
UIDs and sandbox flags are inspected without adding ptrace privileges. The final
strict D-Bus address validation was followed by another passing 64-test Linux run.
Desktop passed 77 focused tests and its full suite (3,889 passed, 3 skipped), build
and main/preload/renderer type checks. Initial transport tests needed socket
permissions; rerunning with those permissions passed. Targeted Ruff, shell syntax
and diff checks passed. The complete Core suite passed 6,645 tests, 59 skipped,
12 deselected and 13 warnings in 302.95 seconds. No merge, push or deployment
was performed.

Root fixture reproduction (fresh disposable container only):

```sh
docker run --rm --user 0 --cap-add SYS_RESOURCE --cap-add NET_ADMIN --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_ACCOUNT_TEST=1 flowly-screen-gateway-test:bookworm \
  python -m pytest tests/integration/screen/test_runtime_account.py \
    tests/test_screen_runtime_account.py -q -p no:cacheprovider
```

Root hosts guard each profile browser's DevTools port with nftables (only root may connect);
`NET_ADMIN` stands in for a real root host here. Without it a screen refuses to start with
`SCREEN_ISOLATION_UNAVAILABLE`.

The Desktop runner accepts `FLOWLY_SCREEN_ROOT_TEST=1` to select the root fixture;
without it the existing unprivileged fixture is unchanged. Its test-only bootstrap
requires the container marker and does not provision the user's own computer.

## Lightweight desktop candidate

The required final screen includes a dock, Terminal, Browser and Settings.
`Dockerfile.desktop` extends the disposable gateway runner with candidate Xfce
packages. It does not alter the product installer recipe or enable a production
desktop runtime. Build the gateway image above first, then:

```sh
docker build -f tests/integration/screen/Dockerfile.desktop \
  -t flowly-screen-desktop-test:bookworm .
docker run --rm --security-opt seccomp=unconfined --shm-size=512m \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e FLOWLY_SCREEN_DESKTOP_TEST=1 flowly-screen-desktop-test:bookworm \
  python -m pytest tests/integration/screen/test_desktop_session.py \
    tests/test_screen_desktop_defaults.py -q -p no:cacheprovider
```

The candidate uses the actual Xfce panel, window manager, settings daemon and
desktop, with a private D-Bus/Xvnc session. Its dock opens an interactive Terminal
and Settings window. Keyboard input executes a synthetic terminal command;
the browser launcher focuses the existing normal Chromium window instead of
its invisible helper window. A minimized window can be restored without a new
page or cookie jar. A changed font preference is written by Xfconf and survives
re-seeding. This test changes the preference through Xfconf, not by clicking the
Settings dialog controls. First-run configuration is executed with the
application UID, preserves existing preferences, rejects symlinks and quotes
browser/profile paths literally.

This is a component integration test, not a substitute for the real Desktop
viewer. Production supervisor/readiness integration, browser-last-window
closure/reopening, installation package admission, application process cleanup
and actual noVNC/Electron acceptance remain required. The final seed payload
must run after UID/GID drop on root hosts; system Python can execute its fixed
standalone source even when the agent checkout is unreadable by that account.
Do not run account-provisioning experiments outside a fresh disposable container.


## Browser close and reopen lifecycle

The application-account browser manager remains in a supervisor-owned process
group when Chromium exits. A private Unix datagram socket accepts only `open`;
dock clicks cannot supply a command, URL or profile. The fixed launcher focuses
an existing normal Chromium window or requests a new process in the same
persistent browser profile. Runtime status has a null CDP port while the browser
is closed; the provider reports `BROWSER_UNAVAILABLE` and reconnects after reopen.
The desktop generation and input ownership do not change during this transition.
Managed launcher code is refreshed on startup; user desktop preferences are not.

Verified in the disposable Linux runner: close Chromium, retain the desktop,
reopen from the launcher, reconnect the provider, preserve a persistent cookie,
then stop normally. Root-account runtime regressions also pass. This does not
certify browser crashes during an in-flight agent action, arbitrary terminal
child-process cleanup, resource limits, or packaged-client behavior.
