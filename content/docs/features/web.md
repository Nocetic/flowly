---
title: Web & research
eyebrow: Features
description: Flowly can search the web, pull a page's content, and search X/Twitter — so the agent answers from live information instead of stale training data.
---

## Tools

| Tool | What it does |
| --- | --- |
| `web_search` | Search the web via the **Brave Search API** — directly with your own key, or through the Flowly proxy. |
| `web_fetch` | Fetch a URL and return its readable content, so the agent can read a page end-to-end. |
| `x` / `x_search` | Search X/Twitter for live posts (see [X integration](/docs/integrations/x)). |

Web search and fetch are part of Flowly's [grounding](/docs/features/memory)
discipline: when a question is about current facts — weather, news, versions,
prices — the agent is steered to look it up rather than answer from memory.

## How it's keyed

`web_search` runs one of two ways, picked automatically:

- **Bring your own key:** set a Brave Search API key and the search hits Brave
  directly.
- **Flowly proxy:** with a [Flowly Cloud](/docs/using-flowly/flowly-cloud) account,
  search is routed through the hosted proxy — no separate Brave account needed.

Configure under `tools.web` in `~/.flowly/config.json`, or run `flowly setup` →
Tools and follow the prompts.

## Typical flow

A research turn usually chains the two tools:

1. `web_search("…")` → a ranked list of results with titles, URLs, and snippets.
2. `web_fetch("<url>")` on the most promising hit → the full readable page, which
   the agent then summarizes or extracts from.

The agent decides when to fetch deeper; you don't have to ask it to.

## Pitfalls

- **No key, no cloud → no search.** If neither a Brave key nor a Flowly Cloud
  account is configured, `web_search` can't run. `flowly doctor` will flag it.
- **Fetch isn't a browser.** `web_fetch` reads page content; for pages that need
  clicking, logging in, or rendering, use [computer use](/docs/features/computer-use)
  or [browser tabs](/docs/features/browser) instead.
