# Disposable screen latency experiment

This compares two capture paths against the **same real X11 desktop**:

- Xvnc → binary WebSocket → noVNC 1.7.0 canvas, quality 6 / compression 2.
- X11 capture without cursor (MSS) → aiortc software video encoder → Chromium video.

Both use the same loopback input WebSocket and `ScreenInputBroker`, including
its authority checks and Xvnc input fence. The fixture moves an unpressed mouse
60 times/second. A browser on the desktop paints the received pointer X coordinate
as a black/white marker. The viewer decodes that marker from its rendered canvas
or video and measures elapsed time on its own monotonic clock. No clock sync or
Playwright screenshot timing is involved.

The experiment is **not a product transport**. It deliberately has no account,
cloud registration, authentication UI, persistent session, TURN or external STUN.
The HTTP/WebSocket listener binds only to container loopback; do not expose it.
A normal container network interface is needed for ICE host candidates (ICE did
not connect with `--network none`). No ports are published, ICE server lists are
empty at both ends, and the viewer's HTTP requests are restricted to the fixture.
The experimental packages are installed only in this Docker image, not in Core's
package files or the user's VPS.

## Run

First build the existing `flowly-screen-gateway-test:bookworm` integration image
as described in `../README.md`. Run these commands from the Core worktree; set
`screen_desktop` to the existing Desktop worktree containing the locked noVNC
installation:

```sh
screen_desktop=/Users/hakanoren/flowly-desktop/.codex-worktrees/remote-screen-desktop
docker --context colima-flowly-screen build -t flowly-screen-latency-test:local tests/integration/screen/latency
for fps in 30 60; do
  docker --context colima-flowly-screen run --rm --init \
    --security-opt seccomp=unconfined --shm-size=512m \
    --mount "type=bind,source=$PWD,target=/workspace,readonly" \
    --mount "type=bind,source=$screen_desktop/node_modules/@novnc/novnc,target=/novnc,readonly" \
    -e PYTHONPATH=/workspace -e FLOWLY_SCREEN_LATENCY_TEST=1 \
    -e FLOWLY_SCREEN_BENCH_FPS="$fps" -e FLOWLY_HOME=/tmp/test-flowly-home \
    flowly-screen-latency-test:local \
    python tests/integration/screen/latency/benchmark.py
done
```

The seccomp override is for the existing disposable Linux integration runner;
Chromium's sandbox remains enabled. The image wraps its own Xvnc executable to
set the chosen frame-rate cap at process start. The benchmark verifies the actual
Xvnc process arguments. Runtime `vncconfig -set FrameRate` was rejected by the
installed server despite a zero command exit code; do not use that as evidence
of a changed frame rate. The host executable and product supervisor are untouched.

Each run uses VNC/WebRTC/WebRTC/VNC order. It prints aggregate results only:
observed sample counts, median/p95/max visible latency, local input ACK p95,
container process CPU (100% means one logical core), and available WebRTC stats.
The final marker must match the final input. Missing intermediate markers are
expected when the display frame rate is below the input rate; they are **not**
a packet-loss count. Percentiles describe only observed markers. CPU includes
both sender and receiver inside the container and is not VPS-only CPU. Short-lived
processes exiting between the snapshots may be omitted.

## Limits and decision boundary

This is a static page with a small changing marker at 1440×900, on one Linux VM.
It excludes the actual authenticated gateway, Desktop main-process bridge, SSH,
relay, WAN latency/loss, scrolling/video-heavy pages and real VPS CPU contention.
WebRTC input still uses the common WebSocket fixture, so this does **not** measure
unordered DataChannel input or its recovery/ordering semantics. WebRTC cursor
shape/position, control takeover, reconnect and profile routing are not implemented.
The negotiated codec must be read from the result, not assumed.

Do not infer that one protocol always wins from these results or deploy the
fixture. A production replacement still needs the existing identity, epoch,
revocation and drain guarantees, plus realistic network/CPU comparisons.

`../test_cursor_transport.py` separately reproduces the cursor issue on actual
TigerVNC: capture on a different connection from pointer injection contains a
baked-in cursor and an empty local cursor shape. Using the same connection
produces clean pixels and a local cursor shape. The direct pointer write in that
test is **only a diagnostic control**; bypassing the product input broker would
break the current handback boundary and is not a proposed fix.

## Product path benchmarks

Two smaller benchmarks measure the product code itself, with synthetic peers
(no Xvnc, no browser) inside the existing gateway image:

```sh
docker run --rm --init --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e PYTHONPATH=/workspace -e FLOWLY_HOME=/tmp/fh flowly-screen-gateway-test:bookworm \
  sh -c 'cd /workspace && for load in 0 6; do
    python tests/integration/screen/latency/gateway_stream_benchmark.py --load "$load"; done
    python tests/integration/screen/latency/capture_proxy_benchmark.py'
```

`gateway_stream_benchmark.py` sends 48 KiB frames at 60 fps through the real
authenticated gateway stream. `--load N` keeps N blocking jobs in the default
thread pool, as agent tools do. Before the stream checks moved to a lease and
private workers, 6 such jobs raised frame latency from about 4 ms to about
360 ms (p50) and cut delivery to 37 fps; afterwards both stay under 1 ms
(p50) at 60 fps. `capture_proxy_benchmark.py` compares a direct socket with
the supervisor's shared-capture proxy (`SharedRfbPeer`); the proxy adds well
under a millisecond at p50. Neither includes Desktop, relay or WAN latency.
