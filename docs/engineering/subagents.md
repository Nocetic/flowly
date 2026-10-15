# Subagents — how they work in the core

Status: `feat/agents-v2` (core). Owner-facing summary: `content/docs/features/delegation.md`.
This document is the engineering reference: what each piece does, where it lives, and why it
is shaped the way it is. Keep it current with every change to the subagent system.

## 1. What a subagent is

A subagent is another Flowly working on one task in the background. The main agent starts it
with the `spawn` tool; the run reports back to the conversation that started it. It has its own
model run, its own (restricted) tools and its own transcript, and costs a full run of its own.

**Nothing in the harness pushes the main agent toward or away from delegating.** The agent
decides, or the owner asks. The prompt and tool descriptions state facts (it runs in the
background, it costs a full run, it reports back) — never "do it yourself unless…" or "split
independent parts". The runtime only records *who asked* (see §3). History: an early prompt sent
anything over 15 s to a subagent; a later runtime rule refused a lone unrequested spawn. Both
are gone (`test_delegation_rule.py` pins the current contract).

## 2. Files

| File | Role |
|---|---|
| `flowly/agent/subagent.py` | `SubagentManager`: spawn, the child loop, messaging, waiting, delivery (announce), cancel, recovery, metrics hook |
| `flowly/agent/subagent_registry.py` | `SubagentRunRecord` + `SubagentRegistry` (`runs.json` index, result files, transcripts, file lock, pruning, `claim`), `add_usage`, `append_jsonl` |
| `flowly/agent/subagent_owner.py` | Who owns a run (`process_owner`) and whether that owner is gone (`owner_state`) |
| `flowly/agent/subagent_checkpoint.py` | A running run's checkpoint (`SubagentCheckpoint`), `ChildState`, `apply_step`, `restore_child_state`, `tool_class` |
| `flowly/agent/subagent_observation.py` | `run_view` (the wire shape of a run), `TaskEvents` (coalesced `subagent.*` events) |
| `flowly/agent/subagent_metrics.py` | One metric line per ended run (`metrics.jsonl`), `summarize` |
| `flowly/agent/delegation.py` | `DelegationTurn` (per-turn facts in a context var), owner-quote check, conversation fork, marks |
| `flowly/agent/tools/spawn.py` | The `spawn` tool |
| `flowly/agent/tools/agent_tools.py` | `agent_read`, `agent_message` (parent and child side), `agent_wait` |
| `flowly/agent/tools/sessions_list.py` | List / cancel this conversation's tasks (`run_id="all"` stops them all) |
| `flowly/agent/repetition.py` | Repetition guard used by both the main loop and the child loop |
| `flowly/agent/loop.py` | Turn wiring: `DelegationTurn` binding, system (report) turns, dispatch, plan-guard exemption |
| `flowly/channels/feature_rpc.py` | `subagents.list/get/result/transcript/cancel/stats/spawn` RPCs |
| `flowly/profile_host_contract.py` | Which subagent RPCs reach another local bot through the profile host |

## 3. Starting one: `spawn`

Arguments: `title` (shown to the owner, in their language), `task` (the brief), optional
`owner_request`, `model`, `context` (`conversation` default, or `none`).

* **Who asked.** When the owner asked for a subagent the agent passes their exact words as
  `owner_request`. `delegation.owner_quote_is_genuine` normalizes (casefold, Turkish dotted /
  dotless i folded, quotes and punctuation stripped) and requires the quote (≥ 6 chars) to be a
  substring of what the owner wrote *this turn*. Only real owner arrivals count
  (`owner_authored`); a goal turn counts the goal's objective; a voice turn counts what was said.
  Genuine → `origin="owner"`, otherwise `origin="agent"`. This only labels the run; it never
  refuses one.
* **Limits (budget guards, not delegation rules).** At most 3 non-system children running per
  conversation (`MAX_CHILDREN_PER_CONVERSATION`) and 8 overall (`_MAX_CONCURRENT_SUBAGENTS`);
  past them `spawn` returns an error the agent can read.
* **Context fork.** With `context="conversation"` the child's first message carries the
  owner/agent exchange so far (`conversation_from_history`): what was said and answered, no
  tool calls, no tool output, no `[System:` rows (reports and notices are not the owner
  speaking), summaries labeled. Newest kept, bounded to 24k chars (`render_conversation`).
* **Turn facts come from the turn, not the shared tool.** One loop serves many conversations at
  once, so origin channel, chat id, session key and model come from the `DelegationTurn` bound
  in a context var for the turn — never from the `SpawnTool` instance.
* **Batch.** Every spawn in one turn carries that turn's `turn_id` as the record's `batch_id`
  (§6).
* **Mark.** Each run gets a stable mark (`assign_mark`): 12 glyphs × 12 hues from FNV-1a of
  `"subagent:" + run_id`; siblings never share a glyph. Clients draw it everywhere the run
  appears.

The tool returns `{"status":"started","run_id","title","model","message"}`; the message tells
the agent the results arrive together as a new message, not to wait, not to redo the work.

## 4. The child run

`SubagentManager._run_subagent` → `_child_run` (tools from `_child_tools`; the loop's state is a
`ChildState`, §8 "Checkpoint"):

* **Prompt.** `_build_subagent_prompt`: one task, act with tools, report with checkable
  evidence (URLs, paths, ids), say what could not be done.
* **Tools.** A fresh restricted registry (`SubagentToolRegistry`): files, exec, web, skills,
  knowledge graph, artifact, the child-side `agent_message`. Blocked: spawning (no recursion),
  `agent_read`, `agent_wait`, cron, owner-facing I/O, external writes, GUI tools, cross-session
  reads, system/docker/process.
* **Limits = the main agent's.** `max_iterations` is the main agent's own
  (`AgentLoop.max_iterations`, passed to `SubagentManager`); there is **no wall clock**, as for
  the main agent. Each model call has a fixed timeout (`_MODEL_CALL_TIMEOUT_SECONDS`, 300 s),
  each tool call its own; a caller may still pass `timeout_seconds` (tests).
* **Repetition guard.** Same as the main loop (§9). When the child stalls, or hits the
  iteration limit, one tool-less "summarize what you found" call (the grace call) produces its
  result.
* **Messages in.** The main agent's `agent_message` lands in the child's inbox and is read at
  its next step (never interrupting a tool call). A message arriving during the final answer is
  still read (the child takes one more step).
* **Tokens.** After every model call (including the grace call) `_count_usage` adds the
  provider's `usage` to the record (`add_usage`: `prompt_tokens` → input, `completion_tokens` →
  output, `cache_read_tokens` → cacheRead, `total_tokens` or the sum → total) and publishes a
  progress event, so clients show it live. Tokens only — no price.
* **Transcript.** Each step is appended to the run's transcript (§8).

## 5. Talking to running subagents

* `agent_message(to=run_id | [ids] | "all", message)` — parent → child, this conversation's runs
  only.
* Child → parent: `agent_message(message, await_reply?)` queues a notice for the parent (with a
  `message_id`) and wakes it; with `await_reply` the child waits up to 300 s for a reply.
  Questions are delivered at once, never batched.
* `agent_read(run_id, offset, limit)` — the rest of a long result, page by page, or a result from
  an earlier turn. Its description says a result already received is not new.
* `agent_wait(run_ids?, timeout_seconds)` — rarely needed. 30 s default, 120 s max, **300 s per
  turn** in all (the turn's `DelegationTurn.waited`). Returns as soon as a child finishes or
  writes, the owner writes (`should_yield`), or at the timeout. Results it returns are marked
  `taken` and are not handed over again; saving the turn makes them `delivered` (§6, "Delivery
  commit point"), and a turn that ends without saving gives them back (`release_taken`): they
  are announced again.

## 6. Results reach the conversation — together

`SubagentManager._announce`:

1. A finished run whose batch siblings are still running is **held** (its result stays
   `pending`). A timer is armed (`_arm_batch_flush`).
2. The last sibling to finish delivers every held result in **one notice** — one turn that can
   answer the owner's request as a whole. A run alone in its turn reports at once.
3. **A slow sibling never stalls the rest:** `_BATCH_HOLD_SECONDS` (300 s) after the first
   result was held, everything held so far is delivered (`flush`), naming the siblings still
   working; their results follow in their own notice.
4. **Claiming:** members are marked `announced` before any await, so two siblings finishing
   together deliver once.
5. **Not repeated:** results already taken with `agent_wait` (`delivered`) are left out; a run
   stopped on request (`outcome == "cancelled"`) is not reported — whoever stopped it knows —
   but a stopped sibling is *named* once in the batch's notice so the agent does not wait for it.
6. The notice is a `ConversationInboundMessage` (`channel="system"`, `sender_id="subagent"`,
   `conversation_key` = the parent conversation's own key — a desktop chat's key is not
   `channel:chat_id`). If the parent is mid-turn it is queued (`subagent_announce_queue`).

Notice text: per run `[Subagent '<title>' <status> (runtime: Ns)] run_id=<id>`, tools used,
brief (≤ 600 chars), result (8 000 chars alone; 24 000 shared across a batch, at least 2 000
each), then guidance (check what matters, don't redo, answer as a whole, no run ids).

### The report turn

`AgentLoop._process_system_message` handles the notice as the conversation's own turn:

* **Concurrent dispatch.** `_dispatch_inbound` runs subagent reports (`sender_id == "subagent"`
  and a `ConversationInboundMessage`) as their own task, like web turns. The conversation's
  turn lock keeps its order; other conversations — and the owner's next message — are never
  held up behind it.
* **Already handled?** `notice_already_handled` skips a notice all of whose runs were already
  delivered (taken with `agent_wait`), or a child message already consumed.
* **Yields to the owner.** The turn is not steerable (the owner's message waits for the next
  turn), so its `DelegationTurn` has `steerable=False` and `should_yield` = "the owner's epoch
  moved" (`goal_user_epoch`, bumped when an owner message arrives, before it waits on the lock).
  `agent_wait` in such a turn returns at once with "the owner sent a new message; finish now".
* After the turn, `mark_delivered` records the runs as handled (goals wait on this, §10).
* **Compaction, like the owner's turn.** Before the turn, inside the turn lock, the same pass
  the post-turn background check runs is awaited (`_compact_before_report` →
  `_post_turn_compaction(settle=False, should_cancel=Stop)`); after it, the provider's own count
  is recorded (`_note_turn_usage`) and the post-turn compaction is scheduled. Agenda wake-ups,
  which share `_process_system_message`, get neither.

### The report turn streams (`flowly/agent/report_turn.py`)

Nobody typed a report turn, so no client started it with `chat.send`. It still streams exactly
like the owner's turn, because it runs through the surface's own chat runner — the same path a
standing goal's next turn takes:

1. `_deliver_to_parent._send` calls `SubagentManager.report_submitter` (set by `AgentLoop` to
   `_submit_report_turn`). The direct-queue path (`subagent_announce_queue`) uses the same
   `_send`, so queued notices take it too.
2. `_submit_report_turn` captures the notice's authority the way the bus does
   (`bind_bus_message`'s rule) and tries the registered runners
   (`register_report_turn_submitter`): the relay first for a conversation started on the relay
   (`parent_channel == "web"`), the direct gateway first otherwise. A runner declines when nobody
   watches the conversation on it (no bound gateway socket; no relay session).
3. **Nobody declined → the runner owns it.** `GatewayServer.run_report_turn` /
   `WebChannel.run_report_turn` mint a run id, record the command
   (`chat_commands.accept(..., {"turnOrigin": "subagent"})`) and start the turn in its own task
   (the caller is a subagent's completion and never waits on the turn lock). The notice rides
   as the turn's content with the `REPORT_TURN` marker (`{"sender_id", "chat_id"}`); the agent
   builds the same `ConversationInboundMessage(channel="system", sender_id="subagent", ...)` the
   bus would carry (`process_direct` on the direct gateway, the agent queue on the relay), so the
   turn lock, ordering, `notice_already_handled`, `_bind_subagent_turn`, `release_taken` and
   the delivery commit point are unchanged.
4. In the turn: a notice already taken with `agent_wait` ends before anything is announced
   (command record `aborted`, no event); otherwise `_on_turn_started` begins the in-flight entry
   **with no user text** and `turnOrigin: "subagent"`, and the runner sends
   `chat {state:"run_started", runId, sessionKey, turnOrigin:"subagent", hiddenPrompt:true,
   steerable:false}`. `_process_system_message` passes the runner's `stream_callback`,
   `on_iteration` and `on_tool_progress` to the tool loop, so deltas, tool steps and tool
   progress stream live; every event of the run carries `turnOrigin` and `sessionKey`.
5. The turn returns the owner turn's terminal metadata (`stream_run_id`, `session_key`, usage,
   context occupancy, `aborted`, `duration_ms`, tool messages, provider `error`) and the runner
   sends **one** final (or error) under the run id it streamed; no `push_session_message`. The
   direct runner's final also schedules the offline push.
6. **Stop** is `chat.abort(runId)`: the run has a command record and an in-flight entry, the tool
   loop polls `is_run_aborted`, and `should_yield` also yields on it. A stopped report is saved
   like a stopped owner turn (partial text, `aborted`), so its results are **delivered**: never
   announced again (the owner stopped the report; it stays in the conversation for the next
   turn). A turn that ends without saving (crash, cancellation) still gives back what it took
   (`release_taken`).
7. **Not steerable.** Its trigger is a hidden system notice and its `DelegationTurn` is not the
   owner's (`owner_authored=False`), so guidance cannot go into it: `chat.steer` answers
   `RUN_NOT_ACTIVE` and clients queue what the owner types as their next turn. The owner's
   message bumps the epoch before it waits on the lock, which ends any `agent_wait` in the
   report (`should_yield`), so the reply comes promptly.
8. **Fallback.** Nobody watching (or no runner registered — CLI, tests, plain channels): the
   notice goes over the bus exactly as before. Local clients get the reply through
   `push_session_message`, now keyed by the conversation's own key and carrying the run id the
   saved reply has (`stream_run_id`), so a client that showed the push recognises the saved row;
   and a phone registered for pushes gets the offline push.

Collaboration authority (`PROFILE_RUN_BINDING`) is dropped for system turns, as on the bus.

### Delivery commit point

A result is *delivered* only once the conversation has durably saved the turn that handled
it. Both turn-final saves — the report turn (`_process_system_message`) and the owner's turn
(`_process_message_inner`) — call `turn_reports(session_key, notice_run_ids)` (the notice's
runs plus what `agent_wait` took into the turn), write them into
`session.metadata["subagentReports"]` (`record_subagent_reports`, newest 256 kept) **in the same
save** as the turn, and only then `mark_delivered` (`pending | queued | taken → delivered`).

* A crash between that save and `mark_delivered` used to announce the result again (the owner got
  two answers). Now a restart reads the session's `subagentReports` (`committed_reports`, set by
  `AgentLoop` to `sessions.read`) and records it delivered without a notice.
* `agent_wait` used to mark results delivered before its turn was saved; a crash in between lost
  them for good. Now they are `taken`: a restart announces a `taken` result that no saved turn
  recorded, and `_process_message` calls `release_taken` under the turn lock when a turn ends —
  results it took but never saved (the turn failed, an introduction that left no trace) go back
  to `pending` and are announced again. Taken results belong to the **turn** that took them
  (`_taken[run_id] = (session_key, turn_id)`, the `DelegationTurn`'s id, bound to the inbound
  message by `_bind_subagent_turn`): an agenda wake or Board notice runs under its own
  `system:<key>` lock, alongside the owner's turn of the same conversation, and must neither
  commit nor give back what the owner's turn took. `notice_already_handled` treats `taken` as handled, so a
  queued notice for the same result is not processed meanwhile.

## 7. Stopping

* `subagents.cancel` RPC (owner, from a client) and `sessions_list(action="cancel",
  run_id=<id>|"all")` (agent). `cancel` sets the outcome `cancelled`, cancels the task and awaits
  it.
* A task cancelled before its first step has no handler of its own; the spawn task's done
  callback (`_cleanup`) finishes the record, records its metric and runs `_announce` so siblings
  holding for it are released — unless a shutdown suspended it (§11).
* `cancel` also ends a run still marked running that no live process works on (a restart left it
  to a process that does not continue runs).
* The owner's chat Stop stops the main agent's turn only, as in Codex: subagents keep running
  (they usually outlive the turn that started them). Each subagent is stopped on its own (its
  sidebar / notch "Stop", or by asking the agent).

## 8. Registry, transcripts, results

* `runs.json` (atomic writes under a `FileLock`) holds `SubagentRunRecord`s. Notable fields:
  `origin` (owner/agent/system), `mark`, `batch_id`, `usage`, `delivery_state`
  (pending → queued → taken → delivered | undelivered | not_required), `announced`, `tool_trace`,
  `result_preview/chars/available`, `owner` (below), `reannounced` (times a startup announced the
  result again).
* **Owner.** `spawn` (and `delegate_to`) stamp `owner = {instance, pid, identity, boot, host}`
  (`subagent_owner.process_owner`): the manager instance, the pid and its start identity
  (`subagent_owner.process_identity`: `/proc` start time, else `ps -o lstart` asked in the C locale
  and UTC — `ps` prints the caller's locale and zone, so a Desktop-launched gateway and a Turkish
  terminal would otherwise disagree about one live process — so a reused pid is not mistaken for
  it), the boot
  (`/proc/sys/kernel/random/boot_id`, macOS `kern.bootsessionuuid`), the host name (diagnostic
  only — macOS renames hosts with the network). `owner_state` says OURS / ALIVE / DEAD: no owner
  (records from before owners) → DEAD; another boot → DEAD; our pid → ALIVE only if that manager
  instance lives in this process; another pid → DEAD if it is gone or now has another identity.
  Anything not provably gone is ALIVE. A Flowly home is assumed to live on one machine. On
  Windows the pid is opened for a query, never signalled (`os.kill(pid, 0)` is not a probe there).
  Startup recovery only touches runs whose owner is DEAD, and takes each over with
  `registry.claim(run_id, new_owner, expected_owner)` — compare-and-set under the index lock, so of
  two processes recovering one home at once only one gets a run. A CLI or second gateway on the
  same home no longer finishes a live gateway's work as "restarted".
* Result bodies live in `runs-results/`; transcripts in `runs-transcripts/` as JSON lines
  `{seq, ts, kind, …}` with kinds `brief`, `assistant` (with `toolCalls`), `tool`,
  `message_in`, `message_out`, `result`. `read_transcript(run_id, after, limit)` pages by `seq`
  with an offset cursor cache. System runs keep no transcript. Transcripts are 0600 (older ones
  are made private when next written).
* **Torn lines.** Transcripts and `metrics.jsonl` are append-only JSON lines written through
  `append_jsonl`: when a crash left the last line without its newline, the next record starts on
  a line of its own (it used to be glued onto the fragment, losing both). Readers skip the
  fragment. After a restart a transcript's `seq` continues from the last readable step, so it
  stays gapless.
* Pruning: user-facing runs after 30 days, system or `cleanup=delete` runs after 1 day — never a
  run whose result is still undelivered.

### Checkpoint

A running run that will be continued after a restart (`resumable`: owner/agent runs of a process
with `resume_enabled` — the gateway sets it; not system, `wait`, `silent` or wall-clock runs, and
not the CLI) keeps a checkpoint: `runs-checkpoints/<sha256(run_id)>.jsonl`, 0600, append-only JSON
lines `{v, n, t, type, …}` (`subagent_checkpoint.py` lists every type). Unlike the transcript it
is what the provider saw: the system prompt and first message (brief + forked conversation) as
sent, each model reply with tool calls — full arguments, ids, `extra_content` — written **before**
its tools run, `tool_start` before a tool runs and `tool` with the exact message appended
(meta-tool answers too) plus what the repetition guard saw, messages from the main agent when they
arrive (`mail`) and when read (`step` / `inbox`), each step's context budget, the overflow trim,
the summary call and the final result. `model`, `tool_start`, `mail` and `final` are fsynced.

* **Exact rebuild.** Every change to the child's message list goes through a function the replay
  calls too (`apply_step` for the inbox and the strip/trim compaction with the step's recorded
  budget, `_answer_tool` for tool messages, the overflow trim, the grace prompt), so
  `restore_child_state(records)` rebuilds the list the provider was sent, step for step
  (`test_subagent_checkpoint.py` checks it at every model call). The repetition guard is rebuilt
  from the results it saw (a marked result is unmarked first), the search memo from the search
  results.
* **Calls.** The last reply's calls without a `tool` record come back as `open_calls`: started
  (`tool_start`, may have had effects) or not; `suspended` after a start marks it stopped by a
  clean shutdown. A reply that `agent_message` read counts as read only if that call finished.
* **Lifetime.** Deleted when the run ends (after the registry has its result); the registry's
  pruning also removes any checkpoint whose run is no longer running. Past 16 MB, or if the disk
  refuses a line, the checkpoint is dropped and the run marked not resumable — the run itself
  goes on. System runs never keep one (they hold the owner's conversation and memory).

## 9. Repetition guard (`flowly/agent/repetition.py`)

Watches *results*, not arguments. A result that says the same as one the turn (or child run)
already received — compared after clock readings, dates, elapsed times and Unix timestamps are
set aside (`_VOLATILE`) — is marked `[Identical to an earlier <tool> result in this turn …]` for
the model. After `STALL_AFTER` (4) such results in a row the next model call runs **without
tools** (main loop) or the child takes its grace call. Repeats also count as "stuck" in the main
loop's iteration checkpoint. Real changes (other numbers, pages, files) are never set aside.

## 10. Goals

A goal parked on its subagents waits on the `subagents:<key>` barrier until
`pending_for_goal` is empty: every non-system run of the conversation ended *and handled*
(`delivery_state` delivered; `taken` still waits for its turn's save), with a 600 s grace for a
result that was published but never handled (a crash between the two). The grace counts from the
later of the run's end and this process's start, so a result announced again after a long
downtime holds the goal while its report turn runs.

## 11. Restart

Design: `output/research/subagent-resume-2026-10-15/RAPOR.md` (phases 1–3 are built; deviations
are noted below).

**Shutdown suspends.** The gateway calls `subagents.shutdown()` first — right when the signal
arrives and again (a no-op) at the top of its cleanup — before MCP, cron, the gateway server and
channels close: Desktop kills a foreground gateway 3 s after SIGTERM (`flowlyai-service.ts`) and a
named agent's runtime 5 s after (`profile-runtime-supervisor.ts`), so subagents cannot wait for
the rest to close first. `stop_all` (also from `AgentLoop.stop()`) refuses new spawns; a
`resumable` run gets `suspended_at` and `activity {"phase": "suspended", "reason"}` in the
registry *before* its task is cancelled — and a `subagent.progress` event, so clients still
connected show it waiting at once (`shutdown` flushes the event queue for up to 0.5 s after the
handlers) — and its handler writes `suspended` to the checkpoint and
re-raises — no `finish`, no notice, no metric; the done callback leaves it too. It stays
`running`, as Desktop requires (a finished run never comes back to running). Any other run is
interrupted as before — except one a conversation waits for in a process that continues runs but
whose checkpoint could not be kept: it ends `interrupted` / `shutdown` with its delivery still
pending, so the next start tells the conversation (`_finish_cancelled`). `shutdown` then waits up
to 1.5 s for the handlers. A SIGKILL in between
is the crash path below, with the same result.

**Startup** (`resume_pending`, from `AgentLoop.run()`, before goals resume). Only runs whose owner
is provably gone are touched, each taken over with `claim` (§8); a process with `resume_enabled`
off (the CLI) leaves resumable runs to the gateway.

1. A run still marked running is **continued** when it is `resumable`, its checkpoint is readable
   and of this format, its last sign of life (`suspended_at`, else `updated_at`) is within
   **24 h**, and it was continued fewer than **3** times in a row without finishing a step
   (`resume_count`). It is claimed with `resume_count + 1`, `suspended_seconds +=` the downtime,
   `resumed_at`, `activity {"phase": "resuming"}`; `_resume` then:
   * answers every call of its last reply that has no result — **never running one again**: a
     call with no `tool_start` gets *"Not run: Flowly restarted before this call started. Nothing
     was done; call it again if you still need it."*; one that started gets a factual note for its
     kind (`interrupted_result`, by `tool_class` recorded at `tool_start`): read (changed nothing),
     write (the file may be complete, partial or unchanged — read it), exec (*may have finished,
     been cut off, or still be running — check its effects*; after a clean shutdown: *was stopped
     when Flowly shut down*), agent_message (may not have reached the main agent; a reply arrives
     as a message), anything else (may or may not have taken effect — check before doing it
     again). These go to the checkpoint, the messages and the transcript (`tool` items with
     `status: interrupted | not_run`); the trace's running steps become `interrupted`. They are
     not shown to the repetition guard.
   * gives back the main agent's messages it had not read;
   * writes `resumed {after: "shutdown" | "crash", attempt, interrupted, notRun}` to the checkpoint
     and `{kind: "resumed", at, attempt, after}` to the transcript;
   * starts `_run_subagent(restored=state)`: `started_at` is kept, tokens keep adding to `usage`.
     The loop continues where the checkpoint stopped: a step whose model call never answered is
     redone with the same iteration number; a summary call asked for is redone; a result already
     recorded in the checkpoint (`final`) is recorded and announced without any model call.
   A finished real tool call afterwards sets `resume_count` back to 0, so the bound counts
   restarts with no progress (a call that takes the process down every time ends after three
   continues); the iteration limit bounds the rest.
2. Past the bounds the run ends as **interrupted** and its conversation is told once (`finish(…,
   report=True)` keeps its delivery pending): `suspended_too_long` (*"Flowly was not running for
   <age> while this task ran, longer than a subagent is continued after a restart (24 h)…"*),
   `resume_failed` (*"Flowly restarted N times while this task ran without it finishing another
   step…"*), `resume_unsupported` for a checkpoint of another format (*"Flowly was updated while
   this task ran, and this version cannot continue work saved by the other one…"*). Each says
   nothing it had started was run again.
3. A run with no checkpoint (from before checkpoints, not resumable, or stopped before its first
   step) is finished as `error` (`process_restarted`) and announced, as before. No migration.
4. Finished runs whose result no saved turn handled (`pending`, `queued`, `taken`), kind
   `subagent`: recorded delivered if the session's `subagentReports` has them; otherwise, if
   they ended within **24 h** and were announced again fewer than 3 times (`reannounced`; a result
   only held for its batch does not count), their `announced` flag is reset (so their batch can
   claim them) and they are announced again — the notice says *"It finished <age> ago, before
   Flowly restarted; this result reaches you only now."* Older ones become `undelivered`. (The
   window was one hour: results from before a night with Flowly closed were dropped.) Delegated
   tasks report through their own message and are never announced again here.
5. **Batches.** A held result whose batch has a continued sibling is held again: `_announce` sees
   the sibling running. The hold timer is rebuilt from the registry with an absolute deadline —
   300 s after the first held result ended, and for a result held before the restart at least
   120 s from now (`_RESUMED_BATCH_HOLD_SECONDS`), so the continued sibling gets a moment.
6. **Goals.** A continued run is running (`ended_at` unset), so a goal parked on it keeps waiting
   (`pending_for_goal`); it ends and is reported as usual, or past the bounds is reported as
   interrupted, and the barrier opens once that report's turn is saved.

The notice of a continued run says *"(runtime: Ns, continued after a restart)"*; runtime, the
wire `duration` and the metric's `durationS` leave the downtime (`suspended_seconds`) out.
`cancel` also ends a run still marked running that no live process works on.

Recovery is per run: one run whose record or checkpoint cannot be read or written is logged and
left, and never keeps the agent from starting. A result already in the checkpoint (`final`) is
recorded whatever the bounds. A stop on request that lands in the same moment as a shutdown wins
(the run ends `cancelled`, it is not suspended).

Accepted limits: the checkpoint's `coreVersion` is recorded but not compared — within one format
the replay goes through this version's compaction, so an update that changes how old results are
compacted only changes how the rebuilt history is compacted, never which calls ran. If a
checkpoint cannot be kept *and* the disk then refuses both deleting it and marking the run not
resumable, a restart could continue from that older checkpoint.

Deviations from the design report: no `subagentResume` setting (`auto | ask | off`) and no
`subagents.resume` RPC yet (phase 4); resumed runs are not queued behind the 8-run limit (a
gateway left at most 8); the boot id joins pid and process identity in the owner; the crash-loop
count resets on a finished tool call rather than on the next model reply (a model that calls the
fatal tool again would otherwise loop forever).

## 12. Events and RPCs

* Events (`TaskEvents`, coalesced by run, newest revision wins): `subagent.started`,
  `subagent.progress` (activity, tool steps, tokens), `subagent.completed`. Wire shape is
  `run_view` (schemaVersion 2): ids, label, status, timing, origin, mark, `usage`
  (`inputTokens`, `outputTokens`, `cacheReadTokens`, `totalTokens`), tools used,
  `childSessionKey`, delivery state (`taken` included), `resumedAt` (when it last continued after
  a restart, else null) and `suspendedSeconds` (downtime left out of `duration`). `activity.phase`
  adds `suspended` (stopped by a shutdown to continue later) and `resuming` (claimed by a starting
  process, before its next step). Transcript items add `{kind: "resumed", at, attempt, after}`
  and `tool` items with `status: interrupted | not_run`.
* RPCs: `subagents.list`, `.get`, `.result`, `.transcript {runId, after, limit}` →
  `{task, items, nextSeq, more, done}`, `.cancel {runId}`, `.stats {days, includeSystem}`,
  `.spawn` (manual, origin owner).
* Through the profile host (another local bot): list, get, result, transcript, cancel, stats.
  Spawn and set_model are not exposed there.

## 13. Metrics (`flowly/agent/subagent_metrics.py`)

Every ended run appends one JSON line to `metrics.jsonl` (next to `runs.json`): `runId`, times,
`durationS`, `origin`, `kind`, `model`, `outcome`, `errorCode`, `toolCalls`, `tokens`,
`channel`. Never the brief, result or conversation. Lines are appended with `append_jsonl` (a
torn line never swallows the next). The file keeps its newest half past 8 MB, so
metrics outlive the 30-day run pruning. `subagents.stats` returns, for the last `days`:
`runs`, `byOrigin`, `outcomes`, `durationS {average, p50, p90}`, `tokens`, `toolCalls`, and
`byDay` (UTC). System runs are left out unless asked; a run recorded twice counts once.

## 14. Tests and evaluation

* Unit / integration: `test_delegation_rule.py`, `test_subagent_spawn_contract.py`,
  `test_subagent_reporting.py` (batches, report turns, waits, plan guard, stop all, search SDK),
  `test_subagent_accounting.py` (limits, tokens, slow sibling, stopped sibling, metrics, stats),
  `test_repetition_guard.py`, `test_subagent_hardening.py`, `test_subagent_conversation_key.py`,
  `test_subagent_transcript.py`, `test_subagent_messaging.py`, `test_subagent_safety.py`,
  `test_subagent_profile_transport.py`, `test_subagent_report_streaming.py` (a report turn
  streams on the direct gateway and the relay like an owner turn — `run_started`, deltas, tool
  steps, one final, command record, hidden in-flight prompt; the bus fallback; Stop and
  exactly-once delivery; a failing report gives back what it took (`release_taken`) and ends its
  run; an already-taken notice opens no run; ordering; other conversations not held up; surface
  choice and refusals; the profile host forwarding; a real authenticated gateway socket;
  compaction before and after; agenda wake-ups unchanged).
* Restarts: `test_subagent_restart.py` (torn lines, owners and claims, the delivery commit point,
  long downtime, shutdown order), `test_subagent_checkpoint.py` (the golden replay at every model
  call, each record kind, refusals, privacy, size cap), `test_subagent_resume.py` (each crash point
  rebuilt from a reference run's checkpoint and recovered in a fresh manager: no call runs twice,
  exact provider list, one notice; suspend, two processes, version skew, crash loop, long
  downtime, batches, goals, unread messages, orphans), `test_subagent_resume_process.py` (real
  processes, real SIGKILL mid-tool and mid-model-call, two recovering processes, a fatal call).
* Live model: `tests/test_agent_behavior_real.py` (`-m real_llm`, `FLOWLY_EVAL_CONFIG`,
  `FLOWLY_EVAL_ONLY=a,b`). Checks what the agent *did*: explanations answered without tools,
  "don't search" respected, a detailed single task done itself, explicit delegation starts
  exactly one, "one agent each" starts one per part, its own part is still done, and after
  starting subagents the agent ends its turn instead of waiting or polling.

## 15. Live steps whose calls are all hidden

Calls to the agent's own bookkeeping (`flowly.tool_activity.INTERNAL_TOOLS`: memory, goals,
follow-ups) are left out of the live tool panel. A step that said something and then made only
such calls is still a step boundary: the live `iteration_step` event carries `tool_calls: []`
(an empty list, `AgentLoop._emit_iteration_event`), and `inflight.append_iteration` resets the
re-entry text on any assistant event whose `tool_calls` is a list. Clients move the streamed
text out of the live bubble on that event; without it the sentence showed twice (bubble and work
log) on relay-connected desktops. Persisted history is unchanged (the projection still drops an
all-hidden `tool_calls`). Mobile clients must treat an empty `tool_calls` the same way when they
adopt this branch.

## 16. Open items

* Restarts, phase 4: an `ask` / `off` setting, a `subagents.resume` RPC, and continuing a run
  without a checkpoint from its transcript; Desktop's "resuming…" pill, the `resumed` divider and
  an offline state.
* The main agent's own turn is not continued after a restart (a report turn that crashed is
  processed again, so its own side effects may repeat — design report §4.2 D2). A child's message
  to the main agent that was not yet handled is lost in a crash.
* Relay-connected clients and subagent events: see the round-3 research
  (`output/research/agents-v2-round3-2026-10-15/`).
