---
name: node-inspect-debugger
description: "Debug Node.js via --inspect + Chrome DevTools Protocol CLI."
metadata: {"flowly":{"emoji":"🟢","tags":["debugging","nodejs","node-inspect","cdp","breakpoints"],"requires":{"bins":["node"]},"related_skills":["systematic-debugging","python-debugpy"]}}
---

# Node.js Inspect Debugger

## What this gives you

`console.log` only shows what you remembered to print. The V8 inspector that ships inside every Node binary lets you stop the program mid-flight and look at *everything*: the live call stack, the value of any local or captured variable, and the result of any expression evaluated inside the frame that is currently halted. You drive all of it from a terminal — through the `exec` tool or an interactive PTY shell — without leaving the agent loop.

There are two ways in, and the right choice depends on whether a human-style REPL or a scripted run fits the job:

| Approach | What it is | Reach for it when |
|---|---|---|
| `node inspect` | The REPL that ships with Node. Nothing to install. | Quick, hands-on poking at one or two breakpoints. |
| CDP over `chrome-remote-interface` (or `ndb`) | A library you script from JS/TS. | You want repeatable, non-interactive runs — many breakpoints, scope dumps captured to a file, automated repros. |

Default to `node inspect`. It is always present and it starts instantly. Move to the scripted CDP path only once the interactive REPL starts to feel like overhead.

## Situations where it earns its keep

Reach for breakpoint debugging when the answer lives in runtime state you cannot easily print:

- A Node test produces the wrong result and you need to see the intermediate values that led there.
- A Node UI or CLI process renders incorrectly or crashes, and you want to freeze it *before* the render to inspect state.
- A spawned child or PTY bridge worker is misbehaving and the failure depends on its in-memory state.
- The value you care about is trapped in a closure that no log line can reach without you editing the source.
- You need a CPU profile or heap snapshot from a process that is already running.

Skip it for anything a one-line `console.log` settles in under a minute. Attaching a debugger has real setup cost — spend it only when the payoff justifies it.

## `node inspect` REPL cheat sheet

Start a fresh script halted on its very first statement:

```bash
node inspect path/to/script.js
# TypeScript through tsx:
node --inspect-brk $(which tsx) path/to/script.ts
```

Once you see the `debug>` prompt, these are the controls:

| Type this | It does |
|---|---|
| `c` / `cont` | resume until the next breakpoint |
| `n` / `next` | step over the current line |
| `s` / `step` | step into the call on this line |
| `o` / `out` | finish the current function and stop in the caller |
| `pause` | halt code that is currently running |
| `sb('file.js', 42)` | break at line 42 of file.js |
| `sb(42)` | break at line 42 of the current file |
| `sb('functionName')` | break whenever that function is entered |
| `cb('file.js', 42)` | remove that breakpoint |
| `breakpoints` | print every breakpoint you've set |
| `bt` | dump the call stack |
| `list(5)` | show 5 lines of source on each side of the stop point |
| `watch('expr')` | re-evaluate `expr` at every stop |
| `watchers` | show the current watch expressions |
| `repl` | open a JS prompt scoped to the paused frame (`Ctrl+C` leaves it) |
| `exec expr` | evaluate `expr` a single time |
| `restart` | rerun the script from the top |
| `kill` | terminate the target |
| `.exit` | leave the debugger |

The `repl` sub-prompt is where the real work happens: it sees every local and closed-over binding in the halted frame, so you can type `myVar`, `Object.keys(this)`, or any expression and read the result directly. `Ctrl+C` drops you back to `debug>`.

## Hooking into a process that's already up

If the target is long-lived — a dev server, a gateway, anything you didn't launch with an inspect flag — you can light up the inspector after the fact. Node treats `SIGUSR1` as the "turn on the debugger now" signal:

```bash
# Tell the running process to open its inspector
kill -SIGUSR1 <pid>
# It logs something like: Debugger listening on ws://127.0.0.1:9229/<uuid>

# Connect by PID...
node inspect -p <pid>
# ...or by the websocket URL it printed
node inspect ws://127.0.0.1:9229/<uuid>
```

When you control the launch, ask for the inspector up front instead:

```bash
node --inspect script.js              # opens 127.0.0.1:9229, runs immediately
node --inspect-brk script.js          # opens AND halts on line 1
node --inspect=0.0.0.0:9230 script.js # pick your own host:port
```

For TypeScript run through tsx:

```bash
node --inspect-brk --import tsx script.ts
# Older tsx releases:
node --inspect-brk -r tsx/cjs script.ts
```

## Scripting the inspector with CDP

When one-off REPL stepping doesn't scale — you need a dozen breakpoints, want scope state written somewhere durable, or you're automating a repro that has to run the same way every time — talk to the inspector directly over the Chrome DevTools Protocol using `chrome-remote-interface`:

```bash
npm i -g chrome-remote-interface   # global, or install into the project
# Launch the target, halted, on a known port:
node --inspect-brk=9229 target.js &
```

Here's a driver that connects, stops at a chosen line, prints every local and closure binding, evaluates a custom expression in that frame, then continues. Save it as `/tmp/cdp-debug.js`:

```javascript
const CDP = require('chrome-remote-interface');

async function main() {
  const session = await CDP({ port: 9229 });
  const { Debugger, Runtime } = session;

  // Fires every time execution halts (breakpoint, debugger statement, etc.)
  Debugger.paused(async (event) => {
    const frame = event.callFrames[0];
    const line = frame.location.lineNumber + 1; // CDP lines are 0-based
    console.log(`[halt] ${event.reason} at ${frame.url}:${line}`);

    // Enumerate locals and captured variables in the top frame
    for (const scope of frame.scopeChain) {
      if (scope.type !== 'local' && scope.type !== 'closure') continue;
      const props = await Runtime.getProperties({
        objectId: scope.object.objectId,
        ownProperties: true,
      });
      for (const prop of props.result) {
        const v = prop.value;
        console.log(`  [${scope.type}] ${prop.name} =`, v?.value ?? v?.description);
      }
    }

    // Run an arbitrary expression inside the halted frame
    const probe = await Debugger.evaluateOnCallFrame({
      callFrameId: frame.callFrameId,
      expression: 'typeof state !== "undefined" ? JSON.stringify(state) : "n/a"',
    });
    console.log('  state =', probe.result.value ?? probe.result.description);

    await Debugger.resume();
  });

  await Runtime.enable();
  await Debugger.enable();

  // Match the source file by regex and break at a specific (0-based) line
  await Debugger.setBreakpointByUrl({
    urlRegex: '.*app\\.tsx$',
    lineNumber: 119,
    columnNumber: 0,
  });

  // Release the process that was waiting on --inspect-brk
  await Runtime.runIfWaitingForDebugger();
}

main();
```

Then run the driver against the waiting target:

```bash
node /tmp/cdp-debug.js
```

To avoid adding `chrome-remote-interface` to the project's dependency tree, drop it in a scratch directory and point `NODE_PATH` at it:

```bash
mkdir -p /tmp/cdp-tools && cd /tmp/cdp-tools && npm i chrome-remote-interface
NODE_PATH=/tmp/cdp-tools/node_modules node /tmp/cdp-debug.js
```

## Debugging UI and CLI entrypoints

### A built/bundled entrypoint

If the project compiles first, build once so there's a stable `dist/` to break in, then launch the artifact under `--inspect-brk`:

```bash
cd /path/to/repo
npm run build     # only if the project has a build step
node --inspect-brk dist/entry.js
# From a separate exec / shell:
node inspect -p <node pid>
```

At the `debug>` prompt, set a breakpoint on the suspect render path and let it run up to there:

```
sb('dist/app.js', 220)
cont
```

On the halt, type `repl` and read `props`, your state refs, input-handler values — whatever the frame holds.

### A Node process already running

Find the PID, signal it to expose the inspector, read off the websocket URL, and attach:

```bash
# 1. Locate the process
NODE_PID=$(pgrep -f 'node .*entry' | head -1)

# 2. Open its inspector
kill -SIGUSR1 "$NODE_PID"

# 3. Pull the websocket URL it now advertises
curl -s http://127.0.0.1:9229/json/list | jq -r '.[0].webSocketDebuggerUrl'

# 4. Connect
node inspect ws://127.0.0.1:9229/<uuid>
```

The app keeps responding to input while attached — execution only freezes when it actually reaches one of your `sb(...)` breakpoints.

### When the failing child is Python

This skill covers Node only. If the misbehaving child process is Python (a worker, a PTY bridge), switch to the `python-debugpy` skill for that part.

## Stepping through Vitest

You can run a single test file under the inspector. Aim Node at the Vitest entry and force single-worker execution so you aren't fighting a process pool:

```bash
cd /path/to/repo
node --inspect-brk ./node_modules/vitest/vitest.mjs run --no-file-parallelism src/app/foo.test.tsx
```

In a second `exec`: `node inspect -p <pid>`, then `sb('src/app/foo.tsx', 42)` and `cont`. Use `--no-file-parallelism` for Vitest (or `--runInBand` for Jest) — debugging across a worker pool is miserable.

## Profiles and snapshots without a REPL

The same CDP driver shape works for performance capture: swap the `Debugger` domain for `Profiler` or `HeapProfiler`.

CPU profile over a 5-second window:

```javascript
await session.Profiler.enable();
await session.Profiler.start();
await new Promise((resolve) => setTimeout(resolve, 5000));
const { profile } = await session.Profiler.stop();
require('fs').writeFileSync('/tmp/cpu.cpuprofile', JSON.stringify(profile));
// Load /tmp/cpu.cpuprofile in Chrome DevTools → Performance
```

Heap snapshot (the data arrives in chunks you concatenate):

```javascript
await session.HeapProfiler.enable();
const parts = [];
session.HeapProfiler.addHeapSnapshotChunk(({ chunk }) => parts.push(chunk));
await session.HeapProfiler.takeHeapSnapshot({ reportProgress: false });
require('fs').writeFileSync('/tmp/heap.heapsnapshot', parts.join(''));
```

## Traps that waste an hour

- **TS line numbers don't line up.** The inspector breaks in the *emitted* JS, not your `.ts`. Either set breakpoints against the built `dist/*.js`, or run with `node --enable-source-maps` and break on `src/app.tsx` — but the latter only works with CDP clients that resolve sourcemaps. The plain `node inspect` CLI does not.
- **Picking `--inspect` when you meant `--inspect-brk`.** `--inspect` opens the inspector but lets the program run. Attach a fraction too late and your first breakpoint has already flown past. Use `--inspect-brk` whenever a breakpoint needs to be in place before any code executes.
- **Two processes fighting over 9229.** That's the default port. With more than one inspectable process, launch with `--inspect=0` for a random port and discover the real one from the target list:
  ```bash
  curl -s http://127.0.0.1:9229/json/list   # every inspectable target on the host
  ```
- **Children aren't inherited.** Inspecting a parent does not inspect what it spawns. Export `NODE_OPTIONS='--inspect-brk'` so the flag propagates to every child — and note each needs its own port (Node auto-increments when `--inspect` is inherited this way).
- **A `Ctrl+C` can leave the target frozen.** Bailing out of `node inspect` while the program is paused leaves it paused. Either `cont` it first or `kill` the target outright.
- **Interactive stepping needs a PTY.** `node inspect` is a live REPL. From Flowly, drive it with `exec(pty=true)`, or run it `background=true` and feed input via `process(action='submit', data='...')`. Plain non-PTY foreground exec is fine for a single command but won't sustain interactive stepping.
- **`0.0.0.0` is a remote-code-execution hole.** Binding the inspector to `--inspect=0.0.0.0:9229` lets anyone on the network run code in your process. Keep it on `127.0.0.1` (the default) unless the network is genuinely isolated.

## Sanity checks before you trust the session

- [ ] `curl -s http://127.0.0.1:9229/json/list` shows exactly the target you intended — no more, no fewer.
- [ ] Your first breakpoint genuinely fires. If it never does, you most likely forgot `--inspect-brk` or attached after the code finished.
- [ ] The source shown at the halt is the file you expected. A mismatch means a sourcemap problem (see the first trap).
- [ ] `exec process.pid` inside `repl` returns the PID you meant to attach to.

## Copy-paste recipes

**"This variable is undefined at line X — why?"**
```bash
node --inspect-brk script.js &
node inspect -p $!
# at debug>
sb('script.js', X)
cont
# halted — inspect the frame:
repl
> myVariable
> Object.keys(this)
```

**"How did execution reach this function?"**
```
debug> sb('suspectFn')
debug> cont
# halts on entry
debug> bt
```

**"This async chain hangs somewhere — where?"**
```
# Launch with --inspect (no -brk), let it reach the hang, then:
debug> pause
debug> bt
# the top frame is where it's stuck
```
