# Thinking on, off and its level, on every provider (2026-11-06)

Branch `feat/thinking-controls` (worktree `.claude/worktrees/thinking-controls`,
based on `feat/local-models`). Not merged.

## What the owner asked

Desktop, iOS and Android had a Thinking switch and a level slider only for the
Flowly account, OpenRouter and the ChatGPT subscription. Hakan asked why Grok
and the other providers had none, and to fix it for every provider.

The apps needed no change. They draw the switch when a model's `efforts`
include `none` and another level, and the slider from the other levels
(Desktop `lib/effort.ts` `hasThinkingSwitch`, `sliderEfforts`). A model whose
only levels are `none` and one more gets the switch alone. So the work is
Core's: say which levels each model takes, and send them the way each API
wants.

The earlier rules still hold (`model-settings-2026-11-02.md`): a level a model
lacks is lowered to the nearest one it takes, never raised; `none` on a model
that must think becomes its weakest level; the saved level is kept across
model changes.

## xAI Grok (`a3c92c1c`)

From each model's page on docs.x.ai (`effort.py` `_XAI_FAMILIES`):

| Model | Levels | Default |
|---|---|---|
| Grok 4.6, 4.7 | low, medium, high, xhigh | high |
| Grok 4.5 | low, medium, high | high |
| Grok 4.3 | none, low, medium, high, xhigh | low |
| Grok 4.20 reasoning | no level; off is its `-non-reasoning` twin | |
| Grok 4.20 multi-agent, Grok Build | none (multi-agent's effort is its agent count) | |

- Off on 4.3 sends `none`. Off on 4.20 reasoning sends the request to its
  non-reasoning twin, but only when the account's catalogue lists the twin
  (`xai_request`, `model_catalog._xai_models_from_payload`). That row then
  shows `["none", "high"]`, the switch alone.
- Both ways in: `reasoning_effort` with an API key
  (`openrouter_provider._xai_route`), `reasoning.effort` with the sign-in
  (`xai_responses_provider._route`).

## OpenAI key, Gemini, Groq, Z.AI GLM

Before, these got no thinking field at all. OpenAI's reasoning models were
also sent `max_tokens` and `temperature`, which they refuse with a 400.

**Where the levels come from.** models.dev's `reasoning_options` per model,
which matched the providers' own docs where checked
(`effort.models_dev_efforts`):

- `{"type": "effort", "values": [...]}` lists the levels. Groq's `default`
  value means "the model's own" and is not a level.
- `{"type": "toggle"}` means thinking can be switched off (`none`).
- `{"type": "budget_tokens", "min": 0}` means it can be off too. Gemini's
  OpenAI-compatible API maps `low` / `medium` / `high` onto a budget, so a
  Gemini model that only lists a budget (2.5 Pro) gets those three.
- A model that can only be switched (GLM 4.x, 5, 5.1; Gemma 4; Groq Qwen 3.6)
  gets `["none", "high"]` with `thinking_switch_only`: `high` there means "on"
  and sends no level.
- Defaults: Gemini `high`, GLM `max` (their docs), otherwise `medium`, or the
  nearest level the model takes.

`models_dev.THINKING_CATALOGUES` maps the providers: `openai`, `gemini` →
`google`, `groq`, `zhipu` → `zai`, `zai_coding` → `zai-coding-plan`. The Coding
Plan keeps its own short model list and is filled in from that catalogue by id
(`apply_thinking_controls`). Anthropic is left out on purpose: its levels
follow its model families (`anthropic_efforts`).

**What a request carries** (`effort.thinking_body`, sent from
`openrouter_provider._reasoning_body`):

| Provider | Off | On, switch-only model | A level |
|---|---|---|---|
| OpenAI, Gemini, Groq | `reasoning_effort: "none"` | nothing | `reasoning_effort: <level>` |
| GLM (`zhipu`, `zai_coding`) | `thinking: {type: disabled}` | `thinking: {type: enabled}` | `thinking: {type: enabled}` + `reasoning_effort`; a model that cannot stop thinking gets the level alone |

Nothing is sent when no level is set, for background work
(`reasoning=False`), or for a model the catalogue does not list (offline, or
typed by hand). The model then thinks the way it does by default.

**Sampling fields** (`_sampling_body`):

- OpenAI gets `max_completion_tokens` instead of `max_tokens`. Every OpenAI
  chat model takes it, and the reasoning models refuse `max_tokens`.
- `temperature` is left out when models.dev marks the model `temperature:
  false`, and on an OpenAI reasoning model unless it is sent
  `reasoning_effort: "none"` (OpenAI takes sampling only with thinking off).

**Refusals.** A 400 that names `temperature`, or the thinking fields
(`effort`, `reasoning`, `thinking`), is retried once without that field, and
the model is remembered (`_TEMPERATURE_REFUSED_MODELS`,
`_EFFORT_REFUSED_MODELS`), so later requests leave it out. Any other 400 is
not retried. Both ways (`chat`, `chat_stream`) do this (`_relax_refused`).

Tests: `tests/test_thinking_controls.py`. The models.dev rows in it are copied
from the live catalogue. With the catalogue change alone (no provider change),
17 of its request tests fail.

## Not verified live

No OpenAI, Gemini, Groq or Z.AI key was used: the request shapes follow the
providers' docs and models.dev, and a field a model refuses is dropped
automatically.
