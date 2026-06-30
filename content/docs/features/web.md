---
title: Web & research
eyebrow: Features
description: Flowly searches the web and extracts page content through pluggable backends — Brave, DuckDuckGo, SearXNG, Tavily, Exa, Firecrawl, Parallel — so the agent answers from live information instead of stale training data.
---

## Tools

| Tool | What it does |
| --- | --- |
| `web_search` | Search the web and get back ranked titles, URLs, and snippets. Runs through the configured **search backend** (Brave by default). |
| `web_fetch` | Read **one** URL → its readable content (HTML → markdown/text), with optional query-relevant extraction. |
| `web_extract` | Read **two or more** URLs in one call → clean content per page, through the configured **extract backend** (or local readability when none is set). |
| `x_search` | Grok-backed research/search over X/Twitter for live posts. The fuller `x` tool (post, delete, timeline, user lookup) lives in the [X integration](/docs/integrations/x). |

Web search, fetch, and extract are part of Flowly's [grounding](/docs/features/memory)
discipline: when a question is about current facts — weather, news, versions,
prices — the agent is steered to look it up rather than answer from memory.

## Pluggable backends

`web_search` and `web_extract` don't hard-code a single provider. Each backend
is a small bundled plugin (`kind: backend`); the tools dispatch every call to
whichever backend is active. Mix and match — a free search backend with a paid
extractor, or one provider for both.

| Backend | Search | Extract | Credential | Cost |
| --- | :---: | :---: | --- | --- |
| **Brave** *(default)* | ✓ | — | Your Brave API key, or the Flowly Cloud proxy automatically when logged in | Free tier / included |
| **DuckDuckGo** (`ddgs`) | ✓ | — | None | Free, no key |
| **SearXNG** | ✓ | — | Your instance URL | Free, self-hosted |
| **Tavily** | ✓ | ✓ | API key | Paid |
| **Exa** | ✓ | ✓ | API key | Paid (semantic search) |
| **Firecrawl** | ✓ | ✓ | API key, or a self-hosted URL | Paid / self-hosted |
| **Parallel** | ✓ | ✓ | API key | Paid |
| **Local readability** | — | ✓ | None | Free — the always-available `web_extract` fallback |

Search-only backends (Brave, DuckDuckGo, SearXNG) pair with any extractor. If no
paid extractor is configured, `web_extract` falls back to **local readability**,
so it always works.

## Configuring backends

Every backend appears as a card in the **Web Search** section of the connections
tab — on Desktop, iOS, and Android — and under `/integrations` in the terminal.
For each one you can:

- **enable / disable** it,
- enter its **credential** (API key or instance URL),
- mark it **"Use as default backend"** to make `web_search` / `web_extract` use it.

Everything maps to `tools.web.search` in `~/.flowly/config.json`, so you can also
edit it directly or run `flowly setup` → Tools.

```jsonc
{
  "tools": {
    "web": {
      "search": {
        // Brave (the default backend) + global selectors
        "enabled": true,
        "apiKey": "",            // your Brave key (optional — the proxy is used when logged in)
        "proxyUrl": "",          // backfilled to the Flowly Cloud proxy when logged in
        "maxResults": 5,
        "default": false,        // mark Brave as the active backend
        "backend": "",           // force a backend for BOTH capabilities
        "searchBackend": "",     // …or just search
        "extractBackend": "",    // …or just extract

        // per-backend sub-sections (also written by the cards)
        "ddgs":      { "enabled": false, "default": false },
        "searxng":   { "enabled": false, "default": false, "url": "" },
        "tavily":    { "enabled": false, "default": false, "apiKey": "" },
        "exa":       { "enabled": false, "default": false, "apiKey": "" },
        "firecrawl": { "enabled": false, "default": false, "apiKey": "", "apiUrl": "" },
        "parallel":  { "enabled": false, "default": false, "apiKey": "" }
      }
    }
  }
}
```

Keys may also come from the environment — `BRAVE_API_KEY`, `TAVILY_API_KEY`,
`EXA_API_KEY`, `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL`, `PARALLEL_API_KEY`,
`SEARXNG_URL` — which is handy for self-hosting.

### Which backend runs

For each capability (search / extract), the active backend is resolved in order:

1. **Explicit selector** — `searchBackend` / `extractBackend`, else `backend`.
2. **The card marked "Use as default backend"** (`default: true`).
3. **Availability-ordered preference**, Brave first — so an install that only has
   Brave keeps using Brave, and enabling a second backend never silently steals
   the default. Set it as default (or pick it in a selector) to switch.

A search-only backend configured as the extract backend is skipped — the next
extract-capable one is used instead.

## Optional dependencies

The keyless/REST backends (Brave, SearXNG, Tavily) need nothing extra. The SDK
backends are optional and lazy-loaded — install them with the `search` extra:

```bash
pip install "flowly-ai[search]"   # ddgs, exa-py, firecrawl-py, parallel-web
```

If a backend's package isn't installed, its card shows that and the tool returns
a clear "not installed" message rather than failing silently.

## Typical flow

A research turn chains the tools:

1. `web_search("…")` → a ranked list of results (titles, URLs, snippets) from the
   active search backend.
2. `web_fetch("<url>")` for a single promising hit, **or** `web_extract([...])` to
   pull clean content from several results at once via the active extract backend.
3. The agent summarizes or quotes from what it read.

The agent decides when to fetch or extract; you don't have to ask. Content pulled
from the web is scanned for prompt-injection before the agent reads it.

## Pitfalls

- **No backend → no search.** If nothing is configured (no Brave key, no Flowly
  Cloud login, no other enabled backend), `web_search` returns a clear "not
  available" message. `web_extract` still works via local readability.
- **`web_fetch` / `web_extract` won't hit your LAN.** They block `localhost` and
  private/internal IP ranges (SSRF protection), so they can't be pointed at
  internal services. Firecrawl re-checks the final URL after any redirect.
- **SearXNG needs your own instance.** Public instances often disable the JSON
  API or rate-limit; point `searxng.url` at an instance you run.
- **Fetch isn't a browser.** For pages that need clicking, logging in, or JS
  rendering, prefer a paid extractor (Firecrawl/Exa) via `web_extract`, or use
  [computer use](/docs/features/computer-use) or [browser tabs](/docs/features/browser).
