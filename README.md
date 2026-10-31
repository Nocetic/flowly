# Flowly Core (private)

Core is the agent: the gateway, the agent loop, tools, skills, memory and
channels. It runs in three places:

- **On a user's Mac.** Flowly Desktop carries it compiled
  (`Flowly.app/Contents/Helpers/FlowlyAgent.app`) and runs it as the
  `ai.flowly.gateway` service.
- **On a cloud computer.** One Fly machine per paying user, built from
  [`cloud/image`](cloud/image/README.md).
- **From your checkout**, while you develop.

Wherever it runs, Core connects out to the Flowly server
(`wss://cloud.useflowlyapp.com/v1/agent`). The apps never talk to Core
directly: they sign in to a Flowly account, and the server shows them every
computer and agent on that account. The relay code is still here for older
installs; new work uses the server.

## The repos

| Repo | What |
|---|---|
| `Nocetic/flowly-core` (this one) | Core and the cloud computer image |
| `Nocetic/flowly-server` | The Flowly server, its protocol (`server/PROTOCOL.md`) and the internal docs (`docs/cloud/`: decisions, roadmap, log) |
| `Nocetic/flowly-bots-desktop` | Flowly Desktop (macOS, Windows, Linux) |
| `Nocetic/Flowly-iOS`, `Nocetic/flowly-android` | The mobile apps |
| `hakansoren/flowlyai-build` | Compiles Core (Nuitka) for Desktop |
| `Nocetic/flowly` | The public open-source Core, frozen on 2026-10-26; nothing is pushed there |

## Run Core from a checkout

You need [uv](https://docs.astral.sh/uv/) and Git. uv installs Python itself.

```bash
git clone https://github.com/Nocetic/flowly-core.git ~/flowly-core
cd ~/flowly-core
uv run flowly login --no-relay
uv run flowly server connect
uv run flowly gateway
```

1. **`login`** opens the browser to sign in to your Flowly account and saves
   the account key the agent's models bill to. No provider setup is needed.
   `--no-relay` skips the old "reach from your phone" relay entry; the server
   replaces it.
2. **`server connect`** registers this Mac on your account and writes the
   server address, the Mac's id and its secret into `~/.flowly/config.json`
   (`channels.flowlyServer`). Run it once; running it again re-registers the
   same Mac.
3. **`gateway`** runs your code. It connects to the server, and from then on
   the apps see this Mac.

Then sign in to Flowly Desktop, iOS or Android with the same account. The Mac
shows up under its name, and agents you create there run from your checkout.
No IP, port, token or QR code is involved.

Edit, stop the gateway with Ctrl+C, start it again.

### Where to put the checkout

Clone it into your home folder, not under `~/Desktop`, `~/Documents` or
`~/Downloads` (macOS can keep background processes out of those), and don't
move it afterwards: `.venv` remembers the absolute path. When Core can't read
the checkout, creating an agent fails with `No module named 'flowly.cli'`.

### Your state

Core keeps everything in `~/.flowly`: config, memory, sessions, each agent's
home under `~/.flowly/profiles/`. A checkout uses the same folder as an
installed Flowly.

If Flowly Desktop is installed, its agent service already holds the gateway
port (18790). `uv run flowly gateway` asks before stopping it, serves the port
itself, and puts the service back when you press Ctrl+C. Don't kill the
service by hand; launchd starts it again.

## Tests and lint

```bash
uv run --extra dev pytest
uv run --extra dev ruff check flowly/
```

Tests set their own `FLOWLY_HOME` and never touch `~/.flowly`. CI runs both on
pushes to `main` and on pull requests (`.github/workflows/ci.yml`).

## Traps

- **The `flowly` on your PATH is not your checkout.** It is whatever Desktop
  or `uv tool install` put there. Inside the checkout, always run
  `uv run flowly …`.
- **Don't run `flowly update` in your checkout.** It stashes your changes,
  pulls and restarts.
- **Don't say yes to the relay** if `login` asks. It creates a second entry
  for the same Mac.
- **`scripts/dev-install.sh` predates the server.** Its copy of your config
  keeps your Mac's server entry, so `server connect` there would re-register
  your real Mac. Don't connect an isolated instance to the server until the
  script is updated.

## Where Core ships

- **Desktop:** `flowlyai-build`'s `nuitka-binary.yml` compiles `main` of
  this repo; Desktop's `scripts/stage-agent-runtime.sh` takes a build into
  the app.
- **Cloud computer:** `cloud/image` builds Core from this repo's tracked files.

So `main` is what the next release runs. Push to it only with Hakan's OK.

## More

- [CONTRIBUTING.md](CONTRIBUTING.md): project layout, skills, plugins and
  tools, commit and PR conventions.
- [SECURITY.md](SECURITY.md): the trust model.
- `content/docs/`: the user docs published at useflowlyapp.com/docs.
- `flowly --help`: every command.
