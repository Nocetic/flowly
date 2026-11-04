# Model settings, provider sign-ins and Grok by device code (2026-11-01 – 2026-11-02)

Status (2026-11-02): on `main`, pushed.

This page records what Core gained for the model settings window that every
app now shows (Desktop, iOS, Android), and why each rule is the way it is. The
design and the owner's requests live in `flowly-server`:
`docs/cloud/design/model-settings.md`, `provider-switching.md` and
`reasoning-effort.md`. The app side is recorded in each app's repository (see
"Other repositories" at the end).

The Mac's named agents and gateway run `~/.local/bin/flowly`, a uv tool
installed **editable from this checkout** (`~/.local/share/uv/tools/flowly-ai/uv-receipt.toml`).
A change here reaches them after the gateway and the named agents restart. The
cloud computers run their own Core build and need an update to get any of this.

## What the owner asked

- One window per agent for its model, its provider and how hard it thinks:
  a slider of levels (the "Effort" section), a Thinking switch and a Fast
  switch, shown only where the model and provider support them. No approval
  mode and no favourites in it.
- Switching provider works from any app as it does on Desktop: API keys are
  sealed on the device, subscriptions sign in by code.
- "Everything a person can do on the Mac they can do from the phone and from
  Android, with full control of their agent while away from the computer."
- Levels exact per provider ("I hope the other providers do the same"): they
  did not, which led to the per-provider work below.

## Effort (how hard a model thinks)

`flowly/providers/effort.py`

- The ladder is `none, minimal, low, medium, high, xhigh, max, ultra`.
- `clamp_effort(asked, levels)`: a level the model lacks runs at the nearest
  **weaker** level it takes. It is never raised, and an enabled ask never
  lands on `none`. `none` on a model that must think becomes its weakest level.
- The saved level is **kept** across model changes. Ultra saved on an agent
  runs as Max on a model that stops at Max, and comes back to Ultra on a model
  that has it. Before, Codex fell back to the model's default, Anthropic never
  sent the level, and changing the model erased the saved one. `model.set`
  still returns `effortReset` for old apps, but it is always `false`
  (`feature_rpc.model_set`, `profile.py` `_profile_effort`).

Where each provider's levels come from:

- **Codex (ChatGPT subscription).** From OpenAI's model catalogue.
  `flowly/integrations/model_catalog.py` keeps an offline disk copy at
  `<FLOWLY_HOME>/codex_models_cache.json`, keyed by a sha256 tag of the
  account (`_codex_account_tag`). The window shows the levels with no network,
  and another account's copy is never used.
- **Anthropic.** Clamped to the model family's levels (`anthropic_provider.py`).
- **OpenRouter and Flowly.** Read from the catalogue row's `reasoning` object
  (`openrouter_efforts(row)`):
  - `supported_efforts: null` means every gateway level from `none` to `max`
    (`OPENROUTER_GATEWAY_EFFORTS`); a missing object means the model does not
    think.
  - `mandatory`, `default_effort` and `default_enabled` are read too.
    `default_enabled: false` is sent to the apps as `defaultThinking: false`,
    and the slider shows Off until the person turns thinking on.
  - The request body is `{"reasoning": {...}}`, and `none` becomes
    `{"enabled": false}` (`openrouter_provider._reasoning_field`).
  - flowly-app passes the object through its catalogue (`lib/models/catalog.ts`
    `slimReasoning`, commit `d796a15`, **not deployed**: the Flowly account's
    levels need that deploy).
- **xAI (Grok).**
  - The grok-4.6 family takes `low` to `xhigh`, other reasoning Grok models
    take `low` to `high`, and `-non-reasoning` models take none
    (`XAI_GROK46_EFFORTS`, `XAI_LEGACY_EFFORTS`, `xai_efforts`).
  - The level is sent as `reasoning_effort` with an API key and as
    `reasoning.effort` with the OAuth sign-in (`xai_responses_provider.py`).
  - When xAI refuses the field, the request is retried without it, and that
    model is remembered in `_EFFORT_REFUSED_MODELS`, so later requests leave
    the field out.

## Fast (service tiers)

`flowly/providers/service_tier.py`, `model_catalog.service_tier_choice`

- Codex catalogue rows carry `service_tiers` (`priority` is shown as Fast;
  `ultrafast` exists too). The chosen tier is sent as `service_tier`. A model
  that refuses it is retried without it and remembered
  (`codex_responses_provider._service_tier_refused`).
- Saved as `agents.defaults.serviceTier` for the main agent (`model.set
  {serviceTier}`) and in a named agent's profile (`profiles.configure
  {serviceTier}`). `""` means standard speed.

## What the apps read and write

- `model.list` rows: `efforts`, `defaultEffort`, `defaultThinking`,
  `serviceTiers`.
- `provider.active`: `providerKey`, `model`, `effort`, `serviceTier`.
- `provider.list` rows (no secrets): `key`, `name`, `kind` (`account`,
  `subscription`, `key`, `url`), `login` (`device`, or `null`), `keyable`,
  `hasKey`, `shared`.
- `provider.set {key}` (the main agent) or `profiles.configure {name,
  provider}` (a named agent) chooses the provider.
- `provider.set_key {key, kid, sealed}` saves a key the device sealed with
  HPKE to the agent's key (request id `provider:<key>`, the single value
  `PROVIDER_KEY`). `{key, value: ""}` removes it. A key never travels in the
  clear.
- `provider.login.start {key}`, `.status {loginId}`, `.cancel {loginId}`: a
  subscription sign-in by device code (`flowly/auth/provider_login.py`).

## Sign-ins belong to the computer (`18de2225`)

`flowly/auth/shared_signin.py`

Before, a ChatGPT or Grok sign-in lived in the home of the agent that made it,
so every named agent showed it as "main agent only". Copying the tokens was
not an option, because the issuer rotates the refresh token on every refresh
and two copies log each other out. Now:

- `signin_home()` is the main agent's home for every named agent. The keychain
  service is the same (`signin_scope_suffix()` is empty for the default home).
- A refresh happens under a computer-wide `fcntl` lock
  (`refresh_lock(name)`, file `<home>/credentials/<name>.lock`). Whoever takes
  the lock reads the store again first: another agent may have refreshed
  already, and its new token is the one to use.
- A refresh token the issuer rejects clears the store **only if the store
  still holds that token**. A different token means another agent refreshed in
  between, and its sign-in is kept.
- `provider.list` rows carry `shared`. The apps show "main agent only" only
  when an older Core leaves it out.
- Tests: `tests/test_shared_signin.py`.

## Grok signs in with a device code (`df7c3e8a`)

The owner saw "Only on a Mac" (Desktop: "Only on this Mac") on xAI Grok OAuth
in every app.

- **Cause.** Core's Grok sign-in redirected the browser to
  `http://127.0.0.1:56121/callback`, so it worked only with a browser on the
  agent's own machine. No app ever offered it, not even Desktop on that same Mac.
- **Fix.** xAI publishes the device authorization grant (RFC 8628) for the same
  public client. Its discovery document lists `device_authorization_endpoint`
  and the `urn:ietf:params:oauth:grant-type:device_code` grant. An open-source
  agent in the owner's research folder uses exactly that flow.
- `xai_oauth.login_with_device_code(client_id, on_user_code, timeout_seconds,
  cancelled)`:
  - POSTs `client_id`, `scope` and `referrer=flowly` to the device endpoint.
  - Calls `on_user_code(user_code, verification_url)` once the code is known.
  - Polls the token endpoint. `authorization_pending` waits, `slow_down` adds
    5 s, `expired_token` raises "timed out", `access_denied` raises "not
    approved", and a 403 raises `XAIEntitlementError`.
  - Saves the tokens with `save_token_payload` in the shared store.
- **The page is pinned** (`_device_verification_url`). It must be `https`, host
  `accounts.x.ai`, port 443, with no user info and no fragment. Otherwise the
  login fails before any code is shown, so a bad answer can never send the
  apps elsewhere. `verification_uri_complete` (which carries the code) is
  preferred.
- `provider_login.DEVICE_LOGIN_PROVIDERS` now includes `xai_oauth`, and
  `_sign_in` dispatches to its flow. `provider.list` says `login: "device"`
  for it, so every app shows "Sign in", and the same code UI as ChatGPT works
  unchanged.
- The loopback login still exists for the CLI (`flowly xai login`,
  `--manual-paste`).
- Tests: `tests/test_xai_oauth.py` (device flow against a fake xAI: pending,
  slow_down, success, every unsafe page, how it ends, cancel) and
  `tests/test_provider_switching.py` (`test_grok_signs_in_with_a_code_from_any_device`).

## A named agent moved to another provider takes a model it serves (`7bb6335d`)

- **Symptom (iOS).** After switching provider the window went back, but showed
  no effort levels and no model could be picked.
- **Cause.** `profiles.configure {provider}` kept the named agent's old model:
  ChatGPT to Grok left `gpt-5.5` configured. The new provider refused it, and
  the apps showed a model their new list did not have. The main agent never
  had this, because `provider.set` → `set_active_provider` already moved an
  unserved model to the provider's curated default.
- **Fix.** `profile_host._model_for_provider` applies the same rule to named
  agents. When `provider` is given without `model` and the new provider cannot
  serve the agent's own model (or the one it follows), the model becomes
  `DEFAULT_MODELS[provider]`. A model it serves, an explicit model, or a
  provider not set up yet (`_build_for` returns `None`) leaves the model as it
  is.
- Test: `tests/test_profile_host.py::test_a_named_agent_switched_to_another_provider_takes_a_model_it_serves`
  fails without the fix.
- `profiles.configure` with runtime fields stops the named agent's runtime
  (`await self.stop(name)`). The apps therefore read what the agent runs again
  until it reports the new provider (iOS `ModelSettingsStore.loadAfterProviderChange`,
  Android `ModelSettingsController.loadAfterProviderChange`).

## Verification

- `tests/test_model_settings.py`, `tests/test_effort_levels.py`,
  `tests/test_reasoning_effort.py`, `tests/test_shared_signin.py`,
  `tests/test_xai_oauth.py`, `tests/test_provider_switching.py`,
  `tests/test_profile_host.py`, `tests/test_codex_model_catalog.py`. That last
  file has an autouse `_own_flowly_home` fixture, because the disk cache made a
  test depend on the session's shared home.
- Full suite on 2026-11-02: 10289 passed. 5 failures and 7 errors are
  pre-existing screen tests, unrelated to this work.
- `tests/test_provider_request_coordinator.py::test_a_cut_keeps_every_message_in_place_around_a_pin`
  passes alone and fails when run with `-k "provider or xai or login or
  feature_rpc"`. It fails the same way without these changes: the failure
  depends on test order and existed before.
- Run the suites in the background with `-rfE`. A truncated tail once hid a
  failure.

## Open

- Anthropic's thinking on/off switch (the slider only clamps for now).
- Cloud computers need a Core update for shared sign-ins and Grok by device code.
- flowly-app `d796a15` (the Flowly account's per-model levels) is not deployed.
  There is no flowly-app deploy before launch.
- `flowly-server` `chat-log.ts`: tool results are cut at 4000 characters for new
  log entries but not for entries migrated from the old format
  (`legacyEntries`). Found by the iOS memory audit; not changed yet.

## Commits (Core)

`4cd60be9` effort per agent, `0e8b4830` apps connect providers (sealed keys,
ChatGPT sign-in), `de36f026` merge, `b0202680` Fast and thinking off,
`4b176238` levels from where each provider says them, `18de2225` shared
sign-ins, `df7c3e8a` Grok device sign-in, `7bb6335d` named agent model on
provider switch.

## Other repositories

- **Desktop** (`flowly-bots-desktop`): `docs/model-settings-2026-11-02.md`.
- **iOS** (`Flowly-iOS`): `docs/engineering/model-settings-and-chat-fixes-2026-11-02.md`.
  It also records the chat freeze, home previews and question placement fixed
  in the same session.
- **Android** (`flowly-android`): `docs/engineering/model-settings-and-chat-fixes-2026-11-02.md`.
- **Design** (`flowly-server`): `docs/cloud/design/model-settings.md` and
  `provider-switching.md`.
