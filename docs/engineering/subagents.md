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
| `flowly/agent/subagent_registry.py` | `SubagentRunRecord` + `SubagentRegistry` (`runs.json` index, result files, transcripts, file lock, pruning), `add_usage` |
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

`SubagentManager._run_subagent` → `_do_run`:

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
  delivered and are not handed over again.

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

## 7. Stopping

* `subagents.cancel` RPC (owner, from a client) and `sessions_list(action="cancel",
  run_id=<id>|"all")` (agent). `cancel` sets the outcome `cancelled`, cancels the task and awaits
  it.
* A task cancelled before its first step has no handler of its own; the spawn task's done
  callback (`_cleanup`) finishes the record, records its metric and runs `_announce` so siblings
  holding for it are released.
* The owner's chat Stop stops the main agent's turn only, as in Codex: subagents keep running
  (they usually outlive the turn that started them). Each subagent is stopped on its own (its
  sidebar / notch "Stop", or by asking the agent).

## 8. Registry, transcripts, results

* `runs.json` (atomic writes under a `FileLock`) holds `SubagentRunRecord`s. Notable fields:
  `origin` (owner/agent/system), `mark`, `batch_id`, `usage`, `delivery_state`
  (pending → queued → delivered | undelivered | not_required), `announced`, `tool_trace`,
  `result_preview/chars/available`.
* Result bodies live in `runs-results/`; transcripts in `runs-transcripts/` as JSON lines
  `{seq, ts, kind, …}` with kinds `brief`, `assistant` (with `toolCalls`), `tool`,
  `message_in`, `message_out`, `result`. `read_transcript(run_id, after, limit)` pages by `seq`
  with an offset cursor cache. System runs keep no transcript.
* Pruning: user-facing runs after 30 days, system or `cleanup=delete` runs after 1 day — never a
  run whose result is still undelivered.

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
(`delivery_state` delivered), with a 600 s grace for a result that was published but never
handled (a crash between the two).

## 11. Restart

`resume_pending` on startup: runs still marked running are finished as `error`
(`process_restarted`), their metric recorded and announced; results that ended within the last
hour but were never handled are announced again (their `announced` flag reset so their batch can
claim them); older ones become `undelivered`. Running work is not resumed — see the open item
in §15.

## 12. Events and RPCs

* Events (`TaskEvents`, coalesced by run, newest revision wins): `subagent.started`,
  `subagent.progress` (activity, tool steps, tokens), `subagent.completed`. Wire shape is
  `run_view` (schemaVersion 2): ids, label, status, timing, origin, mark, `usage`
  (`inputTokens`, `outputTokens`, `cacheReadTokens`, `totalTokens`), tools used,
  `childSessionKey`, delivery state.
* RPCs: `subagents.list`, `.get`, `.result`, `.transcript {runId, after, limit}` →
  `{task, items, nextSeq, more, done}`, `.cancel {runId}`, `.stats {days, includeSystem}`,
  `.spawn` (manual, origin owner).
* Through the profile host (another local bot): list, get, result, transcript, cancel, stats.
  Spawn and set_model are not exposed there.

## 13. Metrics (`flowly/agent/subagent_metrics.py`)

Every ended run appends one JSON line to `metrics.jsonl` (next to `runs.json`): `runId`, times,
`durationS`, `origin`, `kind`, `model`, `outcome`, `errorCode`, `toolCalls`, `tokens`,
`channel`. Never the brief, result or conversation. The file keeps its newest half past 8 MB, so
metrics outlive the 30-day run pruning. `subagents.stats` returns, for the last `days`:
`runs`, `byOrigin`, `outcomes`, `durationS {average, p50, p90}`, `tokens`, `toolCalls`, and
`byDay` (UTC). System runs are left out unless asked; a run recorded twice counts once.

## 14. Tests and evaluation

* Unit / integration: `test_delegation_rule.py`, `test_subagent_spawn_contract.py`,
  `test_subagent_reporting.py` (batches, report turns, waits, plan guard, stop all, search SDK),
  `test_subagent_accounting.py` (limits, tokens, slow sibling, stopped sibling, metrics, stats),
  `test_repetition_guard.py`, `test_subagent_hardening.py`, `test_subagent_conversation_key.py`,
  `test_subagent_transcript.py`, `test_subagent_messaging.py`, `test_subagent_safety.py`,
  `test_subagent_profile_transport.py`.
* Live model: `tests/test_agent_behavior_real.py` (`-m real_llm`, `FLOWLY_EVAL_CONFIG`,
  `FLOWLY_EVAL_ONLY=a,b`). Checks what the agent *did*: explanations answered without tools,
  "don't search" respected, a detailed single task done itself, explicit delegation starts
  exactly one, "one agent each" starts one per part, its own part is still done, and after
  starting subagents the agent ends its turn instead of waiting or polling.

## 15. Open items

* Work running when the gateway restarts is lost (finished as `process_restarted`). Resuming it
  is the last task of this round and gets its own design.
* Relay-connected clients and subagent events: see the round-3 research
  (`output/research/agents-v2-round3-2026-10-15/`).
