# A leaner fixed prompt, and a warm local cache (2026-11-06)

Branch `feat/lean-prompt` (worktree `.claude/worktrees/lean-prompt`), on top of
`feat/local-models`. Not merged.

## Why

Hakan ran Gemma 4 on LM Studio and the first answer took one to two minutes,
thinking on or off. LM Studio's log showed why: the request carried ~59K
tokens (Gemma's tokenizer) with an empty cache. It read them at 1050 tokens/s
falling to 590 as the context grew. Writing the answer took seconds
(~83 tokens/s). Every request repeats the same fixed part (instructions and
tool schemas), so its size is the latency floor for a local model and a cost
on every cloud request.

Hakan asked for it to be fixed for every provider, "enterprise-grade", with no
loss of quality, the way the open-source reference agent does it.

## What the fixed part really is

Measured with the real path (`ContextBuilder.build_messages`, as the loop
calls it) and the real tool schemas (an `AgentLoop` built from the owner's
config in a temporary home). Counts are cl100k tokens; Gemma's tokenizer
counts more.

| Message | System | Guide + skills | Tools | Total |
|---|---|---|---|---|
| "selam" before | 14.4K | 2.0K | 22.9K | 39.3K |
| "selam" after | 14.4K | 2.0K | 19.5K | 35.9K |
| "tarayıcıda gmail'i aç" before | 14.4K | 15.1K | 22.9K | 52.4K |
| "tarayıcıda gmail'i aç" after | 14.4K | 7.3K | 19.5K | 41.2K |

Two corrections to the first analysis:

- The skills catalog (9.5K as XML) and the computer and browser playbooks
  (11.7K) are not sent on every request. They were already routed per turn
  into a second system message (`capability_guidance.py`). The first
  measurement used `build_system_prompt` alone, which renders the old eager
  prompt.
- LM Studio's request log truncates tool descriptions (`<Truncated in logs>`).
  The real tool schemas are 22.9K, not the 15.6K the log suggests.

Not changed: memory (6.6K, the owner's own facts) and the long descriptions of
the hot tools. `browser_tab` alone is 4.3K, but it is used in 48 sessions with
384 calls. Shortening those is a product decision.

## 1. Turkish case endings (`ac5bf899`)

`route_capability_guidance` matched bare nouns (`\btarayıcı\b`), so
"tarayıcı**da**" or "ekran**da**" missed. Those turns fell to the
ambiguous-action rule, which sends both playbooks (~15K). UI nouns now take
any ending. "masaüstü" stays exact, because "masaüstüne / masaüstündeki" means
the Desktop folder (file work). Tests: `tests/test_capability_guidance.py`
(6 of the new cases fail without the fix).

## 1b. The turn clock sent the computer playbook with every turn

Every turn's user message starts with the turn clock:
`<turn_time>… on the agent's computer …</turn_time>`. The guidance router
read "computer" there as computer intent. So every real turn, "selam"
included, got the computer playbook. The second system message went from
8,480 to 40,061 characters, about 8K tokens more, on every provider. The
measurements above call `build_messages` without the clock, so they never
showed it. It was found by comparing a warm-up request with a real turn
byte for byte.

Fix: the router ignores `<turn_time>` blocks, also in history
(`capability_guidance._owner_text`). The loop also routes playbooks and
relevant skills on the owner's own words (`build_messages(intent_text=…)`),
never on the additions Flowly makes to a turn's message: the clock, notes,
group context, coverage. Tests: `test_capability_guidance.py`,
`test_lean_prompt.py` (both fail without the fix).

## 2. Rarely used built-in tools wait behind the bridge

Core already had the bridge (`tool_search` / `tool_call`, intent promotion, a
budgeted catalog with compact signatures), but only for MCP and plugin tools.
The reference agent also defers cold built-ins; Flowly now does too.

**Which tools** (`discovery.DEFAULT_DEFERRED_TOOLS`, 17). They were picked
from real use: 1267 sessions, 2332 user turns and 3973 tool calls on the
owner's Mac, 2026-11-06. Each ran in at most 5 sessions:

- `computer`, `screenshot`
- `linear`, `docker`
- `obsidian_ingest`, `obsidian_write`, `obsidian_append`, `obsidian_read`,
  `obsidian_search`, `obsidian_list`
- `template_draft`
- `video_analyze`, `image_analyze`
- `memory_import`, `memory_feedback`, `memory_consolidate`
- `skill_improve`

Left direct on purpose:

- Tools a prompt or flow tells the model to call by name; called while
  waiting, each would cost a step:
  - `session_search` (named in the identity's search tools)
  - `skill_manage` (/learn)
  - `agent_*` (after `spawn`)
  - `follow_up`, `proactivity` (check-ins)
  - `process` (background `exec`)
- `email`, `google_connection`: a mail request ("maillerimi kontrol et",
  "check my email") does not promote the tool.
- `request_secret`: hidden, a model may ask for a key in chat.
- The media generators and board: their own contract in
  `tests/test_tool_routing.py`.
- The browser tools.

The config is `tools.routing.discovery.deferred_tools`. `None` (the default)
means the curated list; a list replaces it; `[]` keeps every built-in direct.
It is `None` rather than the list so a saved config follows revisions.

**Nothing is lost** (`discovery.build_tool_disclosure`, `AgentLoop`):

- **Session pins.** A deferred tool a session promotes or uses stays direct
  for the rest of the session (`_session_pins`, bounded to 512 sessions). The
  tool list only grows within a session, so the provider's prompt cache and a
  local server's KV cache change once per newly needed tool, not every turn.
  Before, an MCP tool promoted by one message was demoted again on the next.
- **Playbook pins.** When the computer or browser playbook is routed into a
  request, its tools are pinned direct for that request (`_playbook_pins`
  reads the guidance message's `_capability_guidance`). The guide and the tool
  always arrive together.
- **Called by name.** A model that calls a waiting tool directly (without its
  schema) is still blocked, the existing safety rule. But the tool is pinned
  and the error says its schema is direct from the next step. A hallucinated
  name the route does not have is blocked as before and not pinned.
- **Promotion by name.** A message naming the tool promotes it in the same
  request ("docker container'larını göster", "linear'da issue aç",
  "Obsidian notlarımda ara").
- **A stable system prompt.** Tool-specific guidance now follows the
  *reachable* tools, not the visible ones (`build_system_prompt`). A waiting
  tool keeps its guide, and promoting it leaves the system prompt
  byte-identical. Only the bridge note follows what is visible.
- **No accidental hiding.** Each kind of deferral must pay for itself
  (`minimum_deferred_schema_tokens`). An unlisted built-in that falls into
  `extensions` (`goal`, `media_find` have no toolset entry) is not hidden just
  because the named built-ins crossed the threshold together.

Tests: `tests/test_lean_prompt.py` (9 of its 13 tests fail without the change; the other four guard behaviour that must not change, such as `goal` staying direct).
The default-route assertion in `tests/test_tool_routing.py` was updated
(deferral is now on); its contract, generators and board direct, is kept.

## 3. Warming a local server's cache

`AgentLoop.schedule_prompt_warm(reason)` sends the agent's fixed prompt to the
local server once, in the background, asking for one token. It runs when the
gateway starts and when the provider or model changes
(`gateway_cmd.on_provider_reload`). After that the first message finds the
prompt cached.

- **Local servers only.** Ollama, or the `vllm` slot (LM Studio) on a local
  endpoint (`is_local_endpoint`); a cloud or self-hosted remote server is
  never warmed.
- **The same prompt as the next turn.** It is built through the same path as
  a fresh turn (`_routed_tool_disclosure` + `build_messages`). It uses the
  route the latest real turn used (channel and the tools it switched off),
  kept in memory and in `warm-route.json` in the loop's state directory (`~/.flowly/warm-route.json` for the gateway). That file
  holds names only, no content, and is written only when the route changes.
  The system prompt depends on no session (the time is in the user message;
  memory freezing is off), so one warm-up serves every new conversation.
- **Waits for MCP discovery** (up to 60 s), so the tool list is final.
- **Sets the window first.** Before warming it runs the same server check a
  turn runs (`LocalWindowSync.before_request`, forced). Tried live on
  2026-11-06: the warm-up skipped it. At 23:08 LM Studio held the model at
  8K and refused the prompt. At 23:10 it warmed at 64K, and at 23:11 the first
  turn's check reloaded the model at 96K, so the cache was lost. Now the
  model is loaded with the needed window (or loaded at all, if unloaded)
  before the warm-up, and the turn finds nothing to change.
- **A real request waits for it.** `_run_llm_tool_loop` waits for a warm-up
  in flight (up to 300 s, then stops it; an owner's stop ends the wait).
  First version cancelled the warm-up instead, expecting the server to reuse
  what it had read. Tried live (19:07, 2026-11-06), LM Studio released the
  cancelled slot ~2.5 s after it had already put the new request in an
  empty slot, so the request read everything again. The warm-up reads the
  very prompt the request needs; waiting costs only what the request would
  read anyway, and then the request finds it cached.
- **A server started after the gateway.** If the server cannot be reached
  (no HTTP answer: LM Studio's server off at 23:04 on 2026-11-06), the
  warm-up tries again after 15, 30, 60, 120 and 240 s, then gives up. A
  server that answers with an HTTP error is not asked again. A request that
  arrives during a pause cancels the warm-up instead of waiting (it reads,
  and caches, the prompt itself); it waits only while the server is reading.
- A failure is logged at info level and changes nothing.

Tests: `tests/test_prompt_warm.py`. The prefix test compares the warm request
with a real turn's first request, both system message and tools. It fails
when the warm-up ignores the turn's route (checked by injecting that bug).

## Not verified live

No live run against LM Studio was made in this branch. To check: start the
gateway from this worktree with the local model and look for `[warm] … has the
agent's prompt cached` in `gateway.log`. Then send a message and read the LM
Studio log: the request should select the warmed slot by LCP similarity and
evaluate only the new tokens.

How much a warm-up saves also depends on the model's chat template. The
second system message (the routed playbook and skills index) varies per
message. A template that renders the tools after all system messages
re-reads the tools from that point.

## 4. Strict chat templates on local servers

Found 2026-11-07 while testing the warm-up with `prism-ml/bonsai-27b` on LM
Studio: its template raises "System message must be at the beginning". Flowly
sends its instructions and the turn's routed guidance as two system messages,
so every request failed, the owner's messages included. Qwen-family templates
do the same, and some templates also need user and assistant turns to
alternate.

Requests to `ollama` and `vllm` (servers that render the model's own template)
are now shaped for that (`openrouter_provider._one_leading_system`):

- The leading system messages are joined into one, in order. The instructions
  stay first, so a server's prompt cache still covers them.
- A later system message (a compaction summary, thread context) becomes a
  `[System note]` user message, joined with a user message beside it.

Cloud providers keep the two system messages: Anthropic's cache relies on the
first one being byte-stable on its own. Tests: `tests/test_strict_templates.py`
(11 of 14 fail without it; the other three check cloud providers are
unchanged), with a stand-in for the template's checks.
