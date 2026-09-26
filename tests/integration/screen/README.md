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
production task pause/resume, root-host runtime provisioning, orphan recovery, host
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
    tests/test_screen_tickets.py tests/test_screen_launcher.py tests/test_screen_execution.py \
    tests/test_screen_gateway_binding.py tests/test_screen_browser_provider.py \
    tests/test_screen_cli.py tests/test_screen_profile_binding.py tests/test_screen_display_gateway.py \
    tests/test_screen_input_broker.py tests/test_screen_input_service.py \
    tests/integration/screen/test_xvnc_browser.py -q \
    --basetemp=/tmp/screen-tests -p no:cacheprovider
```

This adds the real authenticated Gateway WebSocket → browser tool → headed
Chromium path, wrong-profile rejection, same-run target preservation across
human interaction, and actual `flowly screen start/status/stop` subprocesses.
The gateway test uses an explicit test event to hold the callback at handoff:
it verifies binding and observation fencing, **not** a completed production
pause/resume RPC or Desktop UI. No model is invoked.
The profile routing tests use real authenticated primary and named-gateway
WebSockets with stubbed browser I/O. They verify closed grant validation,
destination generation checks, and original run identity across the broker.
The expanded Linux screen suite passed **198 tests** on the environment above.
The authenticated direct display endpoint also delivers real Xvnc pixels for
the default and named profiles. Its single-use tickets are bound to the host,
profile and runtime generation; gateway credentials and tickets use headers.
Synthetic socket tests cover input filtering, replay rejection, profile identity
changes, observer limits and teardown. This endpoint currently advertises viewing
only: public lifecycle, takeover and input-drain coordination remain unfinished.

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
through a private, bounded Unix request service; public display control RPCs are
not connected yet. Gateway worker exit preserves the same input handle and
sequence. Repeating the last acknowledged sequence with the same payload returns
its acknowledgement without reapplying input; changed/older sequences fail.
The service keeps a bounded cache of completed handbacks. Replaying an old
handback cannot release a newer holder; an evicted handle fails without mutation.
The real Linux test exits a separate worker after pressing Shift, adopts its
broker from another client, deduplicates the retry and releases Shift. New
runtime manifests advertise `inputServiceVersion: 1`; an already-running older
supervisor does not gain this capability by gateway reconnect alone.

Supervisor death or unknown input outcome retains human ownership and requires explicit recovery;
automatic timeout/disconnect release is intentionally absent. Extended pointer,
QEMU key, resize and extended clipboard messages are not admitted by this basic
input connection; viewing-stream negotiation must be aligned before enabling
public input. See the
[RFB Fence protocol](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst#748clientfence).

The `screen` Python extra adds Playwright without downloading a bundled browser;
the runtime attaches to the installed distro Chromium. CLI lifecycle commands
use the ordinary selected `FLOWLY_HOME`/`--profile` context. They expose no CDP
port, Unix socket path or raw viewer capability. Mac/Windows screen lifecycle is
unsupported; their normal CLI agent functionality remains independent.
