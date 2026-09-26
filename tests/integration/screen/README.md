# Linux screen integration runner

This disposable runner exercises the product launcher and supervisor against
Xvnc, Openbox and sandboxed Chromium. It uses generated local test credentials;
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

Not yet certified: relay binary display transport, integrated Desktop/noVNC,
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
viewing-only, even when the viewer owns human control. Public lifecycle remains unfinished.

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
