# The first ten minutes: fixes from an outside audit

Status: branch `fix/first-ten-minutes` (2026-10-28), not merged, not pushed.
Engineering gotchas found along the way are in [`notes.md`](../../notes.md).

## Where this came from

An agent set Flowly up on a clean machine and used it for an afternoon
(CLI install, a gateway, a cheap OpenRouter model). The verdict: the core
holds (setup, subagents, memory, approvals and Board worked), but the first
ten minutes look broken, because several failures are silent. Every finding
below was checked against the code before anything was changed; two were
wrong and are recorded as such.

| # | Finding | Verdict | Section |
|---|---|---|---|
| 1 | The agent can read its own API key (`config.json`) | **True** | [Credentials](#1-credentials-out-of-configjson) |
| 2 | Commands run with full rights and no prompt by default | True (`exec/approvals.py`: `full` / `off`) | pending |
| 3 | The gateway token is on the launchd command line, visible in `ps` | True | pending |
| 4 | The gateway asks no token on loopback | True by design (Origin check); the agent's own shell is the gap | pending, with #1 |
| 5 | A connection that needs a restart never restarts; the app says "restarting" | **True, and worse** | [Restart](#5-restart-in-place) |
| 6 | Web search does not work on a fresh install | True without an account or key | [Search](#6-keyless-search-fallback) |
| 7 | Cron delivery is broken | **False**: the `message` tool is off in cron by design; the gateway delivers the reply | [Cron](#7-8-cron-from-the-terminal) |
| 7b | A job made from the terminal is accepted with no word that nothing will show its results | True | [Cron](#7-8-cron-from-the-terminal) |
| 8 | `flowly cron remove` does not stop the job in a running gateway | True | [Cron](#7-8-cron-from-the-terminal) |
| — | A failed delivery counts as `ok` | Partly: the run is `ok`, `lastDeliveryError` is kept and shown by Desktop and iOS | no change |
| — | `profile create --clone` copies keys and `USER.md` | By design (a clone answers with the model it was given); identity is stripped | no change |

Every fix has a regression test that fails without it; the commit message
of each says what was proven.

## 5. Restart in place

Commit `af52d077`.

**Before.** A settings change that attaches at boot (a channel, a tool,
voice) returns `willRestart` and the transport then asked the machine's
service manager to restart the gateway (`launchctl kickstart -k
ai.flowly.gateway`, `systemctl --user restart`). That only works when the
asking process *is* that service:

- A named agent runs as a child of its host gateway (`flowly --profile X
  serve`), but used the fixed label, so it bounced the **host** and every
  agent. macOS had no "is this service me" check (Linux had one).
- A gateway started by hand, under Desktop's own launchd job, or on Linux
  without systemd had no service: the result (`no_service`) went to the log
  while the app had already said "restarting".
- Success was "the port answers", which the old process (or a gateway
  started by hand) still did. On the maintainer's machine the launchd job
  pointed at a deleted worktree while a hand-started gateway held the port:
  every restart would have reported success and changed nothing.

**Now.** `flowly/self_restart.py`. The gateway stops the way SIGTERM stops
it (channels, cron, MCP servers, agents wind down), then `execv`s the same
command line. The PID stays, so launchd, systemd, a host gateway and a
terminal all keep the process they started, and no second gateway can race
for the port.

- `gateway_cmd` installs the stop hook (`shutdown_event.set`) on POSIX, drops
  one-shot flags (`--rotate-token`) from the replayed command, and hands a
  service takeover to the restarted process (`FLOWLY_RESTORE_SERVICE_PORT`)
  instead of restoring the service.
- Clients are closed with WebSocket code **4075**. The host gateway
  (`ProfileHost._read_runtime`) seeing 4075 does not kill its agent: it
  keeps the process (and keeps draining its pipes), waits for the agent's
  new lease with `read_runtime_lease`, and re-attaches the same process,
  owned as before. Meanwhile the agent counts as "starting", so a request
  waits instead of spawning a second process.
- `gateway.restart` works for a hand-started gateway too.
- The service-manager path remains for Windows and the TUI. On macOS it
  refuses a process that is not the launchd job and waits for a new job PID
  before calling a restart done.

**Tests.** `tests/test_gateway_restart_in_place.py`, including a real host
gateway with a real named agent: `config.set {restart: true}` → same PID,
new instance, `sessions.list` answers.

## 6. Keyless search fallback

Commit `2e097b2b`.

Brave answers only with a key or a signed-in Flowly account; DuckDuckGo was
off until turned on. A CLI install without an account had no web search
(the audit's first research task took 164 s working around it, 22 s with
ddgs on). Signed-in installs (Mac app, cloud) already use Flowly's search
proxy and are unchanged.

`tools.web.search.ddgs.enabled` is now tri-state: unset (default) makes
DuckDuckGo the **last resort**, used only when no other search backend is
available, after the whole preference order; `false` turns it off for good;
`true` puts it in the normal order. Its connection card shows it on.

**Tests.** `tests/test_web_search_last_resort.py`.

## 7, 8. Cron from the terminal

Commit `1fc02f40`.

**The audit was wrong about delivery.** A cron run has the `message` tool
disabled on purpose (`CRON_DISABLED_TOOLS`); its final reply is the
deliverable and the gateway delivers it: to the origin chat over the
gateway socket and its history for Desktop/iOS/Android, through the adapter
for relay and channels, plus push. The audit created its job from `flowly
agent`, so the origin was the terminal, which nothing reads later.

**What was real.**

- `flowly cron remove` (and `add`, `enable`) wrote `jobs.json` under a
  running gateway, which holds the schedule in memory and saves it on its
  own: the next save undid the change and the job kept firing. These
  commands now go through the running gateway's cron RPCs
  (`flowly/cli/live_gateway.py`) and edit the file only when no gateway runs.
- The CLI asks only the gateway serving **its own Flowly home**: `/health`
  carries an opaque `home_id`. Without it, a test run reached the
  maintainer's real gateway on 18790 (a test job id; nothing changed).
- A new job's reply now says where results go: nowhere but the job, the
  terminal conversation, or the channel.

Delivery itself (chat, relay, APNs/FCM) is untouched; the cron, push and
relay suites (420 tests) pass.

**Tests.** `tests/test_cron_cli_live_gateway.py`: a gateway holding the
schedule in memory, the CLI changing it, then the gateway's own save.

## 1. Credentials out of config.json: not done

Decided 2026-10-28: **not doing it**. Flowly is an agent that runs on the
owner's own machine; there, the agent being able to read the owner's own key
in `config.json` is the same trust model every local agent has. In the cloud,
D27 already limits what a machine holds to a revocable, model-only `flw_` key.

What was found stays recorded for whoever revisits it:

| Command | Result |
|---|---|
| `cat ~/.flowly/config.json` | blocked |
| `cd ~/.flowly && cat config.json` | ran |
| `cat ~/.flowly/profiles/work/config.json` | ran (profiles not listed) |
| `python3 -c "open('…/config.json').read()"` | ran |
| `grep -r apiKey ~/.flowly` | ran |

- The guard (`find_protected_paths_in_command`) is a string check, not a
  boundary; the CLI sandbox wraps the gateway too, so it cannot hide config.
- A file split (`config.json` + `secrets.json`) was prototyped and reverted.
  It cannot ship from Core alone: the Electron Desktop reads `config.json`
  directly in 15+ places (including `gateway.token` to reach the local
  gateway), and its named-profile sandbox (`sandbox/policy.ts`) denies the
  primary's `config.json` but would not know a `secrets.json`, so a named
  agent could read the primary's keys. Any future attempt must ship with
  Desktop and gate the migration on a Desktop that understands it.
- Writes to config go through `loader.save_config`,
  `integrations.config_io._atomic_write_json` and `profile._atomic_write_json`;
  MCP `env` values are often `${VAR}` references to `.env`, not secrets.
