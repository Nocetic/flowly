# The model chosen on each provider, and favourite models (2026-11-07)

Branch `feat/model-memory`. Not merged.

## What the owner asked

Hakan: switching provider does not go back to the model he had chosen on it.
When he leaves a provider and comes back, the model he last chose there should
come back. He also wants to star models. The stars live in Core rather than
on each device, so every app reads the same list.

Choices made with him:

- Favourites are kept per computer: every agent on the Mac shares one list,
  and a cloud computer has its own.
- The picker shows every provider's favourites at the top. Picking one on
  another provider switches provider and model in one step.

## Why it happened

On a switch, Core (`set_active_provider` for the main agent, `profile_host.
_model_for_provider` for a named one) replaced the model with the new
provider's curated default (`DEFAULT_MODELS`) whenever the old model did not
fit it. Nothing recorded which model had been chosen on which provider.

## How the reference agent does it

The open-source reference keeps favourites as `provider::model` keys, in
starring order. A key is kept when its model leaves the catalogue. They sit
in a machine-wide preferences store served by its backend
(`ui-preferences.json` through `/api/ui-preferences`), with a three-way merge
for concurrent windows. It keeps no model per provider: its picker chooses
provider and model together from one grouped list.

## 1. The model chosen on each provider (per agent)

`flowly/integrations/model_memory.py`, stored in each agent's own config as
`agents.defaults.modelsByProvider` (provider key → model id, at most 64).

- **Remembering.** A model chosen with `model.set` (main agent) or
  `profiles.configure {model}` (named agent) is remembered under the provider
  it runs on. On a switch, the model being left is remembered under the old
  provider first, so a model chosen before this change also comes back.
- **Recall.** A switch (`provider.set`, `profiles.configure {provider}`) brings
  back the target provider's remembered model. Three conditions:
  - The provider is set up (same guard as before: a provider without a
    credential would fall through to another one).
  - The provider still fits the model (`model_fits_provider`).
  - For a server whose list was read, the model is in that list: a model
    deleted in LM Studio does not come back.

  Otherwise the old rule applies: keep a model the provider serves, else its
  curated default.
- **Spelling.** The config writer turns dict keys to camelCase on disk
  (`openaiCodex`) and the loader turns them back. The raw-dict paths compare
  provider keys either way (`_same_provider`).
- **`model.set {provider, model}`.** Switches provider and model in one call,
  one reload (a favourite on another provider). The result carries `provider`
  only then, so the existing result shape is unchanged.
- A behaviour change, on purpose: coming back to a provider brings its
  remembered model even when the current one is served there too
  (`tests/test_profile_host.py`, updated).

## 2. Favourite models (per computer)

`flowly/integrations/model_favorites.py`. The file is
`state/model-favorites.json` in the main agent's home (`signin_home()`, as the
computer's sign-ins), so named agents share it.

- An entry is `{provider, model, addedAt}`, in starring order, at most 200.
  The same model on two providers is two favourites. An entry stays when its
  provider is signed out or the catalogue drops the model.
- Writes take a computer-wide `fcntl` lock and replace the file atomically. A
  damaged file reads as no favourites and is rewritten on the next star.
- RPCs:

  | RPC | Does |
  |---|---|
  | `model.favorites` | Returns the list. Each entry has `name` (from a cached catalogue, else the id), `providerName`, and `available` (this agent can use the provider now). |
  | `model.favorite {provider, model, on}` | Stars or unstars a model, idempotent (`on` true or false, never a toggle). Errors: `INVALID`, `FAVORITES_FULL`. |
  | `model.list` | Each row now carries `favorite`. |

  Both new RPCs are also allowed through to named agents
  (`profile_host_contract.py`).

## Tests

`tests/test_model_memory.py`: main and named agents, the camelCase round trip,
a deleted local model, favourites' order, idempotence, sharing, concurrent
writers, the ceiling, a damaged file, and the `favorite` flag. On `main`, the
come-back scenario fails (`grok-4.3`, the default, instead of the chosen
`grok-4.7`).

## Apps

All three read the same RPCs. With an older Core, none of them shows stars.

- **Desktop** (`1d85c58`): a star beside each model; a Favorites section
  heads the model window, from every provider.
- **iOS** (`3539b58f`) and **Android** (`336d28f0`): the screens are
  different. The favourites sit one under another on the settings page,
  below the Thinking and Fast switches, and the model list has a star on each
  row. A favourite on another provider moves the agent there in one step and
  is greyed when that provider is not set up. iOS unstars with a long press,
  Android with the row's star. Not built here: Hakan builds both apps.
