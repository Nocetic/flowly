---
name: llm-wiki
description: "Build/query an interlinked markdown knowledge base (Karpathy's LLM Wiki pattern). Compounding research over RAG."
homepage: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
metadata: {"flowly":{"emoji":"🗂️","tags":["wiki","knowledge-base","research","notes","markdown","rag-alternative","obsidian"],"requires":{"bins":["python3"]},"related_skills":["arxiv","summarize","writing-plans"]}}
---

# LLM Wiki

A long-lived knowledge base kept as plain markdown files that link to each other. The idea
comes from [Andrej Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
instead of re-deriving understanding from raw documents on every question, you pay the
distillation cost once and let the result accumulate.

The payoff over conventional RAG is that the expensive work — finding the connections,
spotting the disagreements, merging overlapping sources — is already baked into the pages.
A query reads digested knowledge rather than re-chewing source text. Every ingest makes the
next query cheaper and the next ingest faster.

Two roles, kept distinct:

- **You (the human)** pick what goes in and say what to dig into.
- **The agent** does the reading, distilling, linking, filing, and keeps the whole thing
  internally consistent.

## When to reach for this skill

Trigger it whenever the user is doing any of:

- Standing up a fresh wiki or knowledge base.
- Feeding a source — link, file, or pasted text — into an existing one.
- Asking a question while a wiki exists at the configured path.
- Requesting a health check, audit, or lint pass over the wiki.
- Referring to "my wiki", "my notes", or "the knowledge base" in a research conversation.

## Where the wiki lives

The path comes from the `WIKI_PATH` environment variable — typically declared in
`~/.flowly/.env`. With nothing set, fall back to `~/wiki`:

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
```

Everything is ordinary markdown on disk. There is no index server, no embedding store, no
proprietary format. Point Obsidian, VS Code, or `grep` at the folder and it just works.

## How it's organized

Three conceptual layers map onto one directory tree:

```
wiki/
├── SCHEMA.md           # the rulebook: conventions, page policy, tag list, domain
├── index.md            # catalog of every page, grouped by type, one summary line each
├── log.md              # append-only journal of actions, rolled over once a year
├── raw/                # LAYER 1 — verbatim sources, never edited
│   ├── articles/       #   clipped web pages, blog posts
│   ├── papers/         #   PDFs, arxiv preprints
│   ├── transcripts/    #   interviews, meeting notes
│   └── assets/         #   figures and images the sources reference
├── entities/           # LAYER 2 — pages for people, orgs, products, models
├── concepts/           # LAYER 2 — pages for ideas, topics, techniques
├── comparisons/        # LAYER 2 — head-to-head analyses
└── queries/            # LAYER 2 — answered questions worth keeping around
```

- **Layer 1 (`raw/`)** is read-only ground truth. The agent consults it and never rewrites it.
- **Layer 2 (the wiki pages)** is the agent's working surface — it authors, revises, and
  links these freely.
- **Layer 3 (`SCHEMA.md`)** is the contract that governs Layer 2: naming, frontmatter,
  thresholds, and the allowed tag vocabulary.

## Picking up an existing wiki (do this first, every session)

Never touch a populated wiki cold. Spend the first moves getting your bearings:

1. **Open `SCHEMA.md`** so you know the domain, the conventions, and which tags are legal.
2. **Open `index.md`** so you know what pages already exist and roughly what each says.
3. **Tail `log.md`** — the last 20-30 lines tell you what was recently done.

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
# Bearings before action
read_file "$WIKI/SCHEMA.md"
read_file "$WIKI/index.md"
read_file "$WIKI/log.md" offset=<last 30 lines>
```

Do the ingest / query / lint only after that. Skipping orientation is what produces:

- a second page for an entity that already had one,
- a new page that links to nothing because you forgot the relevant existing pages,
- edits that quietly violate the schema, and
- redoing something the log already shows as done.

Once a wiki passes ~100 pages, add one more step before creating anything: a quick
`grep`/`rg` sweep for the current topic, since the index summary may not surface a near-match.

## Standing up a new wiki

When asked to start one:

1. Resolve the path — `$WIKI_PATH`, else ask, else `~/wiki`.
2. Lay down the directory tree shown above.
3. Pin down the domain with the user. Push for specificity, not "general notes".
4. Author a `SCHEMA.md` tuned to that domain (template below).
5. Drop in a skeleton `index.md` with the section headers.
6. Drop in a `log.md` whose first entry records the creation.
7. Tell the user it's live and propose a couple of first sources to feed it.

### `SCHEMA.md` starting point

Tailor every section to the domain. This file is what keeps the agent honest and the pages
uniform:

```markdown
# Wiki Schema

## Domain
[One or two sentences naming exactly what belongs here — e.g. "frontier LLM research",
"my training log and bloodwork", "competitor product intelligence".]

## Conventions
- Filenames: all lowercase, words joined by hyphens, no spaces — `transformer-architecture.md`.
- Every page opens with the YAML frontmatter block defined below.
- Connect pages with `[[wikilinks]]`; aim for at least two outbound links on any page.
- Any edit to a page also refreshes its `updated` date.
- A new page is not done until it has a line in `index.md` under the right section.
- Every action lands as a line in `log.md`.
- **Claim provenance:** once a page weaves together three or more sources, tag the
  paragraphs that lean on one particular source with `^[raw/articles/that-source.md]` at
  the paragraph's end. A reader can then verify a single claim without re-opening every
  source. Pages built on one source can skip this — the `sources:` frontmatter already
  says where it came from.

## Frontmatter
  ```yaml
  ---
  title: Page Title
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  type: entity | concept | comparison | query | summary
  tags: [chosen from the taxonomy below]
  sources: [raw/articles/source-name.md]
  # Optional quality flags:
  confidence: high | medium | low        # strength of the evidence behind the claims
  contested: true                        # the page holds an unresolved disagreement
  contradictions: [other-page-slug]      # pages whose claims clash with this one
  ---
  ```

The `confidence` and `contested` flags are optional, but lean on them for anything
opinionated or fast-moving. Lint pulls out every `contested: true` and `confidence: low`
page so shaky claims get a second look instead of quietly calcifying into wiki canon.

### Frontmatter for `raw/` files

Source files carry a slim frontmatter block too, so a later re-ingest can tell whether the
content moved:

```yaml
---
source_url: https://example.com/article   # where it came from, when applicable
ingested: YYYY-MM-DD
sha256: <hex digest of the body that follows this frontmatter>
---
```

Hash the body only — everything below the closing `---`, not the frontmatter. On a repeat
ingest of the same URL, recompute and compare: matching hash means nothing changed, so skip
the work; a mismatch means the source drifted and the wiki should be updated.

## Tag Taxonomy
[List the 10-20 top-level tags that fit the domain. A tag must be registered here before any
page may use it.]

Illustration for an AI/ML wiki:
- Models: model, architecture, benchmark, training
- People & orgs: person, company, lab, open-source
- Methods: optimization, fine-tuning, inference, alignment, data
- Meta: comparison, timeline, controversy, prediction

Hard rule: a tag on a page must already exist in this list. Need a new one? Register it here
first, then apply it. That keeps the tag set from sprawling into noise.

## Page Thresholds
- **Make a page** when something shows up across 2+ sources, or is the heart of a single source.
- **Extend a page** when a source touches a topic you already cover.
- **Skip the page** for one-off mentions, trivia, or anything off-domain.
- **Split a page** once it crosses ~200 lines — carve it into sub-topics that cross-link.
- **Retire a page** once it's wholly outdated — move it to `_archive/` and drop it from the index.

## Entity Pages
One per noteworthy entity, covering:
- what it is at a glance,
- the key facts and dates,
- links to related entities via `[[wikilinks]]`,
- the sources behind it.

## Concept Pages
One per idea or topic, covering:
- the definition or explanation,
- where understanding currently stands,
- the open debates,
- related ideas via `[[wikilinks]]`.

## Comparison Pages
Head-to-head writeups, covering:
- what's being compared and the reason,
- the axes of comparison (a table reads best),
- a bottom line or synthesis,
- the sources.

## Update Policy
When fresh material clashes with what's on a page:
1. Look at dates first — newer usually wins over older.
2. If both genuinely stand, record each position with its date and source.
3. Note it in frontmatter as `contradictions: [page-name]`.
4. Raise it in the next lint report for the user to settle.
```

### `index.md` starting point

The index is grouped by page type; each line is just a wikilink plus a terse summary.

```markdown
# Wiki Index

> The page catalog. Every wiki page sits under its type with a one-line gist.
> Start here to find what's relevant to a question.
> Last updated: YYYY-MM-DD | Total pages: N

## Entities
<!-- keep alphabetical inside each section -->

## Concepts

## Comparisons

## Queries
```

**Growth handling:** once a section runs past 50 lines, break it into sub-groups by initial
letter or sub-domain. Once the whole index runs past 200 lines, add `_meta/topic-map.md`
that clusters pages by theme so navigation stays fast.

### `log.md` starting point

```markdown
# Wiki Log

> A running, append-only record of everything done to the wiki.
> Line format: `## [YYYY-MM-DD] action | subject`
> Valid actions: ingest, update, query, lint, create, archive, delete
> Past 500 entries, roll over: rename to log-YYYY.md and begin a clean file.

## [YYYY-MM-DD] create | Wiki initialized
- Domain: [domain]
- Laid down SCHEMA.md, index.md, log.md
```

## The three operations

### Operation 1 — Ingest

A source arrives (URL, file, or pasted text). Fold it into the wiki:

1. **Stash the raw copy.**
   - A URL → pull it with `web_fetch` as markdown, drop it in `raw/articles/`.
   - A PDF → `web_fetch` handles those too; drop it in `raw/papers/`.
   - Pasted text → save under the fitting `raw/` subfolder.
   - Give it a self-explanatory name, e.g. `raw/articles/karpathy-llm-wiki-2026.md`.
   - **Attach the raw frontmatter** (`source_url`, `ingested`, `sha256` of the body). On a
     re-ingest of the same URL, recompute the digest and compare: identical → skip; changed
     → flag the drift and update. It's cheap enough to run every time and it catches sources
     that mutate silently.

2. **Talk through the takeaways** with the user — what's notable, what bears on the domain.
   (In an automated or scheduled run, skip the chat and keep going.)

3. **See what's already on file.** Read `index.md` and grep the wiki tree for the entities
   and concepts this source names. This step is exactly what separates a compounding wiki
   from a heap of near-duplicate pages.

4. **Author or revise the pages.**
   - *New entities/concepts:* create a page only when it clears the Page Thresholds in
     `SCHEMA.md` (named by 2+ sources, or central to one).
   - *Existing pages:* fold in the new facts, correct what changed, bump `updated`. If the
     new material conflicts, run the Update Policy.
   - *Linking:* any page you create or edit must point to at least two others via
     `[[wikilinks]]`, and confirm those others link back where it makes sense.
   - *Tags:* draw only from the `SCHEMA.md` taxonomy.
   - *Provenance:* on pages stitching 3+ sources, append `^[raw/articles/source.md]` to the
     paragraphs that trace to a specific source.
   - *Confidence:* set `confidence: medium` or `low` for opinion-driven, fast-changing, or
     single-source claims. Reserve `high` for things several sources back up.

5. **Refresh the navigation.**
   - Add each new page to `index.md`, alphabetically, under its section.
   - Update the header's "Total pages" count and "Last updated" date.
   - Append `## [YYYY-MM-DD] ingest | Source Title` to `log.md`.
   - In that log entry, name every file you created or changed.

6. **Tell the user what moved** — the full list of created and updated files.

One source routinely ripples into 5-15 page edits. That's not a problem to minimize — it's
the compounding effect doing its job.

### Operation 2 — Query

A question lands that the wiki's domain should cover:

1. **Read `index.md`** to spot the pages that look relevant.
2. **On wikis of 100+ pages**, also grep every `.md` for the key terms — the index summaries
   alone can miss a buried match.
3. **Open the relevant pages** with `read_file`.
4. **Answer from the distilled knowledge**, naming the pages you drew on: "Drawing on
   [[page-a]] and [[page-b]]…".
5. **File the answer back when it's worth keeping** — a real comparison, deep dive, or new
   synthesis becomes a page in `queries/` or `comparisons/`. Don't file trivial lookups; only
   answers that would hurt to reconstruct.
6. **Log it** — note the query and whether you filed the result.

### Operation 3 — Lint

When asked to audit, health-check, or lint:

1. **Orphans** — pages no other page links to.
```python
# Whole-wiki scan, run via exec
import os, re
from collections import defaultdict
wiki = "<WIKI_PATH>"
# Walk entities/, concepts/, comparisons/, queries/ for .md files
# Pull every [[wikilink]] and tally inbound links per page
# Any page with zero inbound links is an orphan
```

2. **Dangling links** — `[[wikilinks]]` that resolve to no file.
3. **Index gaps** — every page on disk should appear in `index.md`; diff the tree against
   the index entries.
4. **Frontmatter checks** — each page must carry every required field (title, created,
   updated, type, tags, sources), and each tag must be in the taxonomy.
5. **Staleness** — pages whose `updated` lags the newest source on the same entities by
   more than 90 days.
6. **Conflicts** — pages on one topic asserting different things. Cross-check pages that
   share tags or entities, and surface everything flagged `contested: true` or with a
   `contradictions:` field.
7. **Weak evidence** — every `confidence: low` page, plus any single-source page that never
   set a confidence field; either corroborate them or knock them down to `medium`.
8. **Source drift** — for each `raw/` file carrying a `sha256:`, rehash the body and report
   mismatches. A mismatch means the raw file was edited (it shouldn't be — `raw/` is frozen)
   or the originating URL has since changed. Not fatal, but report it.
9. **Oversized pages** — flag anything past 200 lines as a split candidate.
10. **Tag drift** — enumerate tags in use, flag any missing from the `SCHEMA.md` taxonomy.
11. **Log size** — if `log.md` is over 500 entries, roll it over.
12. **Write up the findings** with concrete file paths and recommended fixes, ordered by
    severity: dangling links, then orphans, source drift, contested pages, staleness, and
    finally style nits.
13. **Log the pass** — `## [YYYY-MM-DD] lint | N issues found`.

## Day-to-day handling

### Looking things up

```bash
# Pages whose content mentions a term
grep -rli "transformer" "$WIKI" --include="*.md"

# Every page, by filename
find "$WIKI" -name "*.md" -type f

# Pages carrying a tag
grep -rl "tags:.*alignment" "$WIKI" --include="*.md"

# What happened lately
tail -n 30 "$WIKI/log.md"
```

### Ingesting several sources at once

Batch it so you don't redo work:
1. Read all the sources up front.
2. Gather every entity and concept across the whole set.
3. Do one existence-check pass for all of them, not one per source.
4. Create and update pages in a single sweep so no page gets touched twice.
5. Write `index.md` once, at the end.
6. Capture the whole batch in one log entry.

### Archiving

When a page is fully obsolete or the domain has moved on:
1. Create `_archive/` if it isn't there yet.
2. Move the page into `_archive/` keeping its relative path — `_archive/entities/old-page.md`.
3. Take it out of `index.md`.
4. Fix anything that linked to it: swap the wikilink for plain text plus "(archived)".
5. Log the archive.

### Using it as an Obsidian vault

The folder is a working Obsidian vault as-is:
- `[[wikilinks]]` become clickable.
- Graph View draws the link network.
- The YAML frontmatter feeds Dataview.
- Images live in `raw/assets/` and embed with `![[image.png]]`.

To get the most out of it:
- Point Obsidian's attachment folder at `raw/assets/`.
- Keep "Wikilinks" enabled (it usually is by default).
- Add the Dataview plugin for queries such as
  `TABLE tags FROM "entities" WHERE contains(tags, "company")`.

### Obsidian on a headless box

No display? Use `obsidian-headless` rather than the desktop app. It rides Obsidian Sync
without any GUI, so an agent on a server can keep writing to the wiki while you read the
same vault from another device.

```bash
# Needs Node.js 22+
npm install -g obsidian-headless

# Sign in (needs an Obsidian account with a Sync subscription)
ob login --email <email> --password '<password>'

# Make a remote vault for this wiki
ob sync-create-remote --name "LLM Wiki"

# Bind the wiki folder to that vault
cd ~/wiki
ob sync-setup --vault "<vault-id>"

# First sync
ob sync

# Keep syncing (runs in the foreground — wrap it in launchd/systemd for the background)
ob sync --continuous
```

Now the agent can write `~/wiki` on the server while the same vault stays live in Obsidian
on your laptop or phone, with edits showing up within seconds.

## Things that bite

- **`raw/` is sacred** — never edit a source. Fixes belong on wiki pages, not in the original.
- **Bearings before anything** — `SCHEMA.md`, `index.md`, recent `log.md`, then act. Skip it
  and you'll spawn duplicates and orphans.
- **Keep `index.md` and `log.md` current** — they are the navigation spine; let them rot and
  the whole wiki rots with them.
- **Honor the thresholds** — a name in a single footnote does not earn its own page.
- **No page without links** — a page that links to nothing is effectively invisible; give
  every one at least two outbound `[[wikilinks]]`.
- **Frontmatter is mandatory** — it's what makes search, filtering, and staleness checks work.
- **Tags come from the taxonomy** — freeform tags rot into noise; register a new tag in
  `SCHEMA.md` before you use it.
- **Keep pages skimmable** — a page should be graspable in half a minute; split past 200
  lines and push the long analysis into its own deep-dive page.
- **Check before a mass edit** — if an ingest will hit 10+ existing pages, get the user to
  confirm the scope first.
- **Roll the log** — past 500 entries, rename `log.md` to `log-YYYY.md` and start clean; the
  lint pass is the natural place to notice this.
- **Surface contradictions, don't bury them** — never silently overwrite; keep both claims
  with dates, mark them in frontmatter, and flag them for the user.
