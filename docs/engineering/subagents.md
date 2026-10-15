# Subagents — how they work in the core

Status: `feat/agents-v2` (core). Owner-facing summary: `content/docs/features/delegation.md`.
This document is the engineering reference: what each piece does, where it lives, and why it
is shaped the way it is. Keep it current with every change to the subagent system.

## 1. What a subagent is

A subagent is another Flowly working on one task in the background. The main agent starts it
with the `spawn` tool; the run reports back to the conversation that started it. It has its own
model run, its own (restricted) tools and its own transcript, and costs a full run of its own.
Every subagent working for the owner is a **thread** (§16): a real conversation
`subagent:<threadId>`, stored apart from the owner's, that only its parent agent writes and every
client may read. A run is one turn of its thread; the main agent gives a thread more turns with
`agent_followup` and closes it with `agent_close` (§16b).

**Nothing in the harness pushes the main agent toward or away from delegating.** The agent
decides, or the owner asks. The prompt and tool descriptions state facts (it runs in the
background, it costs a full run, it reports back) — never "do it yourself unless…" or "split
independent parts". The runtime only records *who asked* (see §3). History: an early prompt sent
anything over 15 s to a subagent; a later runtime rule refused a lone unrequested spawn. Both
are gone (`test_delegation_rule.py` pins the current contract).

## 2. Files

| File | Role |
|---|---|
| `flowly/agent/subagent.py` | `SubagentManager`: spawn, follow-ups (`followup`), closing (`close_threads`), the child loop, messaging (`send_to_child`, `message_thread`, `_stash_unread`, `_start_pending_tasks`), waiting, delivery (announce), cancel, recovery, metrics hook |
| `flowly/agent/subagent_registry.py` | `SubagentRunRecord` + `SubagentRegistry` (`runs.json` index and `threads.json` edges under one lock, result files, transcripts, pruning, `claim`, the thread cap, idle close, `close_thread`, `queue_mail`, `forget_parent`, `maintain`), `ThreadCapReached`, `add_usage`, `append_jsonl` |
| `flowly/agent/subagent_threads.py` | Threads (§16): `SubagentThreadRecord`, `decode_thread`, `thread_view`, `thread_history` / `repair_thread_history` (a thread's history as its next turn's model gets it), `SubagentThreadStore` (the thread conversations: `SessionManager` in `<home>/subagents/sessions`, raw outputs in `<home>/subagents/spill/<threadId>`, `commit_turn`, `turn_context`, `persist_context`, `display_rows`, `delete`, `prune`), the cap / idle / retention / pending-mail constants |
| `flowly/session/keys.py` | `is_subagent_thread`, `require_rpc_subagent_access` (a subagent conversation is read only, §16), `SUBAGENT_THREAD_READABLE_METHODS` |
| `flowly/session/history_rows.py` | The `chat.history` row projection shared by `chat.history` and `subagents.history` (`history_row`, `subagent_history_row`) |
| `flowly/agent/subagent_owner.py` | Who owns a run (`process_owner`) and whether that owner is gone (`owner_state`) |
| `flowly/agent/subagent_checkpoint.py` | A running turn's checkpoint (`SubagentCheckpoint`, version 3: on top of the thread's history), `ChildState` (with the run's journal), `append_message`, `apply_step` / `apply_microcompact` / `apply_compacted`, `restore_child_state`, `history_digest`, `task_index`, `open_call_ids`, `tool_class` |
| `flowly/compaction/` | The main agent's context management, which the child uses as is (§4.1): `CompactionService`, `ProviderRequestCoordinator.fit` (with `pin_at` for a later turn's task), `pruning.iteration_strip_passes` / `strip_old_tool_results` / `microcompact_tool_results`, `summarizer.collect_summary_usage` |
| `flowly/agent/subagent_observation.py` | `run_view` (the wire shape of a run), `TaskEvents` (coalesced `subagent.*` events) |
| `flowly/agent/subagent_stream.py` | A thread turn, live (§16a): `ChildStream` (journal → events, the in-flight entry, one terminal), `SubagentStreamHub` (ordered, non-blocking delivery to the transports' watchers), `child_event_access` |
| `flowly/agent/subagent_metrics.py` | One metric line per ended run (`metrics.jsonl`), `summarize` |
| `flowly/agent/delegation.py` | `DelegationTurn` (per-turn facts in a context var), owner-quote check, conversation fork, marks |
| `flowly/agent/tools/spawn.py` | The `spawn` tool |
| `flowly/agent/tools/agent_tools.py` | `agent_read`, `agent_message` (parent and child side), `agent_followup`, `agent_close`, `agent_wait`; `resolve_own_run`, `resolve_own_thread` |
| `flowly/agent/tools/sessions_list.py` | List this conversation's subagents (threads) and tasks / cancel tasks (`run_id="all"` stops them all) |
| `flowly/agent/repetition.py` | Repetition guard used by both the main loop and the child loop |
| `flowly/agent/loop.py` | Turn wiring: `DelegationTurn` binding, system (report) turns, dispatch, plan-guard exemption |
| `flowly/channels/feature_rpc.py` | `subagents.list/get/result/transcript/history/watch/unwatch/cancel/stats/spawn` RPCs; the read-only refusal for every feature RPC (§16) |
| `flowly/profile_host_contract.py` | Which subagent RPCs reach another local bot through the profile host; `subagent:` keys only on reads; the conversation a `subagents.watch` binds |
| `flowly/gateway/server.py`, `flowly/channels/web.py` | The stream's transports (§16a): watchers per subagent conversation, leases, cleanup on disconnect |

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

The tool returns `{"status":"started","run_id","agent_id","title","model","message"}` (`agent_id`
is the thread's id, the first run's; no `agent_id` for a run with no thread); the message tells
the agent the results arrive together as a new message, not to wait, not to redo the work.

## 4. The child run

`SubagentManager._run_subagent` → `_child_run` (tools from `_child_tools`; the loop's state is a
`ChildState`, §8 "Checkpoint"):

* **Prompt.** `_build_subagent_prompt`: one task, act with tools, report with checkable
  evidence (URLs, paths, ids), say what could not be done. A later turn of the thread keeps the
  prompt its first turn saved (§16b).
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
* **Tool results.** What the model gets of a result is cut to the main agent's per-tool limit
  (`loop._TOOL_MAX_CHARS`: 8,000 by default, `web_fetch` 6,000, `web_search` 4,000, …) by
  `_cap_tool_result`; past it the full output is spilled — into the run's thread
  (`<home>/subagents/spill/<threadId>/`, 0700/0600, kept as long as the thread's conversation) or,
  for a run with no thread, the shared temp spill dir — and the text ends with the spill pointer
  (`build_spill_pointer`) `read_file` can follow; a `read_file` result points back at its own
  offset/limit. It used to be a bare 4,000-character cut with nothing kept.
* **Journal.** Every message the run adds goes through `append_message` (live and replay): to
  the working list the provider is sent, and to `ChildState.journal`, which is never cut by a step
  or a fitting. At the end the journal is the run's turn in its thread (§16).
* **Tokens.** After every model call (including the grace call, an overflow retry and each call a
  summary made, §4.1) `_count_usage` adds the provider's `usage` to the record (`add_usage`:
  `prompt_tokens` → input, `completion_tokens` → output, `cache_read_tokens` → cacheRead,
  `total_tokens` or the sum → total) and publishes a progress event, so clients show it live.
  Tokens only — no price.
* **Transcript.** Each step is appended to the run's transcript (§8).

### 4.1 Context management — the main agent's

The child has no context management of its own. `SubagentManager` gets the main agent's
`CompactionService` (`AgentLoop` builds it first and passes `compaction=`; without one — tests,
embeddings — a stateless service with the same defaults) and drives the same pieces the main
loop's tool loop does, against the window of **the child's model**:

* **Window, threshold, reserve.** `effective_context_window_for(model)`: the catalog window
  (else the family table, else the configured window; a pinned `compaction.contextWindow` wins),
  clamped to the Flowly proxy's per-model cap and to any input ceiling a provider taught the
  service. Threshold = window − `effective_reserve_tokens_for(model)` (the configured reserve,
  at most 35 % of the window, at least 512). A request — messages **and the real tool schemas**
  (`ProviderRequestCoordinator.budget`) — must fit the threshold. The reply budget
  (`subagent_output_budget`: ≤ 16,384, ≤ 25 % of the window, ≤ the reserve) stays inside the
  reserve, so request + reply fit one window; the grace call asks for at most 4,096.
* **Each step** cuts old tool results on the main loop's schedule (`iteration_strip_passes`:
  from iteration 6 all but the last 3 to 300 chars, from 9 all but the last 2 to 150).
* **Each request** (`_call_model` → `_fit_request` → `ProviderRequestCoordinator.fit`, the main
  agent's per-request fitting): under the threshold it goes out as it is. Over it the run shows
  `activity {"phase": "compacting", "iteration", "reason": "threshold"}`; microcompact cuts all
  but the newest `microcompact.keepRecentFull` tool results to `truncateChars` (spill pointers
  kept); if that is not enough, the history is **summarized** — the last resort, a model call
  (`CompactionService.compact`, `track_cycle=False`) with `CHILD_SUMMARY_INSTRUCTIONS` (the work
  so far, evidence with exact URLs/paths/ids, failures, what is not done; never "the user").
  The system prompt and the turn's task are **pinned** (`fit(pin_at=task_index(state))`, the
  task's place in the list; `pinned=1` when a recorded list no longer has it): never summarized,
  kept verbatim; the list becomes `[system, summary, brief, kept tail…]`. On a later turn the task
  follows the thread's history, which is compactable like the rest: a summary takes the place of
  what it covers, and what it keeps stays on its side of the task (`[system, summary, kept
  history…, task, kept work…]`, §16b). A later summary updates the
  earlier one (it is fed as the previous record), so there is one summary at a time. The summary
  is made by the service's own provider and model (the main agent's, like the main agent's own
  summaries) and its calls' `usage` is collected (`collect_summary_usage`, a context variable
  per task, failed attempts included) and counted toward the run.
* **A request that cannot be made to fit is not sent.** A summary that failed (provider error,
  timeout, a summary that would not shrink anything: `RequestFit.retryable`) is an ordinary
  transient error — retried within the run's error budget; anything structural (system prompt,
  brief and tool schemas alone fill the window) ends the run `input_too_large`, as the main agent
  fails closed.
* **Overflow.** A `context_overflow` / `input_too_large` rejection is handled as the main agent
  handles it (`_call_provider_with_context_recovery`): `input_too_large` teaches the service the
  provider's ceiling (`note_observed_input_limit`, the advertised limit or 90 % of the rejected
  estimate) — learned per model and shared by the main agent and every child; then the request is
  fitted with `force_reduction` (`reason: "overflow"`) and sent **once** more, only if that changed
  it. A second rejection ends the run with the overflow error.
* Per-request fitting keeps no breaker or session state (`track_cycle=False`, no lease), as for
  the main agent's own per-request fitting. The old subagent-only trimming
  (`_strip_subagent_tool_results`, `_trim_to_context_budget`, a 4K guess for tool schemas, no
  summaries, a one-off overflow trim) is gone.
* **Visible.** A summary adds a transcript item `{kind: "compacted", at, iteration, reason,
  tokensBefore, tokensAfter, summary}` (estimates of the whole request; summary clipped to 4,000
  chars) and is logged; the activity goes back to the step's phase when fitting ends.

## 5. Talking to running subagents

* `agent_message(to=agent_id | run_id | [ids] | "all", message)` — parent → child, this
  conversation's subagents only. A subagent with a task running reads it at its next step
  (`delivered`); an open one with nothing running keeps it for its next task (`queued`, the
  thread's `pending_mail`, at most 32; no task starts); a closed one refuses it (`closed`, with
  `closed_text`). A run with no thread (from before threads) answers as before (`finished`).
  `"all"`: every running one.
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

Notice text: per run `[Subagent '<title>' <status> (runtime: Ns[, task N])] run_id=<id>
agent_id=<threadId>` (`task N` for a thread's later turn; no `agent_id` for a run with no thread),
tools used, brief (≤ 600 chars), result (8 000 chars alone; 24 000 shared across a batch, at
least 2 000 each), then guidance (check what matters, don't redo, answer as a whole, no run or
agent ids) and, when a subagent of it is still open, the fact that it keeps its conversation
(`agent_followup` gives it another task, `agent_close` ends it). The notice regexes match the run
id wherever `agent_id` follows it.

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
  it. It stops the **task**; the thread stays open for the next one. `agent_close` closes the
  thread and stops its running task (§16b).
* A task cancelled before its first step has no handler of its own; the spawn task's done
  callback (`_cleanup`) finishes the record, records its metric and runs `_announce` so siblings
  holding for it are released — unless a shutdown suspended it (§11).
* `cancel` also ends a run still marked running that no live process works on (a restart left it
  to a process that does not continue runs).
* The owner's chat Stop stops the main agent's turn only, as in Codex: subagents keep running
  (they usually outlive the turn that started them). Each subagent is stopped on its own (its
  sidebar / notch "Stop", or by asking the agent).
* **Who stopped it.** `cancel(run_id, by=...)` records `stopped_by` on the run: `"owner"` from a
  client (`subagents.cancel` in `feature_rpc`, which every transport and the profile host reach;
  a Board card's cancel), `"agent"` otherwise (`sessions_list` cancel, `agent_close`). A stop the
  agent made it already knows: its run needs no delivery (`not_required`), as before. **An
  owner's stop is reported** (`_finish_stopped`: `report=True`, so `delivery_state` stays
  `pending`). Without that the main agent went on telling the owner that subagents the owner had
  stopped were "still running", and a batch the owner stopped entirely ended in silence.
  - **Notice.** `_announce` treats an owner-stopped run as a member to tell: its section reads
    `[Subagent '<title>' was stopped by the owner before it finished …] run_id=…` with the
    result `(none: the owner stopped it)`, so `notice_already_handled` and `mark_delivered` see
    it like a result. It follows the batch rules: held while a sibling still works (an owner
    stop never starts the hold clock), reported with the last sibling's result. A notice with
    only stops says so, asks for a brief word to the owner, and says not to start them again or
    do their work unless the owner asks; one with results adds a line naming the stopped ones.
    The summary reads "— stopped".
  - **A turn already running.** Before each model call (`_run_llm_tool_loop`) the loop asks
    `take_owner_stops(session_key, turn_id, known)` and appends what it returns as an ephemeral
    note (`_EPHEMERAL_NUDGE`, never saved): "While this turn ran, the owner stopped the
    subagent(s) … There is no result …". The runs it names are taken by that turn exactly as an
    `agent_wait` result is (`delivery_state="taken"`, `_taken[run_id] = (session, turn_id)`):
    the turn's save delivers them (`turn_reports` → `mark_delivered`), an unsaved turn gives them
    back (`release_taken` → announced again), and the notice already queued behind the turn is
    skipped as handled. A run the turn's own messages already report (its notice) is left to
    that notice (`known`). `_owner_stops` keeps, per conversation, the run ids not yet told, so
    a step with nothing new costs a dict lookup, not a registry read.
  - **Tools.** `agent_message` to a subagent whose last task the owner stopped says "(the owner
    stopped its last task)"; `sessions_list` lists such a subagent with "its latest task was
    stopped by the owner, with no result".
  Tests: `tests/test_subagent_reporting.py` ("The owner's own stop").

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
* **Threads.** `threads.json` (next to `runs.json`) holds `SubagentThreadRecord`s and is written
  under the same `FileLock`, in the same `_write_locked`, threads first (a crash between the two
  writes leaves a thread whose run is missing — it reads as idle — never a run naming a thread that
  was never written); it is rewritten only when it changed. A run record gains `thread_id` (empty:
  system run, delegated task, or a run from before threads), `turn` (1 = the spawn) and `trigger`
  (`spawn` | `followup`). Delivery, batching, metrics, `resumable` and `resume_count` stay per run.
  A `threads.json` that cannot be read is never overwritten and nothing is pruned on its account;
  runs carry on without threads (`_threads_unavailable`) until it is repaired.

### Checkpoint

A running turn that will be continued after a restart (`resumable`: owner/agent runs of a process
with `resume_enabled` — the gateway sets it; not system, `wait`, `silent` or wall-clock runs, and
not the CLI) keeps a checkpoint: `runs-checkpoints/<sha256(run_id)>.jsonl`, 0600, append-only JSON
lines `{v, n, t, type, …}` (`subagent_checkpoint.py` lists every type). Unlike the transcript it
is what the provider saw: the system prompt and first message (brief + forked conversation) as
sent, each model reply with tool calls — full arguments, ids, `extra_content` — written **before**
its tools run, `tool_start` before a tool runs and `tool` with the exact message appended
(meta-tool answers too) plus what the repetition guard saw, messages from the main agent when they
arrive (`mail`) and when read (`step` / `inbox`), each step's tool-result cuts (`step.strip`), each
fitting of a request (`microcompact`, `compacted`), the summary (grace) call and the final result.
`model`, `tool_start`, `mail`, `compacted` and `final` are fsynced.

* **Format (version 3).** The header names the thread's history the turn started from:
  `baseCount` (messages of `thread_history`, between the system prompt and the first message)
  and `baseDigest` (`history_digest`, SHA-256 of their canonical JSON), and `threadId`, `turn`,
  `trigger`, `sessionKey`. The checkpoint holds the **running turn only**; `restore_child_state`
  rebuilds `[system, *history[:baseCount], firstMessage]` from the thread's conversation and
  replays the lines on top, so it does not grow with the thread. A history that no longer
  matches its digest refuses the replay (`ResumeRefusal("base")` → `resume_unsupported`, §11)
  unless the turn's `final` is already recorded (then no history is needed; the state is marked
  `base_mismatch` and nothing is kept from it). `mail` may carry `mailKind: "task"` (+ `batch`,
  `origin`): a task given to the running turn; a step's `notes` hold it as `{"text", "kind":
  "task"}`. **Version 2** is version 3 with no history (`baseCount` 0) and is continued; a v2 file
  continued by this build gets v3 lines after its v2 header. Version 1 and newer versions are not.
  `step {iteration, notes, mail, strip: [[keepLast, maxOldChars], …]}`;
  `microcompact {iteration, reason, keepRecentFull, truncateChars}` — a fitting that only cut old
  tool results, recorded with the parameters it used; `compacted {iteration, reason, summarized,
  messages, tokensBefore, tokensAfter}` — a summary, recorded as **the exact list that replaced the
  old one** (a summary is a model call and cannot be computed again). The rule: deterministic
  changes are recorded with their parameters, the rest with their result. A cut whose replay
  would not reproduce exactly what was sent is recorded as `compacted` with `summarized: false`
  (the live loop checks before choosing). Version 1 (a `budget` per step, a `recovery` trim) is
  not replayed: its runs end `resume_unsupported` (§11) — v1 never shipped beyond this branch.
* **Exact rebuild.** Every change to the child's message list goes through a function the replay
  calls too (`apply_step` for the inbox and the step's cuts, `apply_microcompact` and
  `apply_compacted` for a fitted request, `_answer_tool` for tool messages, the grace prompt), so
  `restore_child_state(records)` rebuilds the list the provider was sent, call for call
  (`test_subagent_checkpoint.py` checks it at every model call, through cuts, summaries and an
  overflow retry). The repetition guard is rebuilt from the results it saw (a marked result is
  unmarked first), the search memo from the search results.
* **Crash around a summary.** After `compacted`: the restart rebuilds the summarized list and
  makes the model call with it — no summary call. Before it (the summary was paid for but not
  written): the step is rebuilt as it was, the request is fitted again and the summary made once
  more; its tokens are counted again, because they were spent again.
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

A later turn (follow-up) continues the same way: the replay rebuilds the thread's history from its
conversation, checks it against the header's digest and continues; a changed history ends it
`resume_unsupported` (*"The subagent's saved conversation no longer matches the one this task
started from…"*). A turn that a restart ends without continuing it (past the bounds, no usable
checkpoint) still takes its place in the thread (`_save_settled_turn`: the task and a closing
row with what the conversation was told, `aborted`), so a later task's history says it was
asked and did not finish.

Accepted limits: the checkpoint's `coreVersion` is recorded but not compared — within one format
the replay cuts old results through this version's functions with the recorded parameters, so an
update that changes how they cut only changes how the rebuilt history is cut, never which calls
ran; summaries are replayed from their recorded result whatever the version. If a
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
  adds `suspended` (stopped by a shutdown to continue later), `resuming` (claimed by a starting
  process, before its next step) and `compacting` (`{phase, iteration, reason: "threshold" |
  "overflow"}`, while a request is fitted — a summary call may take seconds; §4.1). Transcript items
  add `{kind: "resumed", at, attempt, after}`, `{kind: "compacted", at, iteration, reason,
  tokensBefore, tokensAfter, summary}` and `tool` items with `status: interrupted | not_run`.
* `run_view` adds (additively, `schemaVersion` stays 2) `threadId`, `turn`, `trigger`,
  `threadStatus` (`open` | `closed`) and `threadRuns`. A thread that closes (`agent_close`, the
  cap) moves its runs' revisions in the same write and their views go out again as
  `subagent.completed`, so a client's run store (which drops same-revision views) shows it closed. A subagent run from before threads has
  `threadId = runId`, `threadStatus: "closed"` and `legacy: true`; system runs and delegated tasks
  have `threadId: null`.
* RPCs: `subagents.list`, `.get`, `.result`, `.transcript {runId, after, limit}` →
  `{task, items, nextSeq, more, done}` (unchanged, still written per run), `.history` (§16),
  `.watch` / `.unwatch {threadId}` (§16a), `.cancel {runId}`, `.stats {days, includeSystem}`,
  `.spawn` (manual, origin owner).
* A running thread turn's own chat events (`run_started`, deltas, steps, one terminal) go only to
  whoever watches it (§16a); the `subagent.*` events above are unchanged.
* Through the profile host (another local bot): list, get, result, transcript, history, watch,
  unwatch, cancel, stats. Spawn and set_model are not exposed there.
* `profiles.rpc {…, ifRunning: true}` reads a named agent only while it runs: `_target_rpc` checks
  `_runtime_is_open` (as `chat.abort` does) and raises `PROFILE_NOT_RUNNING` instead of calling
  `_ensure_runtime`, so a stopped or never-started agent is not woken by what follows it in the
  background (a client's subagent list, stats, and a conversation's watch renewed every ~90 s).
  Default `false`; a non-boolean is `INVALID_PARAMS`. The default profile goes through the
  primary socket, which is always up. A host that has it says so: `profiles.capabilities()`
  `rpcIfRunning: true`, so a client sends the option only there (an older host ignores unknown
  keys and would start the agent). `subagents.unwatch` is answered by the host before any of this
  and never forwarded. Test: `tests/test_profile_host.py`
  (`test_a_read_if_running_never_starts_a_stopped_agent`).

## 13. Metrics (`flowly/agent/subagent_metrics.py`)

Every ended run appends one JSON line to `metrics.jsonl` (next to `runs.json`): `runId`, times,
`durationS`, `origin`, `kind`, `model`, `outcome`, `errorCode`, `toolCalls`, `tokens`,
`channel`, `trigger` (`spawn` | `followup`). Never the brief, result or conversation. Lines are appended with `append_jsonl` (a
torn line never swallows the next). The file keeps its newest half past 8 MB, so
metrics outlive the 30-day run pruning. `subagents.stats` returns, for the last `days`:
`runs`, `byOrigin`, `outcomes`, `durationS {average, p50, p90}`, `tokens`, `toolCalls`,
`byTrigger {spawn, followup}` (a line from before follow-ups counts as `spawn`), and `byDay`
(UTC). System runs are left out unless asked; a run recorded twice counts once.

## 14. Tests and evaluation

* Unit / integration: `test_delegation_rule.py`, `test_subagent_spawn_contract.py`,
  `test_subagent_reporting.py` (batches, report turns, waits, plan guard, stop all, search SDK),
  `test_subagent_accounting.py` (limits, tokens, slow sibling, stopped sibling, metrics, stats),
  `test_repetition_guard.py`, `test_subagent_hardening.py`, `test_subagent_conversation_key.py`,
  `test_subagent_transcript.py`, `test_subagent_messaging.py`, `test_subagent_safety.py`,
  `test_subagent_profile_transport.py`, `test_subagent_threads.py` (§16: where a thread's
  conversation lives and that no reader of the owner's conversations sees it — listing, search
  index, MCP read plane, voice recall, file tools; the conversation equals what the provider saw,
  uncut, message by message; a long tool result's full output in the thread's spill dir; notes,
  the hidden summary request, an aborted turn; `subagents.history` paging, errors, legacy runs and
  authorization; the gateway's `chat.history`; history on the gateway, the relay and the profile
  host; every write refused on each path; idle close, retention, the cap and its text; a deleted
  parent; the journal rebuilt from the checkpoint; a continued run's turn saved once, and not
  again after a crash just after saving), `test_subagent_report_streaming.py` (a report turn
  streams on the direct gateway and the relay like an owner turn — `run_started`, deltas, tool
  steps, one final, command record, hidden in-flight prompt; the bus fallback; Stop and
  exactly-once delivery; a failing report gives back what it took (`release_taken`) and ends its
  run; an already-taken notice opens no run; ordering; other conversations not held up; surface
  choice and refusals; the profile host forwarding; a real authenticated gateway socket;
  compaction before and after; agenda wake-ups unchanged).
* Follow-ups (§16b): `test_subagent_followup.py` — an idle follow-up is a new turn whose model
  gets the thread's own conversation (same system prompt, earlier task, call, result, report,
  then the labeled task), saved, reported (`task 2`, `agent_id`) and counted (metrics,
  `byTrigger`); the owner-quote label; a follow-up while running enters the turn
  (`noteKind: "task"` live and in history) and one report covers it; a task that arrives as the
  turn ends starts the next turn, is kept across a restart and starts then, and is dropped when
  the turn was stopped (owner stop, cancel) while its messages are kept; turn numbers are
  decided under the registry lock; a closed subagent (agent, deleted parent, idle) refuses tasks and
  messages with the reason; `agent_close` stops a working one with no report and tells clients;
  spawns and follow-ups of one parent turn report together; the running limits; a message kept for
  the next task; `agent_read` by `agent_id`, `sessions_list`; the new tool descriptions hold no
  steering; history repair (unit, a turn stopped mid-call, an older conversation with a call left
  open mid-history) against a strict provider-ordering check; checkpoint v3 — the golden replay at
  every model call of a follow-up turn on top of the thread's history, across a cut and a summary
  that is then kept and that turn 3 starts from; a kept summary after a stopped turn keeps the
  saved rows active (not copies); a long thread whose next task's first request is
  over the window; a restart mid follow-up (no call runs twice, exact list, one notice), a changed
  history (`resume_unsupported`), a crash after the turn was saved (no model call), a version-2
  checkpoint; a follow-up turn's stream (`run_started` with `trigger: "followup"`, `turn: 2`).
  `test_provider_request_coordinator.py` pins `pin_at` (order kept around the pin, a cut in place,
  `pin_at=1` = `pinned=1`).
* Context: `test_subagent_compaction.py` (request + reply fit the child's model for small and large
  windows, the shared service and its learned ceilings, `AgentLoop` wiring, the main loop's cut
  schedule, a request under the threshold untouched, a failed summary retried, a request that
  cannot fit never sent, an overflow compacted and retried once — not forever, a 40-step run on a
  16K model that summarizes repeatedly and finishes with every request under the threshold and
  every reply under the reserve, summary tokens per task).
* Restarts: `test_subagent_restart.py` (torn lines, owners and claims, the delivery commit point,
  long downtime, shutdown order), `test_subagent_checkpoint.py` (the golden replay at every model
  call — one run through the step cuts, an overflow summary and the grace call, one over a small
  window through a microcompact, a threshold summary and a 413 that names the provider's ceiling;
  each record kind, refusals, version 1, privacy, size cap), `test_subagent_resume.py` (each crash
  point rebuilt from a reference run's checkpoint and recovered in a fresh manager: no call runs
  twice, exact provider list, one notice; a crash just before and just after a `compacted` record;
  suspend, two processes, version skew both ways, crash loop, long downtime, batches, goals, unread
  messages, orphans), `test_subagent_resume_process.py` (real processes, real SIGKILL mid-tool and
  mid-model-call, two recovering processes, a fatal call).
* Token streaming (§16a): `test_subagent_token_streaming.py` — streamed deltas assemble to exactly
  the recorded reply (checkpoint `model` record with `extra_content`, journal, saved rows, terminal)
  and a streamed run records what a blocking run of the same replies records (requests, `model`
  records, rows, tokens, replay); a tool-call boundary takes the streamed text out of the bubble and
  the in-flight entry; a watcher arriving mid-reply gets every character once; nobody watching →
  the blocking call and the same events offered as before; a provider without streaming keeps one
  delta per reply; a stream that fails before any text falls back (no stream, a rejected key) or
  not (no credits); a reply that fails after some text is taken back (`stream_reset`) and the retry
  streams cleanly; the whole-reply timeout; a stop mid-reply; a crash mid-stream (nothing of the
  partial on disk, the restart redoes the call once with the exact list, one closing row, tokens
  once); coalescing, the deferred flush, a slow watcher never holding the run (queue ≤ 3, nothing
  dropped), the size cap; `StreamedReply` against a blocking call's fields.
* Live model: `tests/test_agent_behavior_real.py` (`-m real_llm`, `FLOWLY_EVAL_CONFIG`,
  `FLOWLY_EVAL_ONLY=a,b`). Checks what the agent *did*: explanations answered without tools,
  "don't search" respected, a detailed single task done itself, explicit delegation starts
  exactly one, "one agent each" starts one per part, its own part is still done, and after
  starting subagents the agent ends its turn instead of waiting or polling, and asked to give the
  subagent that already reported another task it calls `agent_followup` on that subagent rather
  than spawning a new one (`followup-same-agent`, a conversation seeded with the earlier report).

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

## 16. Threads: every subagent is a conversation

Design: `output/research/subagent-threads-2026-10-15/TASARIM.md` (phases 1, 2 and 4 are built;
deviations below, §16a and §16b).

**Identity.** A subagent started for the owner or by the agent (`origin` `owner` | `agent`) is a
thread whose id is its first run's `run_id`, so its conversation key is the
`childSessionKey` clients already had: `subagent:<threadId>`. System runs (self-review, Board)
get no thread: they carry the owner's conversation and memory and are never opened from a chat.

**The conversation** is a real `Session` written with the owner's machinery
(`extend_with_turn_messages`, the append-only `.full.jsonl`, `history_page`) by
`SubagentThreadStore`, a `SessionManager(sessions_dir=<home>/subagents/sessions)` with no search
indexer (dir 0700, files 0600). Being in its own directory is what keeps it out of every reader of
the owner's conversations: `sessions.list` / `list_sessions`, the `session_search` index (built
from `<home>/sessions`), the MCP read plane (`SessionReader` over the main manager), voice recall
(`latest_chat`, `search_conversations`), memory extraction and the dreamer (driven by the main
agent's turns), `flowly doctor`, profile clones and templates (`subagents/` is excluded) and the
disk cleanup (protected). The file tools deny `<home>/subagents/sessions` like `<home>/sessions`
(`filesystem._get_denied_paths`, `protected_paths`). Header metadata `subagent`:
`{threadId, parentSessionKey, origin, title, mark, model, depth, createdAt, systemPrompt, runs}` —
`runs` are the runs it saved (the idempotency record); the thread's `open`/`closed` status lives
only in `threads.json`.

**A turn** is saved by `_save_turn` when the run's result is decided, before
`registry.finish` (after the checkpoint's `final`), whatever the outcome — `ok`, `error`,
`timeout`, or stopped (`aborted`, outcome `cancelled` | `interrupted`); a run suspended by a
shutdown saves nothing until it ends. Rows, all with the run's `run_id`:

| Row | `role` | Fields |
|---|---|---|
| Task | `user` | `kind: "subagent_task"`, `author: "main_agent"`, `turn`, `trigger`; the content is the first message as sent (the forked conversation and the brief), with `brief` and `with_conversation: true` when a conversation was forked |
| Reply | `assistant` | `tool_calls` (full arguments, ids, `extra_content`) |
| Tool result | `tool` | `tool_call_id`, `name`, `status` (`ok`, `error`, `timeout`, `interrupted`, `not_run`); the content as the model got it |
| Main agent's message | `user` | `kind: "agent_note"`, `author: "main_agent"` (read at a step) |
| Summary request | `user` | `_display_hidden: true` (the model saw it; display rows leave it out) |
| Closing | `assistant` | the result, `outcome`, `usage` (the run's tokens, provider shape), `duration_ms`, `aborted` |

Every row is the message as the model first got it: the journal is independent of the working
list that steps and fittings cut (`test_the_conversation_holds_what_the_provider_saw_uncut`). A
summary made during the run is not a row (the request it fitted is the provider's business); the
journal keeps what was summarized. Saving is idempotent per run (`meta.runs`) and never fails the
run (logged). A thread whose parent was deleted saves nothing.

**Restarts.** The journal needs no new checkpoint records: `restore_child_state` rebuilds it with
the same replay (`append_message` in `apply_step`, `model`, `text`, `tool`, `phase`), stamping rows
with the records' times, so a continued run saves its whole turn — the steps before the crash, the
calls a restart answered (`interrupted` / `not_run`) and the rest — once. A crash after the turn
was saved and before the registry recorded it replays `final`, saves nothing (already in
`meta.runs`) and records the result. The checkpoint format (v2) and the resume rules (§11) are
unchanged. Runs registered before threads existed (no `thread_id`) continue as before and get no
thread.

**Edges** (`SubagentThreadRecord`, `threads.json`): `thread_id`, `parent_session_key`,
`child_session_key`, `status` (`open` | `closed`), `created_at`, `closed_at`, `closed_reason`
(`agent` — `agent_close`; `parent_deleted`, `idle`, `cap`; `owner` has no client path), `origin`,
`title`, `mark`, `model` (its latest turn's), `depth`, `runs` (oldest first), `active_run_id`,
`last_run_ended_at`, `usage` (summed over ended runs), `revision`, `updated_at`, `pending_mail`
(`[{id, text, at}]`, messages for its next task). `register(record, thread=…, open_cap=…)` writes
the run and its new thread together; a run ending (`update` with `ended_at`) clears
`active_run_id`, sets `last_run_ended_at` and adds its tokens in the same write. Reading reconciles
a thread whose run is gone or ended.

**Lifetime** (`subagent_threads` constants; decided when reading, persisted with the next write and
by `maintain()` at startup, from `resume_pending`):
* **Idle close:** an open thread with nothing running for `THREAD_IDLE_CLOSE_SECONDS` (7 days) is
  closed `idle`, `closed_at` = last activity + 7 days.
* **Cap:** a conversation keeps at most `MAX_OPEN_THREADS_PER_CONVERSATION` (16) open. `spawn`
  past it closes the thread idle longest (`cap`), under the registry lock, and the tool result says
  so as a fact: *"This conversation keeps at most 16 subagents open; starting this one closed '…',
  idle the longest. Its history stays readable. agent_close closes a subagent that is no longer
  needed."* If every open thread has a task running (`ThreadCapReached`) the spawn is refused:
  *"16 subagents of this conversation are open and all of them are working; one must finish, or
  be closed with agent_close, before another can start."* Follow-ups open no thread and are not
  counted.
* **Retention:** a closed thread is removed `THREAD_RETENTION_SECONDS` (30 days) after
  `closed_at`, with its conversation, lock and spill dir — unless one of its runs still has a
  result waiting to be delivered. Files are pruned by name only (`subagent_<uuid>[.full].jsonl`,
  `spill/<uuid>/`), whenever the set of threads changed.
* **Deleted parent:** `AgentLoop.delete_session` → `SubagentManager.forget_conversation`: the
  conversation's threads close (`parent_deleted`) and their conversations and raw outputs are
  removed at once (they hold the forked conversation). The records stay until retention. A run
  still working for it finishes as before (it is not cancelled) but saves nothing.

**Read only.** Only the parent agent writes a thread, through its runs. Every RPC that names a
`subagent:` conversation (`sessionKey`; `key` for `chat.` / `sessions.` / `goal.` calls) is refused
with `SUBAGENT_THREAD_READ_ONLY` unless it is a read (`SUBAGENT_THREAD_READABLE_METHODS`:
`chat.history`, `chat.inflight`, `subagents.history`, `goal.get`, `plan.get`, `plan.list`,
`plan.mode.get`): at the top of the direct gateway's `_dispatch_ws_rpc`, of the relay's
`_dispatch_rpc`, in `feature_rpc.dispatch` (every transport's feature RPCs, including
`subagents.spawn {sessionKey}` and `cron.add`), and in `validate_profile_rpc` (the profile host
accepts `subagent:` keys only for those reads; `chat.send`, `chat.steer`, `chat.compact`,
`sessions.delete`, `sessions.model.set`… never reach the named agent).

**`subagents.history {threadId | runId, limit?: 1..200 = 50, before?}`** →
`{schemaVersion: 1, thread: thread_view, sessionKey, readOnly: true, messages, historyPageVersion,
hasOlder, before}`. Rows are `chat.history` rows (`history_row`) plus `kind`, `author`, `turn`,
`trigger`, `brief` / `withConversation` (task), `status` (tool), `outcome` (closing), oldest first;
the page never splits a turn's tool chain but stops at 200 rows (`history_page`). It never creates a
session. `thread_view` (schemaVersion 1): `threadId`, `childSessionKey`, `parentSessionKey`,
`status`, `title`, `mark`, `model`, `origin`, `depth`, `createdAt`, `closedAt`, `closedReason`,
`activeRunId`, `lastRunEndedAt`, `turns`, `runIds`, `runs` (run views), `usage`. A turn appears once
its run ended; the running one is not in it (phase 2 streams it). Errors: `INVALID` (neither id, or
not a subagent id, or a run of another thread), `NOT_FOUND` (unknown; a system run or delegated
task), `SUBAGENT_THREAD_LEGACY` (a subagent from before threads — its transcript is what there is),
`INVALID_HISTORY_CURSOR`, `UNAVAILABLE`. **Who may read:** whoever may read the conversation that
started it (`require_session_file` on the parent, so an account-owned voice conversation's
subagents are its owner's only). Served on the direct gateway, the relay and the profile host
(`PROFILE_RPC_TIMEOUTS`). On the direct gateway `chat.history {sessionKey: subagent:…}` reads the
same store (`readOnly: true`, never creating a session); on the relay `chat.history` stays empty
(Firestore), so clients use `subagents.history` everywhere.

**Migration.** Nothing is rewritten: an older `runs.json` record decodes with `thread_id ""`,
`turn 1`, `trigger "spawn"`; such runs have no thread or conversation, their transcripts open as
before, and `threads.json` is created only once a thread exists. Older Desktops see the same events
and RPCs with additive fields, and no new events.

**Deviations from the design.**
* *Spill retention* (design §11 question 2): raw outputs live in the thread's own spill dir and go
  with its conversation (owner decision: kept 30 days after close), not in the 7-day temp dir.
* *The cap.* The design refuses a spawn past 16 open threads and points to `agent_close`; refusing
  would block a busy conversation's delegation for up to a week. Phase 1 closes the thread idle
  longest instead and tells the agent; it refuses only when all 16 are working. Phase 4 keeps that
  (owner decision) and adds `agent_close` to both texts as a fact.
* No `subagents.close` RPC: the owner decided no client writes a thread, and closing is the
  parent agent's (phase 4 `agent_close`) — or idle, cap, deletion.
* A deleted parent's running turns are not cancelled (they were not before either); their turn is
  not saved.
* The working context (`<key>.jsonl`) holds the turns uncut unless a turn summarized its
  context; then the summary is kept for the next turn (§16b, `persist_context`).
* `SessionManager.read_run_result` keeps its fixed `<home>/sessions` path: nothing reads a
  thread's result through it (the result is in `runs-results/`).

**Phase 2** (live turns) is built: §16a.

**Phase 4** (follow-ups, closing) is built: §16b.

## 16a. A thread turn, live (`flowly/agent/subagent_stream.py`)

Design §7 (phase 2). A turn's rows are saved when it ends (§16); while it runs it streams the
events an owner's turn has, under the thread's conversation key `K = subagent:<threadId>` and the
run's id `R`, to whoever watches the thread. Wire: `docs/chat-wire-protocol.md` §4.8.

**Events** (every one carries `sessionKey: K`, `runId: R`, `turnOrigin: "subagent_task"`; the
parent's report turn keeps `turnOrigin: "subagent"` under the parent's key):

| Event | When | Data |
|---|---|---|
| `chat` `run_started` | The run's loop begins (a run continued after a restart: again, `resumed: true`) | `hiddenPrompt: false`, `steerable: false`, `prompt` (the brief; a follow-up's task), `promptAuthor: "main_agent"`, `threadId`, `turn`, `trigger` (`spawn` \| `followup`), `parentSessionKey`, `title`, `mark`, `model`, `withConversation` (first turn only), `startedAt` (ms), `resumed?` |
| `agent` | The model's text: token by token while somebody watches and the provider streams (coalesced, §16a "Token streaming"), else one delta per reply that has text (the closing reply too) | `stream: "assistant"`, `data: {text}` — incremental, the owner's turn's convention |
| `chat` `stream_reset` | A streamed reply failed (error, timeout, an empty reply) and is not recorded; the call is made again or the run ends | `text`: the live text from here on (what it was before that reply) |
| `chat` `iteration_step` | Each reply with tool calls, each tool result | The main loop's shape (`flowly.tool_activity.iteration_event_payload`): `iterationIdx`, `role`, `content`, `tool_calls?`, `tool_call_id?`, `name?`, `tool_activity?`; a tool result not `ok` adds `status` (`error`, `timeout`, `interrupted`, `not_run`). A reply without calls mid-run (the main agent wrote meanwhile) is a step with `tool_calls: []` |
| `chat` `tool_progress` | A call is queued, runs, ends | `ToolProgress`'s shape: `iterationIdx`, `revision`, `call {id, index, name, arguments, state, …}` |
| `chat` `agent_note` | The child read a message from the main agent at a step | `iterationIdx`, `author: "main_agent"`, `content` (as saved), `noteKind: "task"` for a task given to the running turn |
| `compaction` | A summary fits the run's context (a cut alone is none) | `phase` `started` → `completed` \| `failed`, `compactionId`, `tokensBefore`, `tokensAfter`, `messagesRemoved` |
| `chat` `final` | Outcome `ok`, or stopped (`cancelled` / `interrupted`: `aborted: true`, empty text) | `outcome`, `message {role, content: [{type: "text", text}], usage}` (the run's tokens), `model`, `durationMs`, `completedAt` |
| `chat` `error` | Outcome `error` / `timeout` | `outcome`, `errorCode` (the run's), `errorTitle`, `errorMessage` (the result text), `retryable: false`, `usage`, `model`, `durationMs`, `completedAt` |

Exactly one terminal per run, sent after the turn was saved and the registry recorded it (a
reload of `subagents.history` then has the turn). A run suspended by a clean shutdown sends no
terminal (it continues after the restart). `iterationIdx` is the row's index in the run's
journal, so it is stable across a restart and matches the saved rows' order.

**Built from the journal.** `ChildStream.sync` turns the journal rows (`ChildState.journal`, what
the turn saves) it has not seen into events: the live turn and the saved one are the same rows.
`_child_run` syncs after a step's notes, before each tool call (what the reply said, the results so
far), after the calls and after a mid-run reply; `tool_started` marks a call running; `answer`
sends the closing reply. A continued run replays the restored journal into the in-flight entry
without sending it (watchers re-enter through `chat.inflight` / `subagents.watch`), then streams
from there. Token streaming (phase 6, below) feeds `ChildStream.delta` through its coalescer.

**Token streaming** (phase 6; `SubagentManager._call_model` → `_stream_model`,
`ChildStream.begin_reply` / `stream_text` / `end_reply` / `discard_reply`,
`flowly/providers/streamed_reply.py`).

* **When.** Decided per model call, when it is dispatched (after fitting, §4.1): the run has a
  live stream, the provider streams (its class overrides `LLMProvider.chat_stream`;
  `_streams_tokens`) and somebody watches the thread now (`ChildStream.watched` →
  `SubagentStreamHub.watched`: each transport's `subagent_stream_watched`, one dict lookup).
  Otherwise the call is exactly the blocking `provider.chat(…, max_tokens, timeout)` it was —
  nobody watching costs one watch check per model call (≈0.4 µs measured with the gateway and the
  relay channel as transports) and nothing else; its reply goes out as one delta, as before. A
  watcher arriving during a blocking call gets that reply whole when it is recorded; one leaving
  during a streamed call changes nothing (the deltas reach nobody).
* **Why only when watched.** Exact replay holds either way: the checkpoint's `model` record is
  the assembled reply, written after the whole reply arrived and before any tool runs (a crash
  mid-stream leaves `step` without `model`, so the call is made again on resume — as for a
  blocking call; partial text is never written anywhere). The deciding difference is the call
  itself: the providers' blocking `chat` takes the run's per-call `timeout` and rotates a rejected
  key, their streams do not. Unwatched runs — almost all of them — keep today's call unchanged.
* **The main agent's path.** `provider.chat_stream(messages, tools, model, max_tokens)` consumed
  with `StreamedReply` — the class `AgentLoop._chat_with_stream` now uses too (extracted from it,
  same behavior) — so the reply is assembled exactly as the main agent's: the accumulated text, the
  final chunk's tool calls (`extra_content` included), finish reason, usage, provider state; error
  text never becomes reply text. A failed stream's response carries the provider's error text (not
  the partial reply) so `classify_response` routes it as for a blocking call. `timeout` bounds the
  whole reply (`asyncio.timeout`), as it bounds a blocking call; past it the call is a transient
  error.
* **Fallback.** A stream that failed before any text went out falls back to one blocking call when
  that may succeed (the main loop's rule — a retryable category: a model or route that does not
  stream, a transient fault, a rate limit — plus a rejected key, which the blocking call rotates).
  A request too large, no credits or an unreadable image are not repeated (`_call_model` handles
  the first as before). After some text went out there is no fallback: the run's own error
  handling (backoff, retry, error budget) takes the reply as it takes any failed call.
* **The wire.** `agent {runId, sessionKey, turnOrigin, stream: "assistant", data: {text}}` with
  incremental text, as an owner's turn. `inflight.text` accumulates it (`chat.inflight` /
  `subagents.watch` snapshots hold the partial reply); `delta` changes the entry and publishes in
  one synchronous step, so a watcher whose snapshot is taken mid-reply gets every character once
  (snapshot, then the deltas after it). A recorded reply is *committed* (`_commit`): its text
  streamed already, so nothing more is sent (a recorded text that differs — never with the shared
  assembly — is corrected by a `stream_reset` and the text); a reply that did not stream goes out
  whole. Then the step's `iteration_step` with `tool_calls` (an empty list included) moves the text
  out of the live bubble and resets `inflight.text`, exactly as before. A streamed reply that is
  not recorded — an error, a timeout, an empty or blank reply the run retries, an empty summary —
  is taken back: `chat {state: "stream_reset", text}` with the live text before that reply, and the
  in-flight text set to it in the same step. A run stopped mid-reply sends nothing more of it; its
  one terminal follows.
* **Coalescing and backpressure.** `stream_text` buffers tokens in the run; a delta goes out at most
  every 50 ms (`_DELTA_INTERVAL_SECONDS`; the first token at once), and while the conversation's
  queue still holds events (`SubagentStreamHub.backlog`) the tokens wait in the run — up to
  16,384 characters (`_DELTA_MAX_CHARS`) — so however slow a watcher is, the hub's queue holds
  about one delta of a long reply, never one event per token, and does not reach its 1,024 limit
  on text. A timer sends what is left when tokens pause; `end_reply` sends the rest when the call
  returns. `stream_text` is synchronous: the run never waits for a socket. Measured: 9 µs per token
  in the run's task (assembly 1.8 µs, the rest the coalescer and amortized deltas), a 1,000-token
  reply at 500 tokens/s → 54 deltas; a 20,000-token burst → 11 deltas.
* **Tokens.** The usage of the final chunk, counted once per call through `_count_usage` (both
  calls when a stream fell back), as the main agent; a stream cut short counts nothing (no usage
  arrived), as a blocking call that failed.
* **Unchanged.** Fitting before the call (§4.1), the overflow retry (a failed streamed attempt is
  taken back before the retry streams), the checkpoint records and replay, the journal, saved turns,
  the terminal, compaction events, report turns, follow-ups.

**In flight.** `inflight.begin(K, R, user="", turn_origin="subagent_task", fields=…)`:
`chat.inflight {sessionKey: K}` returns `{runId, text, user: "", iterations, toolProgress?,
turnOrigin, prompt, promptAuthor, subagent: {threadId, turn, trigger, parentSessionKey, title,
mark, model, withConversation}}` — no user row; the task is the brief. `iterations` holds
`iteration_step` and `agent_note` events in order. `chat.inflight` on a `subagent:` key is
allowed to whoever may read the parent conversation (else `NOT_FOUND`); a key with no thread
answers `inflight: null`.

**`subagents.watch {threadId}`** → `{schemaVersion: 1, ok, watching: true, threadId, sessionKey,
status, activeRunId, leaseSeconds: 120, streamVersion: 1, inflight, compaction}`. Who may: whoever
may read the conversation that started it (as `subagents.history`). The transport binds the caller
in the same step as it takes the `inflight` snapshot, so the stream continues exactly where the
snapshot ends (an event the stream sends is never in the snapshot; through the profile host an
event may arrive before the reply — apply the snapshot, then the events in order). Calling again
renews the lease (clients: every ~90 s); `subagents.unwatch {threadId}` (idempotent) or a
disconnect ends it. Errors as `subagents.history` (`INVALID`, `NOT_FOUND`,
`SUBAGENT_THREAD_LEGACY`, `UNAVAILABLE`). `system.capabilities` has `subagentStreamVersion: 1`.

**Delivery.** `SubagentManager.stream_sink` is a `SubagentStreamHub` (`gateway_cmd` sets it with the
gateway server and the web channel as transports). Per event the hub asks each transport,
synchronously, whom it would send to (`prepare_subagent_stream`), queues the deliveries per
conversation and sends them in order from its own task: the child never waits for a socket, a
slow watcher holds back only its conversation's queue (1,024 events; past it new non-terminal
events are dropped — the in-flight entry has them; a terminal never is), each delivery is bounded
(10 s). With nobody watching a transport answers from one dict lookup and nothing is sent.

* **Direct gateway** (local Desktop, remote clients): plain `chat` / `agent` / `compaction`
  events to the sockets watching `K` (leases, at most 32 conversations per socket — the oldest
  lease gives way — and 1,024 in all) and to a socket `chat.inflight` bound to `K`. A disconnect
  drops the socket's watches.
* **Relay**: `{"type": "event", "event": "subagent.stream", "sessionId", "data": {"sessionKey": K,
  "event": "chat" | "agent" | "compaction", "data": <the event's data>}}` to each watching relay
  session (same lease and bounds; `browser-disconnected` and a relay reconnect drop them; no
  offline replay). The relay service hands an event name it does not know to that session only
  and persists nothing of it (`forwardAgentMessage` → `resolveStreamTargets`: not in
  `CONVERSATION_EVENT_NAMES`, so the originating socket; Firestore writes are keyed on
  `event === "chat"` / `"compaction"` / `"queue"` / `artifact.*`). A plain `chat` event would have
  been written into the parent's conversation (`tool_turns/`, `messages/`).
* **Named agents** (profile host): `profiles.rpc {name, method: "subagents.watch", params:
  {threadId}}` reaches the agent's runtime, which binds the host's socket; the client's transport
  binds `(profile, K)` (`profile_rpc_session_key`), so the runtime's events reach it as
  `profile.event {profile, type: "chat" | "agent" | "compaction", data}` — on the gateway and on
  the relay alike (`profile.event` is not stored by the relay either). The default profile goes
  through the host's private socket the same way. `subagents.unwatch` through the host releases
  only the caller's binding and is not forwarded (the runtime's one watcher is the host's socket,
  shared by every client; its lease runs out once nobody renews it). The host treats `K` events
  as nobody's turn: no busy state (`active_runs`), no roster activity, no attention refresh, no
  group routing.

**Authority.** The subscription is the gate (authorized as above). Each event also carries the
parent conversation's authority (`child_event_access`): an ordinary conversation's events are
open to the watching sockets; an account-owned (voice) conversation's go to that account's
recipients only, on every hop (signed across the profile runtime hop). Delivery never reads or
locks anything among the owner's conversations for a `subagent:` key.

**Unchanged:** batching, delivery and the parent's report turns (`turnOrigin: "subagent"` on the
parent's key), `subagent.*` events, `subagents.transcript`. Older clients never bind a `subagent:`
conversation, so they get none of these events.

## 16b. Follow-ups and closing (phase 4)

Design §6 (Codex `followup_task` semantics). A thread takes more than one task; every task is a
run (`trigger: "followup"`, `turn` = its number) of the same thread and conversation key, so
delivery, batching, metrics, cancel and resume stay per run.

**Tools** (main agent; all blocked inside a child). Targets are this conversation's subagents by
`agent_id` (the thread id) or the run id of any of its tasks, full or a unique prefix of ≥ 8
characters (`resolve_own_thread`); another conversation's read as not found; a subagent from
before threads has no conversation and takes no task.

| Tool | Does |
|---|---|
| `agent_followup(to, task, owner_request?, model?)` | `SubagentManager.followup`. **Idle and open:** registers a run of the thread (`register` appends it to `runs`, sets `active_run_id`, moves `pending_mail` into the run's `notes` in the same write) and starts it; returns `{status:"started", run_id, agent_id, turn, title, model, message}` (the spawn's wording). **Working:** the task goes to the running turn's inbox as a task (`send_to_child(kind="task")`, durable `mail` line), read at its next step as `[Another task from the main agent]` (`agent_note`, `noteKind: "task"`; a label of its own, so the turn's own task — `[Task from the main agent]` — is never mistaken for a note repeating it); returns `{status:"added_to_running_task", run_id, agent_id, turn, message}`. **Closed:** `{status:"closed", agent_id, title, error: closed_text}` — a fact per reason (`agent`, `owner`, `idle`, `cap`, `parent_deleted` — the last always refused, its conversation is gone). Also `rejected` (the limits), `busy` (recorded running with no process here, or another task just started). |
| `agent_close(to: id \| [ids] \| "all")` | `close_threads`: `registry.close_thread(reason="agent")` (drops `pending_mail`, moves the thread's runs' revisions), then a running task is cancelled (`cancelled`, no report, its turn saved `aborted`) and the runs' views go out again. `already_closed` with its reason when it was. |
| `agent_message` | Extended (§5): idle → `queued`, closed → `closed`. |
| `agent_read(run_id \| agent_id)` | `agent_id`: the thread's latest ended run's result (`agent_id`, `task_number` in the reply). |
| `sessions_list(list)` | Adds `Subagents (N):` — per thread `'<title>' [agent_id <8>] — open, working \| open, idle \| closed (<reason>), N tasks; latest result: …`; task lines say `task N`. `cancel` stops the task only. |

Descriptions state what happens (background, report together, same limits, history kept, closed
takes nothing); nothing in them steers toward or away from a follow-up or a new subagent
(`test_the_new_tools_state_facts_and_never_steer`). The system prompt's subagent block adds one
fact: a subagent keeps its conversation after it reports, `agent_followup` gives it another task,
`agent_close` ends it. `agent_followup` is in the plan guard's side-effect tools and the cron and
profile-task disabled tools, like `spawn`.

**Origin.** `owner_request` goes through the same owner-quote check as `spawn`
(`spawn_origin`): it labels the run `owner` or `agent`, never refuses. The thread keeps its first
run's origin.

**Model.** The owner's named model, else the turn's model (as `spawn`), so a provider switch
moves later turns along; the thread's `model` follows its latest turn.

**Turn numbers** are decided in `register`, under the registry lock (`len(thread.runs)`), so two
processes sharing a home never give one thread the same turn twice.

**Limits.** `_admission_error` is shared by spawn and follow-up: shutdown, 8 running in the process,
3 running per conversation. A follow-up opens no thread, so the 16-open cap does not apply.

**Batches and reports.** A follow-up run carries the parent turn's `batch_id`, so spawns and
follow-ups started in one turn report in one notice (§6); the header names `agent_id` and
`task N`. A task given to a running turn is covered by that turn's report.

**Late tasks and messages.** A task or message that reaches a turn too late to be read (the turn's
last step, its summary call, or after its answer) is not lost. Once the run's end is recorded and
**before its checkpoint is dropped** (its `mail` lines were the only durable record),
`_stash_unread` moves it to the thread's `pending_mail` (a task as `{kind: "task", batch,
origin}`); a run suspended by a shutdown keeps it in its checkpoint instead. A task is kept only
when the turn ended on its own (`ok`, `error`, `timeout`): a turn stopped on request, by the
owner stopping the conversation (`cancel_children` → `interrupted`) or by a shutdown had its work
stopped too, so its unread tasks are dropped (messages are still kept). Kept tasks start as the
thread's next turn (`_start_pending_tasks`: `take_pending_tasks` under the lock, the first starts
a follow-up with its batch and origin, the rest join it), scheduled when any task of the
conversation ends and at startup (`resume_pending`, gateway only); one that cannot start (limits,
shutdown) is kept again (`requeue_tasks`). A follow-up's `register` takes only the waiting
*messages* as its notes. Nothing is kept for a closed thread.

**The turn's input** (`_child_run`, follow-up): `SubagentThreadStore.turn_context(threadId)` →
the saved `systemPrompt` (`meta.subagent`; rebuilt with `_build_subagent_prompt` when the first
turn ended without one) and `thread_history(session.messages)`: the protocol fields of the
canonical rows (a kept summary as its `system` row; `context_boundary` and client-only rows left
out), a stopped turn's empty closing row as `[This task was stopped before it finished.]`, then
`repair_thread_history`. The first message is `followup_message(task, notes)`: each waiting message
as `[Message from the main agent]\n…`, then `[Task from the main agent]\n<task>`. `ChildState(messages
= [system, *history, first], base_count, base_digest)`; the checkpoint header records both.

**History repair** (`repair_thread_history`). A stopped or crashed turn can leave a reply whose
calls have no result mid-history (its closing row and later turns follow); `_repair_tool_sequence`
only repairs the tail. Every reply's calls are answered right after it in call order — the recorded
result if there is one, else `UNANSWERED_CALL_TEXT` (*"No result: the turn that made this call
ended before its result was recorded. It may or may not have run; check before relying on it."*);
a result answering no call of the reply before it is dropped; an empty reply without calls says
`[No reply.]`. New turns do not leave such holes: `_save_turn` answers a stopped turn's open calls
before saving (`open_call_ids`; `interrupted` with `STOPPED_CALL_TEXT` for a call that started —
`ChildState.started_calls`, rebuilt from `tool_start` — else `not_run` with
`NOT_STARTED_CALL_TEXT`). Pinned by a strict provider-ordering check in the tests.

**Context of a long thread.** Every request of a follow-up is fitted like any child request
(§4.1) with the turn's task pinned in place (`pin_at`): the thread's earlier turns are
compactable; a summary's list is `[system, summary, kept history…, task, kept work…]`. When a turn
summarized (`ChildState.summarized`, set by `apply_compacted(summarized=True)`, replayed from
`compacted.summarized`), `_save_turn` keeps it after saving the turn:
`SubagentThreadStore.persist_context` rewrites the working context `<key>.jsonl` to
`[summary, *kept]` — `kept` = the messages after the summary at the turn's end plus its closing
reply; each is matched to the saved row it is (`_match_saved_rows`: from the end, like the
archive's matching, but a message the projection made up — a repaired call, a stopped turn's
closing text — consumes no row, so the rows before it still match) — with the owner's compaction machinery (`prepare_compaction_archive` marks the replaced
rows compacted in the append-only display log, `append_compaction_summary` archives the summary and
any kept message that is not a saved row as hidden derived copies, `mark_full_synced`, the pending
transaction / boundary markers, then `finalize_compaction_archive` / `finalize_context_boundary`
in a second save). A kept message that is a saved row keeps that row (uncut, with its fields).
`subagents.history` still shows every row, with a `context_boundary` seam. Idempotent per run
(`meta.contextRun`), and only after the turn itself is saved (`meta.runs`), so a crash between the
two is finished by the replay of `final`. The next turn starts from the summary — no summary call
for what was already summarized.

**Streaming (§16a).** A follow-up turn streams as any turn: `run_started` with `trigger:
"followup"`, its `turn`, the task as `prompt`, `withConversation: false`; its `iterationIdx` count
from 0 in its own journal; `chat.inflight {K}` holds it while it runs.

**History rows.** A follow-up's task row has `turn`, `trigger: "followup"` and `brief` (the bare
task; the content is the labeled first message); a task given to a running turn is an
`agent_note` row with `noteKind: "task"`. `thread_view` adds `pendingMessages`.

**Deviations from the design.**
* `agent_message` to an idle subagent queues in `threads.json` (`pending_mail`, ≤ 32) as designed;
  a message or task that arrives too late for a running turn is kept durably for the next turn
  (not in the design); a task given to a running turn has its own label.
* No `subagents.close` RPC (owner decision, §16); `owner` stays a valid reason with no path.
* The 16-open cap keeps closing the idle-longest thread (owner decision); it is not a refusal.
* A restart that cannot continue a turn saves a minimal turn for it (`_save_settled_turn`), so
  the thread's history has every task it was given.
* Accepted: a crash between `persist_context`'s two saves leaves its markers, which the session's
  next load finishes (as for the owner's conversations).

## 17. Open items

* Restarts, phase 4: an `ask` / `off` setting, a `subagents.resume` RPC, and continuing a run
  without a checkpoint from its transcript; Desktop's "resuming…" pill, the `resumed` divider and
  an offline state.
* The main agent's own turn is not continued after a restart (a report turn that crashed is
  processed again, so its own side effects may repeat — design report §4.2 D2). A child's message
  to the main agent that was not yet handled is lost in a crash.
* Relay-connected clients and subagent events: see the round-3 research
  (`output/research/agents-v2-round3-2026-10-15/`).
