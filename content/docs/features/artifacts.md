---
title: Artifacts
eyebrow: Features
description: Renderable, versioned outputs — HTML, charts, diagrams, forms, code — the agent creates, pins, and serves to your dashboard.
---

When the agent produces something you'd want to *look at* rather than read inline
— a chart, a diagram, a styled HTML report, a form, a code snippet — it can save
it as an **artifact**. Artifacts are first-class, renderable outputs: each one has
a type, a title, full content, tags, and a version history. They persist across
sessions, are searchable, and can be pinned to your dashboard or served to client
apps.

Everything is local. Artifacts live in a single SQLite file under your Flowly
home — nothing is synced to any server.

## What an artifact is

An artifact is a stored, typed piece of content the agent can create and revise
over time. Each artifact has:

- a stable **id** (e.g. `art_…`)
- a **type** that tells clients how to render it
- a **title** and full **content**
- a **version** number that bumps every time the content changes
- optional **tags**, **pinned** state, and **dashboard size**

Because content edits are snapshotted, you always keep the earlier versions.

> [!NOTE]
> Some artifacts are *internal context* — working notes the agent keeps for
> itself (for example, fetched web content). These are hidden from listings by
> default. When you ask to save or show one, the agent can **promote** it to make
> it user-visible.

## Artifact types

The `type` field is one of:

| Type | Use for |
| --- | --- |
| `html` | Styled reports, pages, rich layouts |
| `svg` | Vector graphics |
| `markdown` | Formatted documents |
| `csv` | Tabular data |
| `json` | Structured data |
| `code` | Source snippets (set `language` for highlighting) |
| `mermaid` | Mermaid diagrams |
| `latex` | LaTeX / math |
| `form` | Interactive forms |
| `chart` | Data charts |

> [!TIP]
> For `code` artifacts, pass `language` (e.g. `python`, `javascript`, `sql`) so
> clients can syntax-highlight correctly.

## Creating and managing artifacts

The agent works with artifacts through a single `artifact` tool with an `action`
parameter. The available actions:

| Action | What it does | Key params |
| --- | --- | --- |
| `create` | Create a new artifact | `type`, `title`, `content` (required); `pinned`, `dashboard_size`, `tags`, `language` |
| `update` | Update an existing artifact; a content change snapshots a new version | `artifact_id`; `title`, `content`, `pinned`, `dashboard_size`, `tags` |
| `get` | Fetch a single artifact by id (supports `limit`/`offset` to page through content) | `artifact_id` |
| `list` | List artifacts, with filters | `type`, `pinned`, `search`, `tags`, `limit`, `include_internal` |
| `pin` | Pin or unpin to the dashboard | `artifact_id`, `pinned` |
| `export` | Stream content straight to disk (no model round-trip) | `artifact_id`, `path`, `overwrite` |
| `promote` | Make an internal context artifact user-visible | `artifact_id` |
| `delete` | Delete an artifact permanently | `artifact_id` |
| `get_versions` | Return the version history for an artifact | `artifact_id` |

> [!NOTE]
> `export` writes the original bytes directly from storage to a file — it doesn't
> re-emit content through the model. Exports are sandboxed to your own folders:
> `~/Downloads`, `~/Desktop`, or `~/Documents` (default `~/Downloads`). Pass a
> directory and a filename is derived from the title and type; existing files get
> a numeric suffix unless `overwrite` is set.

## Pinning and the dashboard

Set `pinned` to surface an artifact on your dashboard. Pinned artifacts can
declare a `dashboard_size` controlling how large the card renders:

- `small`
- `medium` (default)
- `large`
- `full`

You can pin at creation time (`create` with `pinned: true`), flip the state later
with the `pin` action, or change the size on `update`.

## Versioning

Artifacts are versioned automatically. Every artifact starts at version `1`.
Whenever an `update` changes the **content**, the previous content is snapshotted
into the version history and the version number increments. Title, tag, pin, and
size changes update the artifact in place without creating a new version.

Use `get_versions` to retrieve the history (newest first); each entry carries the
older content and the timestamp it was snapshotted.

## Where artifacts are stored

All artifacts live in a single SQLite database:

```text
~/.flowly/artifacts.sqlite
```

It holds the artifacts themselves, their version snapshots, and an FTS5 full-text
index over titles and content — which is what powers the `search` filter on
`list`. The store is purely local.

## Viewing artifacts

You have several ways to see your artifacts:

- **Terminal UI** — open the artifacts gallery with the `/artifacts` command or
  the **F4** key. It shows a pinned-aware list (a ★ marks pinned items) with a
  preview pane.
- **Desktop / web apps** — artifacts are served over a small local HTTP API the
  gateway exposes, so the desktop and web galleries can fetch and render them:
  - `GET /api/artifacts` — list (supports `type`, `pinned`, `search`, `limit`,
    `offset`, `includeInternal`)
  - `GET /api/artifacts/{id}` — a single artifact
  - `GET /api/artifacts/{id}/versions` — its version history

> [!NOTE]
> Internal context artifacts are excluded from these views by default — both the
> gallery and the list API hide them unless you explicitly include them.

## Configuration

Artifact behavior is controlled under `tools.artifact` in your Flowly config
(stored in camelCase on disk):

```json
{
  "tools": {
    "artifact": {
      "enabled": true,
      "maxContentLength": 500000
    }
  }
}
```

- `enabled` — turn the artifact tool on or off (default `true`).
- `maxContentLength` — maximum content size in bytes (default `500000`, i.e. 500KB).

## Related

- [Board — cross-channel task board](/docs/features/board)
- [Memory](/docs/features/memory)
- [Knowledge graph](/docs/features/knowledge-graph)
