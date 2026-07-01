# Petdex Floating Pet - Bot Integration

This document describes the bot-side half of the Petdex floating pet feature.
The desktop app owns the visual overlay, dragging, window handoff, and live
animation selection. The bot owns the stable feature RPC surface, profile-aware
configuration, Petdex manifest access, asset download/cache, thumbnail rendering,
and spritesheet metadata needed by the renderer.

## TL;DR

- The feature is off by default: `display.pet.enabled = false`.
- Clients call `pet.*` over the shared Feature RPC surface. The same methods work
  through the direct gateway and the cloud relay because they are registered in
  `flowly/channels/feature_rpc.py`.
- The bot never renders a pet and never opens an OS window. It returns config,
  gallery entries, thumbnails, and spritesheet payloads.
- The desktop app maps chat/tool/subagent activity to animation states and can
  render either in-app or in a separate transparent Electron overlay window.
- Petdex network access is server-side, host-pinned to `https://petdex.dev` and
  subdomains, redirect-validated, size-capped, and written atomically.

## Architecture

```text
Desktop / iOS / browser / TUI client
  -> Feature RPC method: pet.info, pet.gallery, pet.select, pet.disable,
     pet.scale, pet.thumb
  -> flowly.channels.feature_rpc
  -> flowly.pet.service
  -> flowly.pet.manifest / flowly.pet.store / flowly.pet.sprites
  -> active profile config + <FLOWLY_HOME>/pets/<slug>/...
```

The transport boundary is intentionally boring. `feature_rpc.FEATURE_METHODS`
is checked by both the direct gateway and the relay path, so a `pet.*` method
added to the dispatch table is available to local desktop, remote gateway, web,
and iOS clients without a second implementation.

The rendering boundary is also intentional. The bot returns data; it does not
pick live animation states such as `waiting`, `review`, or `failed` during a
turn. Those states are chosen by the client from the normal chat/tool/subagent
activity stream. The `pet.*` RPCs only provide the installed pet, spritesheet,
row mapping, frame counts, and user preferences.

## File Map

- `flowly/channels/feature_rpc.py`
  - Registers the public `pet.*` Feature RPC methods.
  - Converts `PetServiceError(code, message)` into Feature RPC errors.
- `flowly/config/schema.py`
  - Defines `display.pet.enabled`, `display.pet.slug`, and `display.pet.scale`.
- `flowly/pet/constants.py`
  - Frame geometry, state vocabulary, scale clamps, and Petdex row taxonomy.
- `flowly/pet/manifest.py`
  - Fetches and caches `https://petdex.dev/api/manifest` for five minutes.
- `flowly/pet/store.py`
  - Profile-aware local storage, slug normalization, host pinning, size caps,
    redirect validation, and atomic writes.
- `flowly/pet/sprites.py`
  - Loads spritesheets, maps rows to states, and trims trailing blank frames.
- `flowly/pet/service.py`
  - Implements the application-level operations used by RPC handlers.
- `tests/test_pet_*.py`
  - Contract tests for config, manifest redirects, service behavior, store
    security, spritesheet taxonomy, and thumbnail generation.

## Configuration

The persisted config shape is:

```json
{
  "display": {
    "pet": {
      "enabled": false,
      "slug": "",
      "scale": 0.33
    }
  }
}
```

Fields:

- `enabled`: whether the selected pet should be shown by clients that support it.
- `slug`: Petdex slug for the active pet. Empty means no active pet.
- `scale`: display multiplier. It is clamped to `[0.1, 3.0]` in both the config
  schema and the service layer.

Changing pet config does not require a gateway restart. The values are read by
`pet.info` and by subsequent `pet.*` calls.

## Storage Layout

All files live under the active Flowly profile, via `get_flowly_home()`:

```text
<FLOWLY_HOME>/pets/
  <slug>/
    pet.json             # manifest-derived metadata and analyzed sprite rows
    spritesheet.<ext>    # downloaded Petdex sheet: webp, png, or gif
    thumb.png            # cached generated/fetched thumbnail, optional
```

`pet.json` stores the local metadata needed to serve the renderer without
hitting Petdex on every app launch:

```json
{
  "slug": "otter",
  "name": "Otter",
  "loopMs": 1100,
  "spritesheet": "spritesheet.webp",
  "rowByState": { "idle": 0, "run": 7 },
  "framesByState": { "idle": 8, "run": 8 },
  "thumbUrl": ""
}
```

A pet is considered installed when `pet.json` exists and a supported
`spritesheet.<ext>` file exists. A cached `thumb.png` alone does not mark a pet
installed.

## RPC Contract

All methods are Feature RPC methods and use the normal RPC envelope of the
caller transport. They return `(result, needs_restart=False)` at the dispatcher
level.

### `pet.info`

Request:

```json
{}
```

Disabled or unset response:

```json
{ "enabled": false }
```

Configured but missing local files:

```json
{ "enabled": false, "slug": "otter", "missing": true }
```

Enabled response:

```json
{
  "enabled": true,
  "slug": "otter",
  "scale": 0.33,
  "name": "Otter",
  "loopMs": 1100,
  "frameWidth": 192,
  "frameHeight": 208,
  "rowByState": { "idle": 0, "run": 7, "failed": 5 },
  "framesByState": { "idle": 8, "run": 8, "failed": 4 },
  "spritesheet": "<base64>",
  "spritesheetMime": "image/webp"
}
```

Notes:

- `spritesheet` is a base64 encoded asset, not a data URI.
- The renderer is expected to prepend the MIME type when needed.
- The payload can be large. Clients should cache it in memory and refetch on
  pet revision/config changes rather than polling aggressively.

### `pet.gallery`

Request:

```json
{}
```

Response:

```json
{
  "pets": [
    { "slug": "otter", "name": "Otter", "installed": true, "active": true },
    { "slug": "foxy", "name": "Foxy", "installed": false, "active": false }
  ],
  "active": "otter",
  "enabled": true,
  "offline": false
}
```

Behavior:

- Online path: fetch Petdex manifest, annotate entries with local installed and
  active flags.
- Offline path: return installed pets only and set `offline: true`.
- Gallery entries do not include thumbnails. Clients should call `pet.thumb` per
  visible entry and cache the result client-side.

### `pet.select`

Request:

```json
{ "slug": "otter" }
```

Response: same shape as `pet.info` enabled response.

Behavior:

1. Normalize and validate the slug.
2. If the pet is not installed, fetch the manifest and find the matching entry.
3. Download the spritesheet through the host-pinned store.
4. Analyze the sheet into `rowByState` and `framesByState`.
5. Write `pet.json` and the asset atomically.
6. Persist `display.pet.slug = slug` and `display.pet.enabled = true`.
7. Return `pet.info`.

Failure guarantee: if install/download/analysis fails, the currently active pet
config is left untouched.

### `pet.disable`

Request:

```json
{}
```

Response:

```json
{ "enabled": false }
```

Behavior: sets `display.pet.enabled = false`. It does not delete assets and does
not clear `display.pet.slug`, so a later select or enable flow can reuse cached
files.

### `pet.scale`

Request:

```json
{ "scale": 0.5 }
```

Response:

```json
{ "ok": true, "scale": 0.5 }
```

Behavior:

- Requires a numeric `scale` field.
- Clamps to `[0.1, 3.0]`.
- Persists the value even if the pet is disabled, so the next enabled pet uses
  the user's preferred size.

### `pet.thumb`

Request:

```json
{ "slug": "otter" }
```

Response:

```json
{ "slug": "otter", "dataUri": "data:image/png;base64,<base64>" }
```

Resolution order:

1. If `<FLOWLY_HOME>/pets/<slug>/thumb.png` exists, return it.
2. If the pet is installed, crop the idle row's first frame from the installed
   spritesheet, downscale to max 128 px on the longest side, cache `thumb.png`,
   and return it.
3. Otherwise fetch the manifest and use `thumb` or `thumbnail` if present.
4. If the manifest has no thumbnail but has a spritesheet URL, download that
   sheet to a temporary `_thumbsrc*` file, render the idle frame, cache only the
   small `thumb.png`, delete the temp sheet, and return the thumbnail.

The thumbnail path deliberately avoids marking a pet installed. Previewing a pet
should not write `pet.json` or leave the full spritesheet behind.

## Error Model

Service failures become Feature RPC errors with structured codes. Common codes:

- `INVALID`: missing/invalid `slug` or non-numeric `scale`.
- `MANIFEST_UNAVAILABLE`: Petdex manifest fetch or parse failed.
- `NOT_FOUND`: requested slug does not exist in the manifest.
- `DOWNLOAD_FAILED`: host pin, redirect, HTTP, size, or write failure while
  downloading an asset.
- `PET_ERROR`: fallback for unexpected pet service errors.

Clients should treat errors as recoverable UI failures. A failed `pet.select`
should not hide or mutate the current active pet because the service preserves
config until installation succeeds.

## Petdex Network Policy

Outbound Petdex access is intentionally constrained:

- Manifest URL: `https://petdex.dev/api/manifest`.
- Allowed asset hosts: `petdex.dev` and subdomains such as `assets.petdex.dev`.
- Scheme must be HTTPS.
- Redirect validation checks the final URL and every redirect hop.
- `evilpetdex.dev` and `petdex.dev.evil.com` are rejected.
- Asset downloads are capped at 20 MiB.
- Writes go through a random `.part` temp file and `os.replace()`.
- User agent is `flowly-petdex`.
- Manifest responses are cached in memory for 300 seconds.

This keeps the desktop/web client from fetching arbitrary remote image URLs and
keeps bot-side asset writes crash-safe.

## Spritesheet Taxonomy

Petdex sheets are grid atlases. Flowly assumes these frame dimensions:

```text
frame width:  192 px
frame height: 208 px
default loop: 1100 ms
```

The supported internal states are:

```text
idle, wave, run, failed, review, jump, waiting
```

Current Petdex atlases are usually 9 rows:

```text
0 idle
1 running-right
2 running-left
3 waving
4 jumping
5 failed
6 waiting
7 running
8 review
```

Older atlases can use the legacy 8-row order:

```text
0 idle
1 wave
2 run
3 failed
4 review
5 jump
6 extra1
7 extra2
```

`flowly.pet.constants.state_row_index()` chooses the taxonomy by row count and
resolves aliases. For example, internal `run` maps to the canonical `running`
row on the current 9-row Petdex layout. If a requested state is absent on a
shorter sheet, it falls back to the idle row.

`flowly.pet.sprites.analyze()` also counts non-blank frames per row. It trims
trailing fully transparent frames while preserving blank frames between nonblank
frames, so animation timing remains faithful to the sheet.

## Desktop And TUI Boundary

The bot-side feature is UI-agnostic:

- Desktop uses `pet.info` to render the active pet and `pet.gallery`/`pet.thumb`
  in settings.
- Desktop owns the in-app/floating overlay window, drag handoff, click-through,
  animation state selection, and local sprite rendering.
- TUI can call the same `pet.*` methods to configure a pet, but the bot will not
  spawn a terminal or OS-level pet. Any TUI display would be a client feature.
- Remote/iOS messages can move the desktop pet only through the normal activity
  signals that reach the desktop client. `pet.*` RPCs are not a live activity
  stream.

## Performance Notes

- `pet.info` returns the full spritesheet. It should be called on mount, on
  selected-bot change, or after a pet config revision. Avoid polling it.
- `pet.gallery` uses the five-minute manifest cache.
- `pet.thumb` caches rendered PNG previews on disk and should be lazy-loaded by
  the client as gallery rows become visible.
- Pillow is imported lazily only when loading/rendering images.
- The package has no import-time side effects, so registering the RPC surface is
  cheap.

## Operational Runbook

`Unknown RPC method: pet.info`

- The desktop/renderer is newer than the running bot backend.
- Confirm the running bot is at a commit containing `pet.info` in
  `flowly/channels/feature_rpc.py`.
- Restart the gateway after updating the bot process if the old Python process
  is still in memory.

Gallery loads but previews are blank

- Check `pet.thumb` responses, not only `pet.gallery`.
- Verify Petdex manifest access and host-pinned asset redirects.
- Check that Pillow is available in the environment.
- Confirm `thumb.png` is being written under the active `FLOWLY_HOME` profile.

Selecting a pet fails but the old pet remains

- Expected behavior. `pet.select` only mutates config after the new asset is
  installed and analyzed successfully.

A selected pet reports `missing: true`

- Config points at a slug but `pet.json` or `spritesheet.<ext>` is missing.
- Re-select the pet to reinstall it, or disable the pet from the client.

Scale does not change in the client

- Verify `pet.scale` returns the clamped value.
- The backend has persisted the setting; live resize is a client-side state sync
  concern.

## Test Coverage

Focused tests:

```bash
uv run --frozen --extra dev pytest   tests/test_pet_config.py   tests/test_pet_manifest.py   tests/test_pet_service.py   tests/test_pet_sprites.py   tests/test_pet_store.py   tests/test_pet_thumb.py
```

Useful assertions covered by those tests:

- Pet config defaults and scale persistence.
- Manifest redirect following without leaving `petdex.dev` subdomains.
- Host pinning rejects HTTP, lookalike domains, and off-host redirects.
- Oversized assets are rejected.
- Downloads and metadata writes are atomic.
- Gallery falls back to installed pets when Petdex is offline.
- `pet.select` preserves the existing active pet on failure.
- 9-row Petdex taxonomy maps `run` to the canonical `running` row.
- `pet.thumb` renders previews from installed sheets and from manifest-only
  spritesheets without marking those pets installed.
