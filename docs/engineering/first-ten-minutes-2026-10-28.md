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

## 2. Auto mode that is worth its name

Decided 2026-10-28: fix Auto with rules (no reviewer model yet) and make it
the default for new installs. No app changes: Desktop (both), iOS and Android
still send `allowlist` + `on-miss` for Auto, and that is still what it is.

**Before.** All four surfaces offer Ask / Auto / YOLO with the same values
(Ask `full/always`, Auto `allowlist/on-miss`, YOLO `full/off`). Auto trusted
sixteen stdin filters (`cat`, `grep`, `jq`, `echo`, …) and judged a command by
its **first word only**:

| Command | Old Auto |
|---|---|
| `ls -la`, `ls \| grep x`, `git status` | asked |
| `echo hi && rm -r ./build` | **ran unasked** |
| `echo x \| bash` | **ran unasked** |
| `cat /etc/passwd` | **ran unasked** |

The owner's allowlist had the same hole: an allowed program carried whatever
was chained after it. New installs defaulted to YOLO (`full/off`).

**Now** (`flowly/exec/auto_rules.py`, used by `executor.execute_command`):

- A command runs unasked only if **every part** of the chain (`|`, `&&`,
  `||`, `;`) is a known read-only program used read-only: `ls`, `cat`,
  `grep`, `find`, `rg`, `stat`, `du`, `tree`, `diff`, `wc`, `sort`, `head`,
  `tail`, `jq`, `which`, …, and `git status/log/diff/show/blame/…` plus
  listing `git branch/tag/remote/stash list`, `sleep`, and `cd` into a folder
  inside the one the command runs in (`cd ..`, `cd ~`, absolute paths out ask).
- Options that write or run something ask: `sort -o` (also `-ro`, `-oout`),
  `find -delete/-exec`, `rg --pre`, `tree -o`, `uniq in out`, `git -c …`,
  `git log --output`, a new branch or tag name.
- Path arguments must resolve (symlinks followed) inside the folder the
  command runs in, without `..` or `~`; in `/` or the home folder itself no
  path argument passes.
- Redirection, `$`, backticks, `$(…)`, `&`, subshells, `VAR=x cmd`, `./script`
  always ask. (`ls 2>/dev/null` asks too: conservative on purpose.)
- The owner's allowlist applies to a single command only, never a chain.
- New installs start in Auto (`ExecApprovalsConfig` default). Installs that
  already have `credentials/exec-approvals.json` keep what it says.

**Left for later.** A reviewer model inside Auto, if Auto still asks too often
in use. The Codex mapping is inverted in every app's preset table (Ask maps
Codex to `auto-review`, Auto to `on-request`); fixing it is an app change.

**Tests.** `tests/test_exec_auto_mode.py`: a table of commands that run and
commands that ask, links out of the folder, the home folder, and the executor
end to end (the chained `touch` is asked about and does not run).

## 12. Memory consolidation keeps duplicates

**Finding** (the audit): the `consolidate` step does not merge copies of the
same information.

**What was measured** (active model `deepseek/deepseek-v4.1-flash`, the
production prompt, a scratch memory store, five runs each):

| Duplicates in the store | Before | After |
|---|---|---|
| Both notes say "User …" | 10 of 10 merged | 10 of 10 |
| One says "User …", the other "Hakan …" | **2 of 10** | **10 of 10** |
| Related but different ("lives in Istanbul" / "sister lives in Berlin", weekday/weekend wake-up) | never merged | never merged |

Exact duplicates were already caught at write time. The pass itself worked
on clean duplicates; it failed when the agent had written the owner's name in
one note and "User" in the other, because the proposer was never told whose
memory it was and, correctly told to be conservative, would not assume
"Hakan" was "User". Rewording the prompt alone ("say the same thing, whatever
the wording") changed nothing measurable.

**Fix.**
- `consolidate.owner_profile` reads the workspace `USER.md` (first 1200
  characters) and `build_context` passes it as `owner`; the prompt says
  "User", "the user" and the owner's name are one person. Both proposer paths
  get it: `Consolidator.run` (app RPC, CLI) and the agent's
  `memory_consolidate` tool (also the timed and every-50-turns runs).
- Writing a note now ignores punctuation as well as case and spacing when
  checking for an existing one ("User prefers Turkish." = "user prefers
  turkish").

**Tests.** `tests/test_memory_consolidate.py` (the owner reaches both paths),
`tests/test_memory_coordinator.py` (punctuation), and the live
`tests/test_memory_consolidate_real.py` (`FLOWLY_REAL_LLM_HOME=~/.flowly
pytest -m real_llm …`): passes 3 of 3 with the fix, merges nothing without it.

## The rest of the audit

- **#9 No time limit on the agent loop**: not a defect. The loop has an
  iteration cap (100), a soft "are you stuck?" warning, and a repetition guard
  that forces an answer when results repeat; subagents have their own cap and
  an optional timeout. A wall-clock cap would cut legitimate long research.
  The audit's 49 turns were an agent searching for an MCP tool it could not
  see, which #10 explains.
- **#10 A live-added MCP server needs a manual retry**: same root cause as #5.
  `mcp.upsert` relies on the gateway restart, which never happened on the
  audit's machine (Linux without systemd). Fixed by the in-place restart. (A
  live connect without restarting the gateway would be better still; not a
  bug fix, not done.)
- **#12 arxiv returns 400 on field queries**: fixed (commit `f3cb671c`).
- **#12 Screen setup fails under the sandbox / `flowly agent` cannot use the
  real screen**: CLI-only paths; not product surfaces under the 2026-10-28
  decision (apps manage the agent). Not changed.
- **#3 Gateway token visible in `ps`** and **#4 no token on loopback**: same
  trust model as #1 on the owner's own machine. Not changed.
- **#11 Screen RAM on 1 GB cloud machines**: a machine-size decision for the
  cloud service, not Core code.
