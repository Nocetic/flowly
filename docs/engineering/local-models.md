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

**Provider kinds.** `provider.list` gives `ollama` and `vllm` the kind `local`
(it was `url` for `vllm`, and nothing for `ollama`, which an app read as "needs
an API key"). An app that does not know `local` shows the row only once it is
in use.

## New: finding the servers (`flowly/integrations/local_models.py`)

`provider.local` → `{servers, minContext, recommendedContext}`. Each server:
`{id, name, slot, base, installed, running, models[]}`; each model:
`{id, tools, maxContext, context, contextOk, loaded, size, parameters, cloud}`.

- Only the two fixed loopback addresses are probed (Ollama `:11434`, LM Studio
  `:1234`), with a 0.5 s connect timeout. A machine without them (a cloud
  agent) answers two servers that are off, at once.
- `installed`: the app is on the computer even if it is off (`ollama` on PATH
  or `Ollama.app`; `~/.lmstudio` or `LM Studio.app`). The app then says how to
  turn it on instead of saying nothing.
- Ollama: `/api/tags`, `/api/ps`, `/api/show` (four at a time, at most 60
  models). Embedding-only models are left out (`capabilities` without
  `completion`). `tools` from `capabilities`.
- LM Studio: `/api/v0/models` (0.3.6+), else the OpenAI `/v1/models` list.
  Embedding models are left out; `tools` from `capabilities: ["tool_use"]`
  when it says.
- Something else answering on the port is an `error`, not a server.

### Context, the trap

A local server runs a model with its own context length: Ollama's default is
4096 tokens below 24 GiB of VRAM, LM Studio's for a model loaded on demand is
4096 too. The agent's instructions and tool definitions alone are longer.
The OpenAI-compatible API has no field for it (Ollama's documentation says so),
so the server silently cuts the start of the prompt, and a local model looks
stupid.

So `context` is what the server will actually use, never the trained window:

- Ollama: the loaded model's `context_length` from `/api/ps`; else the
  `num_ctx` the model was built with (`/api/show` `parameters`); else the last
  one seen loaded (kept in memory, since Ollama unloads a model after its
  keep-alive and loads it again the same way); else unknown.
- LM Studio: `loaded_context_length` of a loaded model; else unknown.
- `contextOk` = `context >= MIN_CONTEXT` (32 768: compaction alone keeps 20 000
  free); null when unknown. `RECOMMENDED_CONTEXT` is 65 536, what Ollama's
  documentation asks of agents.

The model catalogue uses the same numbers: `_fetch_ollama` (now through this
module; an Ollama reached only through its OpenAI route, behind a proxy, still
lists its models from `/v1/models`) and `_fetch_vllm` (enriched from LM
Studio's native list when it answers) put the running context in
`context_window`. Compaction reads the
catalogue first (`CompactionService.model_context_window_for`), so it keeps the
conversation inside what the server really runs, not the model's trained
window.

Nothing changes a server's settings. Raising the context is the owner's
choice (memory use grows with it); the app says where to do it.

## New: using one (`provider.use_local {server, model}`)

One call does what used to take three (`provider.set_base`, `provider.set`,
`model.set`) and two reloads:

1. The model must be one the server holds now (`NOT_FOUND`; `UNREACHABLE`
   when the server is off; `INVALID` for anything but `ollama` / `lmstudio`).
2. Ollama loads it (`POST /api/generate` with no prompt). A model too big for
   the computer fails here with Ollama's own reason (`LOAD_FAILED`), not in the
   middle of a conversation, and the first message does not wait for the load.
   The loaded context comes back. LM Studio loads on the first request.
3. Then, and only then, the config: the slot's address, `providers.active`,
   `agents.defaults.model`; reasoning effort and service tier go back to their
   defaults (a cloud model's mean nothing here). One reload.

A failure in steps 1–2 changes nothing (tested by comparing the config file
byte for byte).

## Tests

- `tests/test_local_models.py` (22): the scan against the servers' documented
  responses (embedding models out, tools, loaded context against built
  `num_ctx` against unknown, not running, installed but off, something else on
  the port, LM Studio native and older), loading (context remembered after
  unload, a model too big), the RPCs (one-step switch for both servers, effort
  and tier reset, refusals, nothing written on failure, kinds, dispatch), and
  the running context reaching the catalogue that compaction reads.
- `tests/test_local_provider.py`, `tests/test_ollama_provider.py`: carried
  over, plus the cloud timeout test.
- `tests/test_provider_switching.py`: the `vllm` kind is `local` now.
- Full suite: 10 395 passed; the only failures are the 12 screen tests
  (`test_screen_desktop_still.py`, `test_screen_capture_service.py`) that fail
  on `main` the same way.
- Not run against a real Ollama or LM Studio: neither had a chat model on the
  machine this was written on (LM Studio held only an embedding model).

## Not done

- Downloading a model from the app (`ollama pull` / `lms get`): gigabytes and
  minutes; it belongs in the model menu with progress, later.
- Named agents: `provider.use_local` is not in the profile host's contract, so
  only the main agent runs on a local model from the app.
- Ollama's server default context cannot be read before a model is loaded;
  until then `context` is unknown and the app says so.
