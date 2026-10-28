# Engineering notes

Short things that are easy to get wrong in this codebase. Each line says what
to do and why; the long story is in `docs/engineering/`. Add to it when a bug
teaches you something the code alone does not say.

## Gateway lifecycle

- **Never ask the service manager to restart the gateway from inside it.**
  Use `flowly.self_restart.request()` (stop like SIGTERM, then `execv` the same
  command line). `launchctl kickstart ai.flowly.gateway` from a named agent
  bounces the host and every agent; from a hand-started gateway it does
  nothing while the app shows "restarting".
  ([report](docs/engineering/first-ten-minutes-2026-10-28.md#5-restart-in-place))
- **"The port answers" is not "it restarted".** The old process, or another
  gateway, answers too. Wait for a new PID.
- **A restarting gateway closes its sockets with code 4075.** Anything that
  owns a gateway process (the profile host) must treat 4075 as "coming back",
  not "gone": keep the process, keep reading its stdout/stderr (a full pipe
  blocks it), wait for its new lease.
- **`reconcile_runtime_lease` kills.** It retires a runtime it believes this
  manager left behind, which includes one this manager is waiting for. To
  look at a lease without side effects, use `read_runtime_lease`.
- **One-shot CLI flags must not survive a restart.** `self_restart.install(...,
  drop_flags=(...))`; `--rotate-token` would unpair every app on every restart.

## State a running gateway owns

- **A CLI command must not write a file a running gateway holds in memory**
  (`cron/jobs.json` today). The gateway's next save undoes it. Go through the
  gateway (`flowly.cli.live_gateway.call_running_gateway`) and write the file
  only when none is running.
- **Only talk to the gateway serving your own Flowly home.** Check `/health`'s
  `home_id` (`flowly.gateway.identity.home_id`). Port 18790 on a developer's
  machine is their real gateway, also while tests run.

## Config

- **`config.json` is read directly by the Electron Desktop** (15+ places,
  including `gateway.token`). Moving or renaming anything in it is a
  cross-repo change: ship it with Desktop.
- **`${VAR}` in config is a reference, not a secret**: the value is in `.env`.

## Exec approvals

- **Judge every part of a shell command, never its first word.** `echo hi &&
  rm -r x` starts with a harmless word. `flowly.exec.auto_rules` splits on
  `|`, `&&`, `||`, `;` and asks on any other shell syntax.
- **An allowlisted program covers one command, not a chain**
  (`auto_rules.is_single_command`).
- **The apps send `security`/`ask` pairs, not mode names.** Ask = `full/always`,
  Auto = `allowlist/on-miss`, YOLO = `full/off`, anything else shows as Custom.
  Changing what a mode does is a Core change; changing its values breaks
  every app's preset table.

## Memory

- **Tell any model that judges memories whose memory it is.** Notes call the
  owner "User" and by name; without the owner's profile (workspace `USER.md`)
  a careful model will not assume they are one person
  (`consolidate.owner_profile`).
- **Measure a prompt change with a live model before shipping it.** Rewording
  the consolidation prompt felt right and changed nothing; the missing input
  was the fix. Keep the measurement as a `real_llm` test.

## Search

- **`tools.web.search.ddgs.enabled` is tri-state**: unset = last resort when
  nothing else works, `false` = never, `true` = normal order. Don't default it
  to a bool.

## Tests

- **Tests must not reach the developer's real gateway or service.** A test
  that runs CLI code can hit 127.0.0.1:18790, and code under test that calls
  `restart_gateway` can `kickstart` the real launchd job. Isolate `FLOWLY_HOME`
  and stub the service manager.
- **Reset module-level flags you set.** `service_control._restart_requested`
  and `self_restart._requested` leak into later tests in the same run.
- **zsh does not word-split `$var`**, and an unmatched glob aborts the whole
  command. Run test file lists through `bash -c`.
- **Prove the regression test**: stash the fix (keep the test), see it fail,
  pop. Write it down in the commit message.
