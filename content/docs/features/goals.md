---
title: Standing goals
eyebrow: Features
description: Give Flowly an objective it keeps working toward across turns. A judge decides after every turn whether the work is done, quality gates can make that decision deterministic, and the goal survives restarts.
group: Automation
---

A **standing goal** is an objective that outlives a single turn. Instead of answering once and waiting for you, Flowly keeps taking the next concrete step — judging its own progress after each turn — until the goal is achieved, it needs you, or its turn budget runs out.

Everything else stays exactly as it is. A goal turn is an ordinary turn: it streams live, shows tool activity, respects Stop, is stored in history, and reaches every device the conversation is open on. The only difference is that when it finishes, something decides whether another turn should follow.

> [!NOTE]
> A goal is **only ever created when you ask for one** — with `/goal`, or by telling the agent in plain language. Ordinary conversation never turns into a standing goal, and the agent is instructed never to invent one.

## Setting a goal

```text
/goal ship the auth migration and keep the tests green
```

That command *is* the first turn. Flowly writes the goal, the goal bar appears above the composer, and the same turn starts working — identical to having typed the objective as a normal message.

You can also just ask: *"keep working on this until the tests pass"*. The agent calls its `goal` tool, which sets the goal and continues in the same reply.

### The completion contract

A goal carries a **completion contract** — five fields that define what "done" means:

| Field | Meaning |
| --- | --- |
| `outcome` | What must exist or be true at the end. |
| `verification` | How completion is proven, concretely. |
| `constraints` | What must not be violated on the way. |
| `boundaries` | What is in and out of scope. |
| `stop_when` | The condition that ends the work. |

There are three ways to get one:

- **Inline fields.** Write them in the goal text and they are used verbatim:

  ```text
  /goal ship the auth migration
  verify: pytest -q passes and /login still returns 200
  constraints: do not touch the billing module
  ```

  Each field accepts several labels: `outcome:` / `goal:` / `done when:`, `verify:` / `verification:` / `evidence:` / `proof:`, `constraints:` / `must not:` / `preserve:`, `boundaries:` / `scope:` / `files:`, and `stop when:` / `blocked:` / `give up when:`. Everything that is not a labelled line stays as the goal's headline.

- **Drafted in the background.** With no inline fields, Flowly drafts a contract from your objective *while the first turn already runs* — the work never waits on it. If the drafter is slow, unavailable, or produces something that describes contract-writing rather than your goal, the contract is simply discarded and the goal runs free-form.

- **Drafted up front.** `/goal draft <objective>` waits for the contract before starting the work. Read it back at any time with `/goal show`.

Your goal text is always authoritative: if a drafted contract conflicts with it, the judge ignores the contract.

## How a goal turn runs

Every turn — the first and each continuation — goes through your surface's ordinary chat path:

1. Flowly builds the next prompt. For continuations it carries the goal, the contract, any extra criteria, and an instruction to take the next concrete step and verify with evidence.
2. The prompt is announced as a user-role row in the conversation, so a reply never appears without a visible cause. It is persisted like any message.
3. The turn streams — deltas, tool calls, Stop, mid-run resume — exactly like one you typed.
4. When the reply is delivered, evaluation begins.

Only after the visible reply lands does the judging start, so you always see the answer first.

## Who decides the work is not done

Not one thing — a chain, and only the last link may ever say "done":

1. **You interrupted the turn.** Stop pauses the goal; nothing is judged.
2. **Context recovery failed.** Once is tolerated; twice in a row pauses the goal with history intact.
3. **A wait barrier is active.** Parked on a background process or a deadline — no turn is spent and no judge is called.
4. **Quality gates.** Deterministic shell commands, run *before* the judge. A non-zero exit means not done — decided by code, not by a model.
5. **The judge.** A separate model call that reads the goal, the contract, extra criteria, the latest reply, and the live background-process list. It returns one of `done`, `continue`, `needs_input`, or `wait`.
6. **Deterministic guards after the verdict.** Three consecutive unparseable judge replies, five consecutive unreachable judge calls, or an exhausted turn budget each pause the goal.

Claiming success is never enough on its own: the judge is instructed to require concrete evidence against the contract's verification criterion.

### Quality gates — deterministic completion

For anything mechanical (tests, builds, linters), gates are what turn "the model thinks it's done" into "the suite is green":

```text
/goal gate add pytest -q
/goal gate add npm run typecheck
/goal gate list
/goal gate remove 1
/goal gate clear
```

- Gates run after every turn, **before** the judge. A failing gate short-circuits it entirely.
- The failing command's exit code and the tail of its output become the next turn's prompt, so the agent works from the real error.
- Each gate gets `gateMaxRetries` attempts (default 3). If the workspace has not changed since a gate's last failure, it is not re-run — the recorded failure is replayed and the attempt still counts, so a stalled agent cannot spin on an identical red suite.
- When a gate exhausts its retries, the goal pauses.
- `done` is only reachable when every gate passes.

Gate defaults: `gateTimeoutSeconds` 300, `gateMaxRetries` 3, and at most 20 gates per goal.

### Extra criteria

`/subgoal <criterion>` adds a plain-language requirement (up to 50). Criteria are injected into both the continuation prompt and the judge's evaluation, so the agent works toward them and completion accounts for them.

```text
/subgoal keep the public API unchanged
/subgoal remove 2
/subgoal clear
```

### Waiting instead of spinning

When progress genuinely depends on something asynchronous, the goal parks instead of burning turns:

- The judge can return `wait` naming a background process, its session, or a number of seconds.
- You can park it yourself with `/goal wait <pid> [reason]`, and release it with `/goal unwait`.

While parked, no turn is consumed and no judge call is made. The barrier is re-evaluated until it clears, then the loop resumes on its own.

## The turn budget

Each judged turn spends one unit of the budget (default **20**, `maxTurns`). Exhausting it **pauses** the goal — it does not delete it:

- state, history and contract are intact,
- the bar shows the paused reason,
- resuming resets the budget and continues from where it stopped.

The budget is a circuit breaker, not an estimate of how much work a goal needs: it guarantees a human checkpoint before an impossible objective, a broken environment, or a judge stuck on `continue` can spend indefinitely. Raise it in config, or just resume.

## Controls

Every surface exposes the same lifecycle, and each control means what it says — pausing or ending a goal also stops the turn that is streaming at that moment (a turn *you* typed is never touched).

| Command | What it does |
| --- | --- |
| `/goal` or `/goal status` | Show the current goal, status and budget. |
| `/goal <text>` | Set (or replace) the goal and start working in the same turn. |
| `/goal draft <text>` | Draft the completion contract first, then set the goal. |
| `/goal show` | Show the goal with its full completion contract. |
| `/goal pause` | Pause the loop; the goal stays resumable. |
| `/goal resume` | Resume and reset the turn budget. |
| `/goal clear` (`/goal stop`, `/goal done`) | End the goal for good. |
| `/goal wait <pid> [reason]` | Park the loop on a process. |
| `/goal unwait` | Clear the wait barrier. |
| `/goal gate …` | Manage quality gates (see above). |
| `/subgoal …` | Manage extra completion criteria. |

In Desktop and iOS the goal bar sits on the composer with the same controls — edit, pause/resume, and end — plus the live turn budget. The bar shows motion while a goal is working, a green check when it completes, and then folds away on its own.

## Durability

- **State lives on disk**, one record per conversation under `~/.flowly/goals/`, written atomically with a file lock and a revision check — two devices or two processes can act on the same goal without losing an update.
- **Restarts resume.** On startup Flowly puts every active goal back in motion; parked goals are re-armed so their barrier decides when they run. Paused, completed and cleared goals are left alone.
- **No live client is fine.** If nothing is connected, the turn still runs and its reply is queued for delivery when you come back.
- **Compaction is survivable.** A goal is not part of the conversation history it outlives.
- **`/clear` and `/new` end the goal.** A new conversation never inherits the previous one's objective.

## Configuration

Under `agents.defaults.goals` in `~/.flowly/config.json`:

| Key | Default | What it controls |
| --- | --- | --- |
| `enabled` | `true` | Whether standing goals are available at all. |
| `maxTurns` | `20` | Turn budget before a goal pauses for review. |
| `judgeProvider` | `""` | Provider for the judge. Empty inherits the conversation's. |
| `judgeModel` | `""` | Model for the judge. Empty inherits the conversation's. |
| `judgeTimeoutSeconds` | `30` | Judge request timeout. |
| `judgeMaxTokens` | `4096` | Judge reply budget — kept high so reasoning models still emit their verdict. |
| `gateTimeoutSeconds` | `300` | Per-gate command timeout. |
| `gateMaxRetries` | `3` | Attempts per gate before the goal pauses. |

> [!TIP]
> The judge runs between every pair of turns, so its latency *is* the pause you feel. With no `judgeModel` set it inherits the conversation model, which on a large one costs seconds each turn. Pointing it at a small, fast model is the single most effective tuning; Flowly logs a one-time warning naming this setting when a verdict takes too long.

## Goals, plans and the board

They solve different problems and can be used together:

- **Standing goal** — an objective pursued autonomously across turns, judged each time. No approval step.
- **[Plan mode](plan-mode.md)** — the agent proposes steps and waits for your approval before acting. When a plan is awaiting approval, an active goal parks rather than working around it.
- **[Board](/docs/features/board)** — discrete cards you or the agent run, sequentially or in parallel.
- **[Cron](/docs/features/cron)** — a prompt on a schedule, not an objective with a completion test.

## For client developers

The wire contract is served identically over the relay and a direct gateway:

- `goal.get` — the canonical snapshot for a conversation (use it on open and reconnect).
- `goal.pause`, `goal.resume`, `goal.stop` — controls; each returns the authoritative post-action snapshot.
- `goal.updated` — a conversation-scoped event carrying the full snapshot after every transition. Snapshots are revision-stamped: apply one only when its revision is newer, and treat `status: "cleared"` as "drop the goal surface".

Autonomous turns are ordinary chat runs. They announce their agent-authored prompt as a `chat` event with `state: "user"` (render it as a user row), then stream and settle exactly like a turn the user sent. Goal lifecycle reports arrive as finals marked `goalNotice`, meant to be rendered as a system line rather than an assistant message.

## Related

- [Plan mode](/docs/features/plan-mode) — propose-and-approve, for work you want to sign off before it happens
- [Board](/docs/features/board) — discrete cards run sequentially or in parallel
- [Cron](/docs/features/cron) — a prompt on a schedule
- [Slash commands](/docs/reference/slash-commands) — `/goal` and `/subgoal` in full
- [Configuration](/docs/using-flowly/configuration) — `agents.defaults.goals`
- [File layout](/docs/reference/file-layout) — where goal state lives on disk
