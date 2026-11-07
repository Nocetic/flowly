# Local models: Ollama, LM Studio and any OpenAI-compatible server (2026-11-06)

Status: branch `feat/local-models`, worktree `.claude/worktrees/local-models`.
Not merged, not pushed. Hakan asked for it: the app should list the models a
person already has in Ollama or LM Studio, load one, and run the agent on it.

## Where it came from

The provider half was written in July 2026 on `worktree-local-providers`
(9 commits, 8–10 July; a backup is `backup-local-providers-pre-sync`). It was
never merged and was 920 commits behind `main`. It was carried over as one
three-way apply of its net diff onto `main` (`d55bbd22`); the old branches are
kept as they were.

What it brought, unchanged unless noted below:

- `ollama` provider slot: explicit only, default address
  `http://127.0.0.1:11434/v1`, no key (a dummy bearer for the OpenAI SDK).
- The `vllm` slot becomes "Local OpenAI-compatible" (LM Studio, vLLM,
  llama.cpp server, text-generation-webui …): explicit only, the address is
  required, `local` is accepted as its name (`/provider local`).
- Neither joins the credential cascade: a saved local address says nothing
  about whether the server is running, and must never win over a real key.
- `is_local_endpoint`: loopback, private ranges, Tailscale (100.64.0.0/10),
  `.local`, dot-less LAN names.
- Local endpoints get a 600 s request timeout instead of 120 s.
- Switching to a local provider picks a model the server serves when the
  configured one is not (`model_catalog.autofill_model_for_local`, in the
  gateway's provider reload).
- `provider.set_base` RPC, setup cards and probes, `flowly setup ollama`,
  `flowly setup local`, the CLI first-run picker, the TUI.

Conflicts on `main`: `feature_rpc.py` (both kept; `provider_set_base` now uses
`_live_provider_reload_succeeded`), `active_provider.py` (both kept),
`doctor.py` (main's rewrite kept; it has no key check any more),
`onboard_cmd.py` (main's), and four docs pages (merged by hand).

## Changed while carrying it over

**The timeout no longer touches cloud providers.** The old branch read the
stock `llmTimeoutSeconds` (120) as "unset" for every provider, so a cloud
provider fell back to its own default: the ChatGPT subscription went from
120 s to 180 s. Now only a local endpoint on the stock value gets 600 s; every
cloud provider gets exactly what it got before.
`test_build_provider_cloud_stock_config_keeps_the_configured_timeout` fails on
the old logic (180 ≠ 120).

**Nothing changes for what exists** (checked before merging, 2026-11-06):

- `provider.list` keeps the kind `url` for `ollama` and `vllm`. The iOS,
  Android and older Desktop lists show a `url` row only once it is set up or
  in use, but list an unknown kind as one more provider; a new kind would
  have put two greyed rows under every phone's list. A fresh config reports
  both as not set up. (`test_local_slots_stay_hidden_in_older_apps`.)
- A self-hosted endpoint with an API key stays the cascade's last resort, in
  `resolve_active_provider` and the legacy `Config` helpers, as on main. The
  old branch had taken it out, which would have left a config that relied on
  it with no provider. Without a key it is explicit-only; Ollama always is.
  The one difference: a key with no address no longer resolves (it sent the
  key to OpenAI's own address before).
- The longer timeout is for the local slots only. A cloud provider pointed
  at a proxy on this machine (the Flowly server in development, LiteLLM) keeps
  its timeout. (`test_a_cloud_provider_behind_a_local_proxy_keeps_its_timeout`.)
- The TUI opens the all-fields setup for a provider without a key field only
  for the local slots; picking the Flowly account card while signed out still
  does what it did.
- Provider cards are not among the connections the apps list
  (`CONNECTION_CATEGORIES`), so the new cards show only in the CLI and TUI.
- Visible on purpose: the CLI first-run picker and `/provider` offer Ollama
  and "Local OpenAI-compatible"; `flowly status` prints an Ollama line; the
  self-hosted row is named "Local OpenAI-compatible" (shown only once set up).

## New: what the agent needs (`flowly/compaction/window_need.py`)

First tried on a real Mac (2026-11-06) with LM Studio and gemma-4-e2b held at
8 192 tokens: every message, even "selam", was refused before dispatch with
"This request is too large. Compact the conversation…". The gateway log said
why: `Fixed request overhead (68013 tokens) leaves no room in a 8192-token
window`. The agent's system instructions and tool schemas alone are 55-68K
tokens. The first version of this branch judged models against a fixed 32K,
so it would have let that model, and any 32-64K one, through.

The need is now measured, not guessed:

- `ProviderRequestCoordinator.fit` records every request's fixed overhead
  (`fixed_prefix_tokens + tool_schema_tokens`).
- The largest of the last 32 decides. It is kept on disk
  (`<home>/state/window-need.json`, written only when it changes), so a
  restarted gateway knows it before its first request.
- Need = overhead + the reply reserve (`compaction.reserveTokensFloor`, 20 000)
  + 8 192 of conversation, rounded up to 1K, at least 32K. For 68 013 that is
  96 256.
- An agent that never made a request has nothing measured; the need is then
  65 536 (the floor other agents use for tool use), and `provider.local`
  says `requiredMeasured: false`.

The refusal is now honest about it. When the fixed overhead alone leaves no
room, `unfit_response` (request_guard) adds the window, the overhead and the
need (`LLMErrorInfo.context_window`, `fixed_overhead`, `required_context`,
`code: "context_window_too_small"`). The presentation becomes
`MODEL_CONTEXT_WINDOW_TOO_SMALL`: "The model runs with 8,192 tokens of
context, and your agent's instructions and tools alone take about 68,013.
Load the model with at least 96,256…". The classification stays
`INPUT_TOO_LARGE`, so retry and recovery rules do not change; a long
conversation still reads as before.

## New: finding the servers (`flowly/integrations/local_models.py`)

`provider.local` → `{servers, requiredContext, requiredMeasured,
recommendedContext, minContext}` (`minContext` = `requiredContext`, the name
apps read before). Each server: `{id, name, slot, base, installed, running,
models[]}`; each model: `{id, tools, maxContext, maxContextOk, context,
contextOk, loaded, size, parameters, cloud}`, judged against the need.

- Only the two fixed loopback addresses are probed (Ollama `:11434`, LM Studio
  `:1234`), with a 0.5 s connect timeout. A machine without them (a cloud
  agent) answers two servers that are off, at once.
- `installed`: the app is on the computer even if it is off (`ollama` on PATH
  or `Ollama.app`; `~/.lmstudio` or `LM Studio.app`).
- Ollama: `/api/tags`, `/api/ps`, `/api/show` (four at a time, at most 60
  models). Embedding-only models are left out; `tools` from `capabilities`.
- LM Studio: its REST API `/api/v1/models` (key, type, `loaded_instances[]
  .config.context_length`, `max_context_length`,
  `capabilities.trained_for_tool_use`); else `/api/v0/models`; else the
  OpenAI `/v1/models` list. Embedding and decision models are left out.
- Something else answering on the port is an `error`, not a server.

`context` is what the server will actually use, never the trained window:
Ollama's loaded `context_length` (`/api/ps`), else a `num_ctx` the model was
built with, else the last one seen loaded; LM Studio's loaded instance. The
model catalogue (`_fetch_ollama`, `_fetch_vllm`) carries the same figure in
`context_window`, so compaction sizes the conversation to it.

### Who sets the context

- **LM Studio: Flowly does.** `POST /api/v1/models/load {model,
  context_length, echo_load_config}` loads it with the next common size at or
  above the need (98 304 for 96 256), within its trained window. A model held
  with less is unloaded (`/api/v1/models/unload {instance_id}`) and loaded
  again; one held with enough is left alone. The applied size comes back
  from `load_config` (or a re-read).
- **Ollama: the owner does.** Its OpenAI route builds options only from the
  standard fields (`ollama/openai/openai.go`: stop, num_predict, temperature,
  seed, penalties, top_p); `num_ctx` sent there is ignored. Its context is the
  app's Context length setting or `OLLAMA_CONTEXT_LENGTH`. Flowly loads the
  model and reads back what Ollama gave it, and says what to set
  (`recommendedContext`). Building a derived model with `num_ctx`
  (`/api/create`) would work but writes models into the owner's Ollama; not
  done.

## New: the window kept current between turns (`flowly/integrations/local_window.py`)

Also seen on the Mac: after the owner reloaded the model in LM Studio at
128K, every message was still refused as too large until the gateway
restarted. The window was read once, when the provider was switched, from a
catalogue cache that never expires. The other way round, LM Studio unloads an
idle model and loads it again on the next request with its own small
default, behind the agent's back.

Now, before every main-agent request on a local slot (`ollama`, `vllm` on a
local address), at most every 30 s:

- the server is asked (`local_models.runtime_window`): LM Studio is made to
  hold the model with the context the agent needs (loaded if it is not,
  reloaded if it holds it with less, left alone otherwise); Ollama is read;
  a `vllm` address that is not LM Studio (vLLM, llama.cpp) is left alone;
- the answer goes to `model_catalog.set_runtime_context_window`, which
  outranks the catalogue in `get_context_window`, the figure compaction sizes
  requests from (the picker's list is untouched; a flush clears it).

When a request is refused because the window cannot hold the fixed overhead,
the server is asked at once and the request sized again, once
(`AgentLoop._call_provider_with_context_recovery`). A check that fails is
logged and changes nothing; the refusal still says why. Cloud providers,
the Flowly account (even a development server on localhost) and remote
addresses are never asked.

`tests/test_local_window.py` (14) covers the loads, the 30 s spacing and the
forced check, what is never asked, failures, the runtime window outranking
the catalogue, and the loop: a stale 8K window with the model now at 128K
goes through (it fails without the check: the request is refused), a model
whose window really is 8K is still refused with the numbers.

## New: using one (`provider.use_local {server, model}`)

Nothing is written unless the agent can run on it:

1. `UNREACHABLE` (server off), `NOT_FOUND` (no such model), `INVALID`.
2. `CONTEXT_TOO_SMALL`: the trained window is below the need.
3. The load: `LOAD_FAILED` with the server's own reason (a model too big for
   the computer fails here, not mid-conversation). LM Studio gets the need.
4. `CONTEXT_NOT_APPLIED`: the server then runs it with less than the need
   (Ollama's setting; LM Studio short of memory). The message says how much
   is needed and what to set.
5. Then the slot's address, `providers.active`, `agents.defaults.model`
   (effort and tier back to defaults), one reload. The result carries
   `context`, `requiredContext`, `contextOk`.

Every failure leaves the config file byte for byte as it was (tested).

Checked live against LM Studio 0.4.28 on this Mac (2026-11-06): the scan read
the v1 list, and `load` loaded google/gemma-4-e2b with 98 304 tokens, read back
from `load_config`; `lms ps` showed the same.

## Tests

- `tests/test_local_window.py` (14): see above.
- `tests/test_window_need.py` (12): the need from the overhead, the cautious
  default, the largest recent figure, ageing out, a restart reading it back,
  writes only on change, a broken file ignored, the load sizes, the
  coordinator recording every request, and the refusal saying the window is
  too small with the numbers (classification unchanged) while a long
  conversation still reads as one.
- `tests/test_local_models.py` (30): the scans against the servers'
  documented responses (LM Studio's v1 entry is the one a real 0.4.28
  returned), judging against the need; LM Studio loading with the need,
  unloading and reloading a model held with 8K (the case hit), leaving one
  held with enough alone, refusing a too-small window before touching
  anything, passing its refusal on, an LM Studio without v1; Ollama loading
  and reading its context; every `provider.use_local` refusal leaving the
  config untouched; the catalogue carrying the running context to
  compaction for both servers; old apps not seeing the slots.
- `tests/test_local_provider.py`, `tests/test_ollama_provider.py`,
  `tests/test_provider_switching.py`: carried over, plus the compatibility
  tests above.
- Full suite on the branch merged with main: 10 404 passed; the only failures
  are the 12 screen tests that fail on main the same way.

## Not done

- Downloading a model from the app (`ollama pull` / `lms get`): gigabytes and
  minutes; it belongs in the model menu with progress, later.
- Named agents: `provider.use_local` is not in the profile host's contract, so
  only the main agent runs on a local model from the app.
- Ollama's context cannot be set from Flowly (see above); the owner sets it.
- Shrinking the need itself: the open agent defers cold built-in tools behind
  its tool-search bridge too; Flowly defers only MCP and extensions, which is
  why the overhead is 55-68K. A design decision for all agents, cloud too.

## The picker follows the server (2026-11-07)

Hakan deleted Gemma in LM Studio and downloaded Bonsai, but Desktop's model
list still showed Gemma. The gateway kept each provider's catalogue from its
first read (`model_catalog._CACHE`) until credentials changed. For a cloud
catalogue that is fine. A local server's models change whenever the owner
downloads or deletes one.

Ollama and the `vllm` slot (LM Studio, the owner's own vLLM) are now read
again when their list is older than `LOCAL_CATALOGUE_TTL_SECONDS` (10 s). A
list opened twice in a row is not scanned twice. Cloud catalogues are still
read once. Desktop asks `model.list` each time the list opens, so it shows
the server's current models. Tests: the two catalogue tests at the end of
`tests/test_local_models.py` (both fail without it).
