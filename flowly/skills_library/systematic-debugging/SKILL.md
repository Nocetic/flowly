---
name: systematic-debugging
description: "4-phase root-cause debugging: understand bugs before fixing them."
metadata: {"flowly":{"emoji":"🔬","tags":["debugging","troubleshooting","root-cause","investigation"],"related_skills":["test-driven-development","writing-plans","subagent-driven-development"]}}
---

# Systematic Debugging

## Why This Skill Exists

Most wasted debugging time comes from one habit: changing code before you know what's wrong. A guess that "fixes" the visible symptom usually leaves the real defect in place — or spawns a second bug somewhere downstream. The result is a thrash loop where each edit creates new mysteries.

This skill replaces guessing with a repeatable investigation routine. The non-negotiable rule:

> **Diagnose first. A change you can't explain is not a fix — it's a coin flip.**

You move through four stages, in order. Each stage produces an artifact (evidence, a comparison, a hypothesis, a verified change) that the next stage depends on. Skipping ahead means building on sand.

## When This Applies

Reach for this routine whenever something behaves in a way you can't immediately account for:

- A test goes red
- Behavior diverges from the spec in production or locally
- A build, CI job, or deploy fails
- Latency, memory, or throughput regresses
- Two systems that should integrate don't

The temptation to skip the routine is strongest in exactly the situations where it pays off most:

- **The clock is ticking.** Pressure makes shortcuts feel justified; they aren't.
- **The fix looks obvious.** "Obvious" is a hypothesis, not a diagnosis.
- **You've already changed things.** Prior failed attempts mean you understand the bug *less* than you think, not more.
- **The bug "looks trivial."** Trivial-looking bugs still have a real cause. Finding it takes minutes when the cause is simple.

There is no category of bug where guessing beats investigating. Disciplined debugging is the faster path even when — especially when — it doesn't feel like it.

---

## Stage 1 — Establish the Facts

Goal: be able to state, in plain language, *what* happens and *why*. Do not touch the fix until you can.

### Read the failure output in full

Errors are not noise to scroll past. Stack traces, line numbers, error codes, and warnings frequently name the defect outright.

- Read the entire trace, top to bottom, before forming any opinion.
- Note every file path and line number it points at.
- Treat warnings as data, not clutter.

Use `read_file` to open the implicated source. Use `exec` with `rg` to locate the exact error text in the tree:

```bash
rg -n "exact error string" --type py
```

### Make it happen on demand

You cannot fix what you cannot trigger.

- Write down the precise steps that produce the failure.
- Confirm it fails *every* time those steps run, not intermittently.
- If it only fails sometimes, that flakiness is itself a clue — collect more data rather than guessing at a cause.

Reproduce it with `exec`:

```bash
# Isolate the failing case
pytest tests/test_module.py::test_name -v

# Full trace when the failure is opaque
pytest tests/test_module.py -v --tb=long
```

### Look at what recently moved

Most bugs are introduced by a change. Find it.

```bash
git log --oneline -10          # recent commits
git diff                       # uncommitted edits
git log -p --follow src/suspect.py | head -100   # history of one file
```

Also check non-code movement: dependency bumps, config edits, environment differences.

### For layered systems, instrument the seams

When the path crosses boundaries — request → handler → service → store, or commit → build → ship — the failure is somewhere along that chain. Don't guess which link.

At each boundary, capture:

- what data arrives
- what data leaves
- whether config and environment carried through correctly
- the state on each side

Run it once with that instrumentation. The logs will tell you which link breaks. *Then* you focus your investigation on that link instead of all of them.

### Walk the bad value back to its source

When the error surfaces deep in a call chain, the surfacing point is rarely the origin.

- Identify where the wrong value first appears.
- Ask what handed it that value.
- Keep walking upstream until you reach the place the value is actually produced.
- The fix belongs at the origin, not where the explosion happened.

Trace callers and assignments with `exec`:

```bash
rg -n "function_name\(" src/      # who calls it
rg -n "variable_name\s*=" src/    # where it's set
```

### Gate before moving on

Confirm all of these before Stage 2:

- [ ] Read and understood the full error output
- [ ] Reproduced the failure deterministically
- [ ] Reviewed recent code/config/dependency changes
- [ ] Collected evidence (logs, state snapshots, traced values)
- [ ] Narrowed the failure to a specific component or line
- [ ] Can state a candidate root cause out loud

If you can't explain *why* it breaks, you are not done. Stay here.

---

## Stage 2 — Contrast With What Works

Goal: turn "something's off" into a precise list of differences.

### Locate a healthy analog

Find code in the same project that does the same kind of thing and works correctly. Now you have a control to compare against.

### Study any reference end to end

If you're following a pattern, library example, or spec, read *all* of it. Skimming half a reference and improvising the rest is how subtle bugs get baked in. Understand the whole thing before you lean on it.

### Enumerate every difference

Lay the working case beside the broken one and list what differs — ordering, arguments, types, timing, config, anything. Resist the urge to dismiss differences as irrelevant; the one you wave away is often the culprit.

### Map the requirements

Spell out what the broken code actually depends on: other components, config keys, environment, and the assumptions it silently makes about its inputs.

---

## Stage 3 — Propose and Probe

Goal: convert your candidate cause into a tested claim using the scientific method.

### Commit to one explanation

Write a single, falsifiable statement: *"The cause is X, because Y."* Vague ("something with the cache") is useless. Specific ("the cache key omits the tenant id, so tenant B reads tenant A's row") is testable.

### Change exactly one thing

Make the smallest possible edit that would confirm or refute that statement. One variable. If you alter several things at once and the behavior changes, you've learned nothing about which one mattered.

### Read the result honestly

- Confirmed? → proceed to Stage 4.
- Refuted? → discard it and write a *new* hypothesis. Do **not** pile a second change on top of the first.

### Admit the gaps

If part of the system is opaque to you, say so plainly — "I don't understand how X is wired." Don't paper over the gap with a plausible-sounding guess. Read more, or ask the user. Pretending to know is how bad fixes ship.

---

## Stage 4 — Fix and Confirm

Goal: repair the actual cause and prove it stays repaired.

### Pin the bug with a test first

Before editing the fix, write the smallest automated test that fails because of this bug. Lean on the `test-driven-development` skill. A red test now becomes your proof later.

### Make one targeted change

Fix the root cause you identified — and only that. No opportunistic cleanups, no "while I'm in here" refactors riding along. Bundled changes muddy what actually resolved the issue.

### Prove it

```bash
pytest tests/test_module.py::test_regression -v   # the bug's test now passes
pytest tests/ -q                                  # nothing else broke
```

### When the fix fails — count your attempts

Stop and tally how many distinct fixes you've now tried.

- **Fewer than three:** go back to Stage 1 and re-investigate with what you just learned. New information, new pass.
- **Three or more:** stop fixing. Three failed attempts is a signal, not a setback — see below.

Never throw a fourth fix at the wall without first questioning the design.

### Three strikes means the design, not the bug

These signs say the problem is structural, not local:

- Every fix uncovers more hidden coupling or shared state somewhere new.
- A "real" fix would require sweeping rework.
- Patching one spot breaks another.

When that pattern shows up, step back and ask the hard questions:

- Is this approach actually sound, or are we propping it up out of habit?
- Would reworking the structure cost less than the next ten symptom patches?

Raise this with the user before continuing. This isn't a wrong guess — it's the wrong foundation, and no amount of guessing fixes a foundation.

---

## Warning Signs You're Guessing

Catch yourself mid-thought. Any of these means you've left the routine:

- "I'll patch it now and figure out the cause later."
- "Let me just flip X and rerun."
- "I'll change a few things at once and see what sticks."
- "I'll skip the test and eyeball it."
- "It's almost certainly X."
- "Not sure why, but this'll probably do it."
- "The reference does it this way, but I'll wing my own version."
- Listing fixes before you've traced a single value.
- "Just one more attempt" — after two have already failed.
- Each attempt breaks something new and unrelated.

Every one of these is a cue to **stop and return to Stage 1**. After three failed fixes, escalate to questioning the architecture (Stage 4, last step).

## Excuses and the Truth Behind Them

| What you tell yourself | What's actually true |
|------------------------|----------------------|
| "Too small to bother investigating." | Small bugs have causes too — and the routine is quick when the cause is small. |
| "No time, it's an emergency." | Investigation beats thrashing on the clock. Guessing is the slow path. |
| "I'll patch now, diagnose later." | The first patch sets your trajectory. Start clean. |
| "I'll add the test once it works." | A fix without a test doesn't hold. The test is the proof. |
| "Batching changes is faster." | You lose the ability to attribute the result, and you seed new bugs. |
| "The reference is long; I'll adapt." | Half-read references guarantee subtle defects. Read it whole. |
| "I can see the problem." | Seeing the symptom is not understanding the cause. |
| "One more try." (post two failures) | Three misses points at the design. Stop patching, question the structure. |

## At a Glance

| Stage | What you do | Done when |
|-------|-------------|-----------|
| **1. Establish Facts** | Read errors, reproduce, review changes, instrument seams, trace values | You can explain what and why |
| **2. Contrast** | Find a working analog, compare exhaustively, map dependencies | You have a difference list |
| **3. Probe** | Write one hypothesis, test with one change | Hypothesis confirmed or replaced |
| **4. Fix & Confirm** | Add a failing test, make one fix, run the suite | Bug gone, suite green |

## Flowly Tool Integration

### During investigation

- **`exec`** — run `rg`/`grep`/`find` to trace calls and find patterns, run tests, inspect git history
- **`read_file`** — open source with line numbers for precise reading
- **`web_search`/`browser_tab`** — look up error strings and library documentation

### Delegating an investigation

For a tangled, multi-component failure, hand the investigation to a subagent with `delegate_to`. Give it the exact failing command, the complete error text, and the suspect file paths, and tell it to follow this skill — its job is to investigate and report back, not to apply a fix.

### Pairing with test-driven-development

1. Capture the bug in a failing test (RED).
2. Run this debugging routine to find the true cause.
3. Repair the cause so the test passes (GREEN).
4. The test now guards against the bug returning.

## What It Buys You

- A diagnosed fix lands in minutes; a guess-and-check spiral burns hours.
- First-attempt fixes become the norm rather than the exception.
- Collateral bugs from blind edits drop toward zero.

**Investigate before you edit. Every time.**
