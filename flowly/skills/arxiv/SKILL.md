---
name: arxiv
description: "Search arXiv papers by keyword, author, category, or ID. No API key."
homepage: https://info.arxiv.org/help/api/index.html
metadata: {"flowly":{"emoji":"📚","tags":["research","arxiv","papers","academic","science","api"],"requires":{"bins":["curl","python3"]},"related_skills":["summarize","llm-wiki"]}}
---

# arXiv Research

Browse and pull down scholarly preprints from arXiv through its open REST endpoint. The service is free and unauthenticated, so everything here works with nothing more than `curl` and the Python standard library.

## At a Glance

| Goal | How |
|------|-----|
| Keyword search | `curl "https://export.arxiv.org/api/query?search_query=all:QUERY&max_results=5"` |
| Look up one paper | `curl "https://export.arxiv.org/api/query?id_list=2402.03300"` |
| Skim the abstract | `web_fetch(urls=["https://arxiv.org/abs/2402.03300"])` |
| Pull the full PDF | `web_fetch(urls=["https://arxiv.org/pdf/2402.03300"])` |

The fastest path is usually the bundled `scripts/search_arxiv.py` helper (see below), which hides all the XML wrangling. Drop to raw `curl` only when you need a parameter the helper doesn't expose.

## Talking to the API directly

Responses come back as Atom XML. You can eyeball it raw, but for anything readable you'll want to run it through a small Python filter.

A bare search:

```bash
curl -s "https://export.arxiv.org/api/query?search_query=all:GRPO+reinforcement+learning&max_results=5"
```

The same search, decoded into something a human can scan, newest first:

```bash
curl -s "https://export.arxiv.org/api/query?search_query=all:GRPO+reinforcement+learning&max_results=5&sortBy=submittedDate&sortOrder=descending" | python3 -c '
import sys, xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
tree = ET.parse(sys.stdin)
for n, item in enumerate(tree.iterfind(ATOM + "entry"), start=1):
    name = " ".join(item.findtext(ATOM + "title").split())
    ident = item.findtext(ATOM + "id").rsplit("/abs/", 1)[-1]
    date = item.findtext(ATOM + "published")[:10]
    who = ", ".join(a.findtext(ATOM + "name") for a in item.iterfind(ATOM + "author"))
    blurb = " ".join(item.findtext(ATOM + "summary").split())[:200]
    fields = ", ".join(c.get("term") for c in item.iterfind(ATOM + "category"))
    print(f"{n}. [{ident}] {name}")
    print(f"   By: {who}")
    print(f"   {date} | {fields}")
    print(f"   {blurb}...")
    print(f"   https://arxiv.org/pdf/{ident}\n")
'
```

## Building Queries

Each search term can carry a field prefix. Stack several with `+`.

| Prefix | Field | Sample |
|--------|-------|--------|
| `all:` | everything | `all:transformer+attention` |
| `ti:` | title only | `ti:large+language+models` |
| `au:` | author | `au:vaswani` |
| `abs:` | abstract | `abs:reinforcement+learning` |
| `cat:` | category | `cat:cs.AI` |
| `co:` | comments | `co:accepted+NeurIPS` |

Logical combinations:

```
all:transformer+attention            # implicit AND between terms joined by +
all:GPT+OR+all:BERT                   # either term
all:language+model+ANDNOT+all:vision  # exclude
ti:"chain+of+thought"                 # quoted phrase
au:hinton+AND+cat:cs.LG               # author AND category
```

## Ordering and Paging

| Knob | Accepts |
|------|---------|
| `sortBy` | `relevance`, `lastUpdatedDate`, `submittedDate` |
| `sortOrder` | `ascending`, `descending` |
| `start` | zero-based offset into the result set |
| `max_results` | how many to return (defaults to 10, ceiling 30000) |

```bash
# Ten most recent cs.AI submissions
curl -s "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10"
```

## Looking Up Known Papers

```bash
# Single ID
curl -s "https://export.arxiv.org/api/query?id_list=2402.03300"

# A batch, comma separated
curl -s "https://export.arxiv.org/api/query?id_list=2402.03300,2401.12345,2403.00001"
```

## Producing a BibTeX Entry

Fetch the metadata, then fold it into a citation:

```bash
curl -s "https://export.arxiv.org/api/query?id_list=1706.03762" | python3 -c '
import sys, xml.etree.ElementTree as ET

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
item = ET.parse(sys.stdin).getroot().find(ATOM + "entry")
if item is None:
    sys.exit("No such paper")

name = " ".join(item.findtext(ATOM + "title").split())
people = " and ".join(a.findtext(ATOM + "name") for a in item.iterfind(ATOM + "author"))
yr = item.findtext(ATOM + "published")[:4]
ident = item.findtext(ATOM + "id").rsplit("/abs/", 1)[-1]
pc = item.find(ARXIV + "primary_category")
klass = pc.get("term") if pc is not None else "cs.LG"
surname = item.find(ATOM + "author").findtext(ATOM + "name").split()[-1]

key = f"{surname}{yr}_{ident.replace(\".\", \"\")}"
lines = [
    f"@article{{{key},",
    f"  title     = {{{name}}},",
    f"  author    = {{{people}}},",
    f"  year      = {{{yr}}},",
    f"  eprint    = {{{ident}}},",
    "  archivePrefix = {arXiv},",
    f"  primaryClass  = {{{klass}}},",
    f"  url       = {{https://arxiv.org/abs/{ident}}}",
    "}",
]
print("\n".join(lines))
'
```

## Actually Reading the Paper

Once you have an ID, hand it to `web_fetch`:

```
# Landing page — metadata plus the abstract, quick
web_fetch(urls=["https://arxiv.org/abs/2402.03300"])

# The whole thing as a PDF
web_fetch(urls=["https://arxiv.org/pdf/2402.03300"])
```

## Category Cheatsheet

| Code | Subject |
|------|---------|
| `cs.AI` | Artificial Intelligence |
| `cs.CL` | Computation and Language (NLP) |
| `cs.CV` | Computer Vision |
| `cs.LG` | Machine Learning |
| `cs.CR` | Cryptography and Security |
| `stat.ML` | Machine Learning (Statistics) |
| `math.OC` | Optimization and Control |
| `physics.comp-ph` | Computational Physics |

The complete taxonomy lives at https://arxiv.org/category_taxonomy.

## The Helper Script

`scripts/search_arxiv.py` does the XML parsing and prints tidy results. Standard library only, nothing to install.

```bash
python3 scripts/search_arxiv.py "GRPO reinforcement learning"
python3 scripts/search_arxiv.py "transformer attention" --max 10 --sort date
python3 scripts/search_arxiv.py --author "Yann LeCun" --max 5
python3 scripts/search_arxiv.py --category cs.AI --sort date
python3 scripts/search_arxiv.py --id 2402.03300
python3 scripts/search_arxiv.py --id 2402.03300,2401.12345
```

---

## Going Beyond arXiv: Semantic Scholar

arXiv exposes no citation graph and no "papers like this" feature. When you need who-cited-whom data, reference lists, or recommendations, reach for **Semantic Scholar** instead. It speaks JSON, and basic access is free and key-free at roughly one call per second.

### Paper metadata, including citation counts

```bash
# Looked up by its arXiv identifier
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300?fields=title,authors,citationCount,referenceCount,influentialCitationCount,year,abstract" | python3 -m json.tool

# Or by an S2 paper id / DOI
curl -s "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1234/example?fields=title,citationCount"
```

### Papers that cite this one

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300/citations?fields=title,authors,year,citationCount&limit=10" | python3 -m json.tool
```

### Papers this one cites

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300/references?fields=title,authors,year,citationCount&limit=10" | python3 -m json.tool
```

### A JSON-native alternative to arXiv search

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/search?query=GRPO+reinforcement+learning&limit=5&fields=title,authors,year,citationCount,externalIds" | python3 -m json.tool
```

### Ask for similar papers

```bash
curl -s -X POST "https://api.semanticscholar.org/recommendations/v1/papers/" \
  -H "Content-Type: application/json" \
  -d '{"positivePaperIds": ["arXiv:2402.03300"], "negativePaperIds": []}' | python3 -m json.tool
```

### Author lookup

```bash
curl -s "https://api.semanticscholar.org/graph/v1/author/search?query=Yann+LeCun&fields=name,hIndex,citationCount,paperCount" | python3 -m json.tool
```

### Fields worth requesting

`title`, `authors`, `year`, `abstract`, `citationCount`, `referenceCount`, `influentialCitationCount`, `isOpenAccess`, `openAccessPdf`, `fieldsOfStudy`, `publicationVenue`, and `externalIds` (which carries the arXiv ID, DOI, and so on).

---

## A Typical Research Loop

1. Cast a wide net: `python3 scripts/search_arxiv.py "your topic" --sort date --max 10`
2. Gauge how influential a hit is via its citation counts on Semantic Scholar.
3. Read the abstract page with `web_fetch`.
4. If it's promising, fetch the full PDF the same way.
5. Walk its reference list (Semantic Scholar `/references`) to find foundational work.
6. POST to the recommendations endpoint for adjacent papers you might have missed.
7. Follow the authors using the Semantic Scholar author search.

## Throttling

| Service | Suggested pace | Credentials |
|---------|----------------|-------------|
| arXiv | about one request every three seconds | none |
| Semantic Scholar | one request per second | none (a key raises this to ~100/sec) |

## Things to Keep in Mind

- arXiv answers in Atom XML; lean on the helper script or one of the inline parsers above rather than reading it raw.
- Semantic Scholar answers in JSON; `python3 -m json.tool` makes it legible.
- Two ID styles coexist: the legacy `hep-th/0601001` form and the modern `2402.03300` form.
- URL shapes: abstract at `https://arxiv.org/abs/{id}`, PDF at `https://arxiv.org/pdf/{id}`, and where it exists an HTML render at `https://arxiv.org/html/{id}`.

## A Note on Versions

- A bare ID like `arxiv.org/abs/1706.03762` always points at whatever the current revision is.
- Appending a version suffix (`...1706.03762v1`) pins you to that exact, frozen revision.
- The API's `<id>` element hands back the versioned URL, e.g. `http://arxiv.org/abs/1706.03762v7`.
- When you cite, carry through the version you actually read. Later revisions can change the content materially, so omitting the suffix invites citation drift.

## Watch for Withdrawals

Authors sometimes pull a submission. Tell-tale signs:

- The `<summary>` text reads like a notice — scan it for words such as "withdrawn" or "retracted".
- Other metadata fields may come back sparse or empty.
- Treat the summary as a gate: confirm it describes a real paper before acting on the result.
