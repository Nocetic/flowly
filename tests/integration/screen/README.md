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

Not yet certified: direct/relay WebSocket transport, Desktop/noVNC, agent tool
binding and resume, root-host runtime provisioning, orphan recovery, host
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
  python -m pytest tests/test_screen_gateway_binding.py tests/test_screen_browser_provider.py \
    tests/test_screen_cli.py tests/integration/screen/test_xvnc_browser.py -q \
    --basetemp=/tmp/screen-tests -p no:cacheprovider
```

This adds the real authenticated Gateway WebSocket → browser tool → headed
Chromium path, wrong-profile rejection, same-run target preservation across
human interaction, and actual `flowly screen start/status/stop` subprocesses.
The gateway test uses an explicit test event to hold the callback at handoff:
it verifies binding and observation fencing, **not** a completed production
pause/resume RPC or Desktop UI. No model is invoked.

The `screen` Python extra adds Playwright without downloading a bundled browser;
the runtime attaches to the installed distro Chromium. CLI lifecycle commands
use the ordinary selected `FLOWLY_HOME`/`--profile` context. They expose no CDP
port, Unix socket path or raw viewer capability. Mac/Windows screen lifecycle is
unsupported; their normal CLI agent functionality remains independent.
