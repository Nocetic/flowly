# The media library

*How Flowly remembers everything it made, and how three clients browse it.*

This document covers the whole feature end to end — the bot's index, the RPC
surface, and what Desktop and iOS do with it. Read it instead of reading the
code; the code is commented, but the *why* lives here.

---

## 1. The problem

Every file Flowly generates or is handed lands in one flat directory,
`~/.flowly/media`. Delivery has always worked off that directory — a `mediaId`
**is** the basename of a file in it — but nothing ever remembered what any of it
*was*.

The provenance existed. `flowly/media/generate.py` measures a clip's duration and
dimensions, `describe()` records the provider and model, and all of it travels
with the reply:

```
tool result   _reply_media / _reply_media_assets       flowly/agent/reply_media.py
    ↓
agent loop    reply_media_assets: list[MediaAsset]     flowly/agent/loop.py
    ↓
outbound      metadata["media_assets"]                 flowly/media/assets.py
    ↓
session       ~/.flowly/sessions/<key>.jsonl extras    flowly/session/manager.py
    ↓
history       rebuilt into Attachment V2 on reload     flowly/gateway/server.py
```

…and then stopped. Everything was written into **one message's metadata inside
one chat transcript**. There was no query that answered "show me every image
this agent has made", and no way to find `img-9f3a71c2b804.png` except to
recognise it by eye while scrolling the conversation that produced it.

Meanwhile the artifact store (`flowly/artifacts/store.py`) had the opposite:
a real SQLite store with FTS5, versions, an RPC surface and a browse screen on
every client — but only for text documents.

---

## 2. The insight that shaped the design

**The delivery layer was already gallery-ready.**

`mediaId` is a bare basename, and both transports resolve it through one shared
implementation in `flowly/media/serving.py`:

| Transport | Route | Notes |
|---|---|---|
| Direct gateway | `GET /api/media?id=` · `/api/media/stream` | Range-capable |
| Relay bridge | `media.fetch` → `read_media_window()` → `media.result` | stateless 4 MB windows, relay stores nothing |
| Hosted | the attachment's own `cdnUrl` | video on the relay path |

`resolve_media_id` enforces containment once, for everyone: the id must be a
basename (separators, `..` and leading dots are rejected before touching the
filesystem), and the *resolved* target must still sit inside the *resolved*
media directory — which is what defeats a symlink planted in the folder.

All of it is **kind-agnostic**. So a gallery needed exactly one thing: a *list*
endpoint returning rows in the shape clients already render. Every player,
thumbnail, download and Range path then works unchanged.

> **This is the load-bearing decision of the entire feature.** A library item is
> an Attachment V2 object with provenance added — not a new type. Desktop hands
> it to `useMediaSource`; iOS hands it to `AudioAttachmentView`. Both inherit the
> stall recovery, position-preserving reload and credential refresh that already
> took real work to get right.

**Second insight: one dispatch entry lights up every surface.** The direct
gateway falls through generically (`flowly/gateway/server.py`):

```python
elif method in feature_rpc.FEATURE_METHODS:
    await self._handle_feature_rpc(ws, rpc_id, method, params)
```

and the relay serves the same table. So five entries in `_DISPATCH` reach
desktop-local, desktop-gateway, desktop-relay, iOS-gateway and iOS-relay at
once. (`artifacts.*` predates that fallthrough and still carries seven
hand-written WS branches. The library deliberately does not copy that.)

---

## 3. The index — `flowly/media/library.py`

### 3.1 Three rules

**The index is a cache of the disk, not the other way round.** Files are the
truth: generation writes them, retention deletes them, and neither asks
permission. Every read path tolerates a row whose file is gone, and
`reconcile()` — not a foreign key — keeps the two in step. Losing
`media.sqlite` costs provenance, never bytes; a rescan rebuilds a working (if
plainer) library from the directory alone.

**Only servable media is indexed.** `SERVABLE_MIME_PREFIXES` is image, video and
audio. A row for anything else would be a row no client could open, so
documents, `model-catalog.json` and other bookkeeping never enter. A test pins
this: `INDEXED_KINDS == {p.rstrip("/") for p in SERVABLE_MIME_PREFIXES}`.

**The database lives OUTSIDE the media directory.** `prune_media` treats every
regular file directly inside `~/.flowly/media` as a deletable unit, so a
`.sqlite` file there would be classified as a stray `file` and evicted under the
image budget. Hence:

| Path | What |
|---|---|
| `~/.flowly/media.sqlite` | the index (profile root, safe from retention) |
| `~/.flowly/media/` | the files themselves |
| `~/.flowly/media/thumbs/` | thumbnail cache — a **subdirectory**, which retention documents itself as leaving alone |

### 3.2 Schema

```sql
CREATE TABLE media (
  media_id     TEXT PRIMARY KEY,   -- basename == the wire `mediaId`
  asset_id     TEXT,               -- MediaAsset.id when a generator supplied one
  kind         TEXT NOT NULL,      -- image | video | audio
  source       TEXT NOT NULL,      -- generated | received
  mime_type    TEXT, file_name TEXT, size INTEGER,
  width INTEGER, height INTEGER, duration_ms INTEGER,
  poster_id    TEXT,               -- basename of the poster sidecar
  provider TEXT, model TEXT, prompt TEXT, title TEXT,
  session_key  TEXT, channel TEXT, message_ts TEXT,   -- "Open in chat"
  status       TEXT NOT NULL,      -- ready | expired
  starred      INTEGER NOT NULL DEFAULT 0,
  thumb_state  TEXT NOT NULL,      -- pending | ready | none
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE media_fts USING fts5(prompt, title, file_name, media_id UNINDEXED);
```

`media_id` is the primary key rather than a synthetic id. It is already the
delivery key every client holds, so a chat attachment and its library row
cross-reference for free.

**`thumb_state`** exists so a hopeless file is attempted once, not on every page
render. Audio has no visual at all; a clip on a host without ffmpeg has no
poster. Both land on `none` and stay there.

### 3.3 Upsert semantics

Every write goes through one statement whose shape is:

```sql
ON CONFLICT(media_id) DO UPDATE SET
  prompt     = COALESCE(NULLIF(excluded.prompt, ''), media.prompt),
  ...
  created_at = MIN(media.created_at, excluded.created_at)
```

A later, better-informed write wins; a write that knows **less** — typically a
reconcile scan running after a real `record()` — must not blank out provenance
the generator supplied. `created_at` only ever moves *earlier*, so a rescan
cannot reshuffle the gallery. Both are pinned by tests.

### 3.4 The write path — one choke point

```python
AgentLoop._index_turn_media(session, reply_assets, inbound_paths, channel)
```

Called after `self.sessions.save(session)` at **both** turn sites in
`flowly/agent/loop.py` (the user path and the system/announce path).

Why after the save: the timestamp recorded has to be the one `chat.history` will
publish. That pair — `session_key` + `message_ts` — is the whole mechanism
behind "Open in chat", so it is read back off the persisted message rather than
guessed from the clock. `extend_with_turn_messages` puts produced media
(`media_assets`) on the closing assistant message and received media (`media`) on
the user message, so each side is found by scanning back for its own marker.

This one hook covers **everything**, because all inbound media — desktop and iOS
uploads through the gateway, iMessage, Telegram — reaches the loop as
`msg.media`. No per-channel hooks were needed.

**Files outside the media directory are skipped, not indexed.** The desktop's
local-attachment optimisation hands the gateway a path straight out of the
user's Finder. Flowly never copied those bytes, cannot serve them by `mediaId`,
and must not pretend to own them.

### 3.5 `reconcile()` and backfill

Four passes, in order:

1. every servable file with no row gets one, as `source=received` — a file
   nobody claimed to have generated is, by elimination, one that arrived;
2. rows whose file is gone flip to `expired` rather than vanishing, so a chat
   bubble still linking to them can explain itself;
3. rows that came back (a restored backup) flip to `ready`;
4. thumbnails with no row are deleted.

Poster sidecars are recognised with the **same rule** `prune_media` groups on —
an image sharing a stem with a video or audio file is that file's preview — so a
clip does not appear twice in the grid.

**Backfill** walks session transcripts once, guarded by a `meta` flag, and
recovers provider/model/prompt/session for files that predate the index. It
reads `<key>.full.jsonl` in preference to `<key>.jsonl`: the canonical file is
the LLM working context, which compaction rewrites as `[summary] + recent`, so
on any long conversation the early turns — and the media they carried — are
simply not in it.

Both run on a daemon thread at gateway start (`_start_media_library_sync`). The
work is pure disk I/O, worthless to wait for, and if the process exits mid-scan
the next start picks up where it left off.

### 3.6 Thumbnails

Rendered lazily, at most `MAX_THUMBS_PER_LIST = 24` per `list()` call, so a
first load against a large cold library cannot become a hundred ffmpeg
invocations in one RPC. Cached at `~/.flowly/media/thumbs/<media_id>.jpg`,
256 px longest edge, JPEG q72 — roughly 10 KB, so a 48-item page inlines in well
under a megabyte and fits comfortably inside the relay's 10 MB frame.

**Encoding matters:** the wire carries **raw base64**, not a data URL, because
that is what a chat attachment has always carried and what clients already wrap
(`data:image/jpeg;base64,${thumbnail}`). Emitting a data URL here would double
the prefix and break every tile.

*Rejected alternative:* thumbnails as a `BLOB` column. 2 000 items × 48 KB ≈
96 MB of database, and it couples the cache's lifetime to the row's.

### 3.7 Retention

`prune_media` gained two things:

- **`audio_max_size_mb`, default 1000.** Audio previously shared the 500 MB
  *image* budget. That was defensible while audio was invisible; a browsable
  Audio tab full of generated music makes it wrong — a short music session would
  silently evict months of screenshots. The parameter sits **after** `protect`
  in the signature on purpose: callers already pass the first four arguments
  positionally, and moving `protect` would reinterpret a protect-list as a byte
  budget.
- **`deleted_paths` in the summary**, so `prune_after_generation` can expire
  exactly those rows via `expire_paths_async` instead of making the library
  rediscover the fact by rescanning.

---

## 4. The RPC surface

Five entries in `flowly/channels/feature_rpc.py`, all `(handler, True, False)` —
never restart-gated, because browsing a gallery must not ask anyone to restart
their agent.

| Method | Params | Returns |
|---|---|---|
| `media.library.list` | `kind?` `source?` `search?` `sessionKey?` `starred?` `limit` `offset` `withThumbs?` `includeExpired?` | `{items[], total}` |
| `media.library.get` | `id` | `{item}` |
| `media.library.star` | `id`, `starred` | `{item}` |
| `media.library.delete` | `id` | `{ok}` |
| `media.library.stats` | — | counts, bytes, retention config |

An item:

```jsonc
{
  // delivery — byte-identical to what a chat bubble receives
  "version": 2, "id": "img-a1b2c3.png", "mediaId": "img-a1b2c3.png",
  "kind": "image", "fileName": "…", "mimeType": "image/png",
  "size": 812345, "width": 1024, "height": 1024, "durationMs": null,
  "status": "ready", "thumbnail": "<raw base64>",

  // provenance — library-only, never on the chat wire
  "source": "generated", "provider": "fal", "model": "flux/dev",
  "prompt": "a red car", "sessionKey": "cli:main",
  "messageTs": "2026-01-01T00:00:09", "starred": false,
  "createdAt": 1754000000.0, "thumbState": "ready"
}
```

`search` runs FTS5 over **prompt**, title and filename. Tokens are quoted and
ANDed with a prefix match, so punctuation a user typed cannot be read as FTS
syntax and turn a search into a syntax error.

`media.library.delete` removes the row **and the bytes** — file, poster sidecar,
cached thumbnail. The chat bubble that referenced them then renders the
`expired` placeholder that already exists for retention-pruned media: one
behaviour for "the media is gone", not two.

### Live updates

Mutations publish `media.changed` through a broadcast callback the gateway
bootstrapper wires in (`flowly/media/library.py::set_on_change`), mirroring the
artifact pattern. It carries counts only — a "look again" ping, not a payload —
and fans out to the direct-gateway WS and the relay leg. A failing subscriber
cannot break a mutation; a test pins that.

---

## 5. Desktop

`/artifacts` → `/library`, with the old path redirecting **and forwarding router
state**, because deep links (command palette, Vox's tag link) carry their target
there and a bare `<Navigate>` would drop it.

```
pages/Library/
  index.tsx            tab shell, filters, actions, delete confirm
  useMediaLibrary.ts   one hook, three transports
  MediaTab.tsx         images + video grid
  AudioTab.tsx         audio rows
  MediaCard.tsx        tile + the shared ⋯ menu
  MediaLightbox.tsx    full size, keyboard paging
  library.helpers.ts   pure rules, unit-tested
types/media-library.ts  wire shape + normalisation, unit-tested
```

**The artifact body was not physically moved.** `Artifacts.tsx` (2 042 lines)
gained an `embedded` prop that drops its own page header, and `index.tsx`
renders `<Artifacts embedded />` for the Files tab. Relocating two thousand
lines to gain a directory name is where regressions hide; the file is cohesive
and the tab shell is what actually needed to exist.

**Three transports, one call path.** All of them answer the same methods, so
`useMediaLibrary` is one implementation with three ways to place a call:

| Selection | How |
|---|---|
| `local` | `window.electronAPI.flowlyai.featureRpc` (generic passthrough) |
| `gateway` | `window.gatewayRemote.featureRpc(gatewayId, …)` |
| `relay` | the typed `BotFeatureClient` from `useBotConnections` |

**Media asks for no `kind`.** The bot filters on one kind at a time, so the
Media tab requests everything and drops audio client-side — one request instead
of two plus a merge, and the ordering the bot chose survives.

**`isUnsupportedError`** turns an older bot's `UNKNOWN_METHOD` into
`supported: false`, which hides Media and Audio entirely. The Files tab carries
on. A version gap is not a failure, and the user is never shown an error for a
feature they did not ask about.

---

## 6. iOS

```
Flowly/Models/MediaLibraryItem.swift             pure model + rules
Flowly/Models/MediaLibraryItem+Attachment.swift  the UIKit bridge
Flowly/Views/Library/LibraryView.swift           segmented shell
Flowly/Views/Library/MediaLibraryStore.swift     paging, filters, mutation
Flowly/Views/Library/MediaTileView.swift         grid tile + shared menu
Flowly/Views/Library/MediaDetailView.swift       full screen pager
Flowly/Views/Library/AudioLibraryList.swift      audio rows
```

`MediaLibraryItem` is deliberately free of SwiftUI, UIKit and Firebase. That
isolation is not tidiness — it is what lets `scripts/verify-library.sh` compile
and **run** the parsing and formatting rules on the host. The UIKit bridge lives
in a separate file for the same reason.

**The Library is the first screen where a relay bot answers for itself.**
`ArtifactsView` reads Firestore on the relay path and feature_rpc on the gateway
path. Media has no cloud copy by design (the video-generation decision: no S3,
the relay bridges bytes off the bot's disk), so Media and Audio talk to the bot
over feature_rpc on **both** transports.

**Save a copy downloads the file first.** Sharing a ticket URL that expires in
minutes is useless in somebody's Photos app.

**"Open in chat" opens the conversation, not the exact turn.** `messageTs` is
carried on every item and recorded by the bot; scrolling to it would need a
target-message parameter `ChatDetailView` does not have. Adding that later is a
change to one screen and no protocol.

### Verification

`scripts/verify-library.sh` — three steps, mirroring `verify-audio.sh`:

1. `swiftc -parse` every file;
2. `swiftc -typecheck` **every** Library file against the real iOS SDK, with
   `scripts/library-typecheck-stubs.swift` standing in for app-module symbols
   that cannot resolve on the host;
3. compile and **run** `scripts/library-rules-harness.swift` — 43 assertions on
   parsing, captions, durations, tab routing and formatting.

Step 2 covers all six files rather than the leaves for a specific reason:
"cannot find X in scope" inside a view body is exactly what `-parse` waves
through, and exactly what has reached a real Xcode build before. The Xcode build
is still the final authority.

---

## 7. Decisions, and what they cost

| Decision | Why | What it costs |
|---|---|---|
| Files tab holds artifacts only | `SERVABLE_MIME_PREFIXES` is image/video/audio; a PDF cannot be served, so a row for one would be unopenable | a produced PDF is not browsable; widening it means an explicit mime allowlist and a new surface |
| Received media is shown, behind a filter | everything you gave the agent is part of your world | one `WHERE` clause and one control per surface |
| Thumbnails on disk | safe from retention, owned by reconcile, cheap to inline | a cache directory to sweep |
| Audio gets its own retention budget | music would otherwise evict screenshots | one more config key (additive, defaulted) |
| Deleting removes the bytes | one behaviour for "media is gone", whether you deleted it or retention did | irreversible; the dialog says so plainly |
| `media_id` as primary key | already the delivery key every client holds | renaming a file on disk orphans its row (reconcile expires it) |

---

## 8. Known limits

- **Enumeration.** Any authorised client could already fetch any `mediaId`; a
  list makes the set *discoverable*. All five methods require auth.
- **`kind: file`.** Indexed nowhere and shown nowhere. If documents ever need to
  be browsable, the change is `SERVABLE_MIME_PREFIXES` plus an allowlist — not
  the index.
- **Backfill accuracy.** A transcript filename maps back to a session key by
  restoring the first `_` to a `:`. Channel names contain no underscore so this
  is exact today; an exotic key yields an item with no "Open in chat" rather
  than a wrong one.
- **iOS "Open in chat"** opens the conversation, not the message (§6).
- **Cross-platform byte formatting** differs on exact rounding ties (Swift
  rounds half-to-even, JS half-away-from-zero). Immaterial in a header line;
  the tests deliberately avoid pinning a tie.
