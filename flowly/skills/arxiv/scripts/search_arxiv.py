#!/usr/bin/env python3
"""Search arXiv and display results in a clean format.

Usage:
    python3 search_arxiv.py "GRPO reinforcement learning"
    python3 search_arxiv.py "GRPO reinforcement learning" --max 10
    python3 search_arxiv.py "GRPO reinforcement learning" --sort date
    python3 search_arxiv.py --author "Yann LeCun" --max 5
    python3 search_arxiv.py --category cs.AI --sort date --max 10
    python3 search_arxiv.py --id 2402.03300
    python3 search_arxiv.py --id 2402.03300,2401.12345
"""
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH_TOTAL = "{http://a9.com/-/spec/opensearch/1.1/}totalResults"

# Map the friendly --sort values onto the arXiv API's sortBy keywords.
SORT_KEYWORDS = {
    "relevance": "relevance",
    "date": "submittedDate",
    "updated": "lastUpdatedDate",
}


def _build_query(query, author, category, ids):
    """Assemble the query-string params for either an ID lookup or a search."""
    if ids:
        return {"id_list": ids}

    clauses = []
    if query:
        clauses.append("all:" + urllib.parse.quote(query))
    if author:
        clauses.append("au:" + urllib.parse.quote(author))
    if category:
        clauses.append("cat:" + category)

    if not clauses:
        print("Error: provide a query, --author, --category, or --id")
        sys.exit(1)

    return {"search_query": "+AND+".join(clauses)}


def _fetch(params):
    """Hit the arXiv endpoint and hand back the raw Atom payload."""
    encoded = "&".join(f"{key}={val}" for key, val in params.items())
    request = urllib.request.Request(
        f"{API}?{encoded}",
        headers={"User-Agent": "Flowly/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _split_id(raw):
    """Return (base_id, version_suffix) from a raw <id> URL."""
    tail = raw.split("/abs/")[-1] if "/abs/" in raw else raw
    base = tail.split("v")[0]
    suffix = tail[len(base):] if tail != base else ""
    return base, suffix


def _render(entry):
    """Print one Atom <entry> in the human-readable block layout."""
    title = " ".join(entry.findtext(ATOM + "title").split())
    base, suffix = _split_id(entry.findtext(ATOM + "id").strip())
    published = entry.findtext(ATOM + "published")[:10]
    updated = entry.findtext(ATOM + "updated")[:10]
    authors = ", ".join(a.findtext(ATOM + "name") for a in entry.iterfind(ATOM + "author"))
    abstract = " ".join(entry.findtext(ATOM + "summary").split())
    categories = ", ".join(c.get("term") for c in entry.iterfind(ATOM + "category"))
    clipped = abstract if len(abstract) <= 300 else abstract[:300] + "..."

    return (
        f"   ID: {base}{suffix} | Published: {published} | Updated: {updated}\n"
        f"   Authors: {authors}\n"
        f"   Categories: {categories}\n"
        f"   Abstract: {clipped}\n"
        f"   Links: https://arxiv.org/abs/{base} | https://arxiv.org/pdf/{base}"
    ), title


def search(query=None, author=None, category=None, ids=None, max_results=5, sort="relevance"):
    params = _build_query(query, author, category, ids)
    params["max_results"] = str(max_results)
    params["sortBy"] = SORT_KEYWORDS.get(sort, sort)
    params["sortOrder"] = "descending"

    tree = ET.fromstring(_fetch(params))
    entries = tree.findall(ATOM + "entry")

    if not entries:
        print("No results found.")
        return

    total = tree.find(OPENSEARCH_TOTAL)
    if total is not None:
        print(f"Found {total.text} results (showing {len(entries)})\n")

    for index, entry in enumerate(entries, start=1):
        body, title = _render(entry)
        print(f"{index}. {title}")
        print(body)
        print()


def _parse_args(argv):
    """Tiny hand-rolled flag parser mirroring the documented CLI."""
    opts = {
        "query": None,
        "author": None,
        "category": None,
        "ids": None,
        "max_results": 5,
        "sort": "relevance",
    }
    flag_targets = {
        "--author": "author",
        "--category": "category",
        "--id": "ids",
        "--sort": "sort",
    }

    words = []
    cursor = 0
    while cursor < len(argv):
        token = argv[cursor]
        following = argv[cursor + 1] if cursor + 1 < len(argv) else None
        if token == "--max" and following is not None:
            opts["max_results"] = int(following)
            cursor += 2
        elif token in flag_targets and following is not None:
            opts[flag_targets[token]] = following
            cursor += 2
        else:
            words.append(token)
            cursor += 1

    if words:
        opts["query"] = " ".join(words)
    return opts


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        sys.exit(0)

    opts = _parse_args(argv)
    search(
        query=opts["query"],
        author=opts["author"],
        category=opts["category"],
        ids=opts["ids"],
        max_results=opts["max_results"],
        sort=opts["sort"],
    )


if __name__ == "__main__":
    main()
