---
name: blogwatcher
description: "Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool."
version: 2.0.0
author: JulienTant (fork of Hyaxia/blogwatcher)
license: MIT
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"📰","tags":["rss","blogs","feed-reader","monitoring"],"requires":{"bins":["blogwatcher-cli"]},"homepage":"https://github.com/JulienTant/blogwatcher-cli","category":"monitoring","related_skills":["watchers"]}}
---

# Blogwatcher

This skill drives `blogwatcher-cli`, a small command-line feed reader. Point it at a blog homepage and it figures out where the RSS or Atom feed lives, pulls down new posts, and keeps a local record of what you have and haven't read yet. When a site has no machine-readable feed at all, you can fall back to scraping its HTML. Subscriptions can also be loaded in batches from OPML exports.

## Mental Model

Think of it as three moving parts:

1. **Sources** — the blogs you are watching. Each has a name, a homepage URL, and (once resolved) a feed URL or a scrape rule.
2. **A scan pass** — fetching each source and folding any posts it hasn't seen before into the local store.
3. **An article inbox** — every captured post, tagged read or unread, that you query and triage.

Everything lives in one SQLite file on disk, so state survives between runs and across machines if you copy that file.

## Getting the binary

Choose whichever install path fits your platform:

- **Build from source (Go):** `go install github.com/JulienTant/blogwatcher-cli/cmd/blogwatcher-cli@latest`
- **Container image:** `docker run --rm -v blogwatcher-cli:/data ghcr.io/julientant/blogwatcher-cli`
- **Prebuilt, Linux x86-64:** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_linux_amd64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Prebuilt, Linux ARM64:** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_linux_arm64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Prebuilt, macOS (M-series):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_darwin_arm64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`
- **Prebuilt, macOS (Intel):** `curl -sL https://github.com/JulienTant/blogwatcher-cli/releases/latest/download/blogwatcher-cli_darwin_amd64.tar.gz | tar xz -C /usr/local/bin blogwatcher-cli`

The complete list of downloads is at https://github.com/JulienTant/blogwatcher-cli/releases.

### Keeping data alive inside Docker

Out of the box the store is written to `~/.blogwatcher-cli/blogwatcher-cli.db`. A throwaway container wipes that on exit, so when running in Docker, redirect the database onto a volume and point `BLOGWATCHER_DB` at it:

```bash
# Option A: a Docker-managed named volume
docker run --rm -v blogwatcher-cli:/data -e BLOGWATCHER_DB=/data/blogwatcher-cli.db ghcr.io/julientant/blogwatcher-cli scan

# Option B: a directory from the host
docker run --rm -v /path/on/host:/data -e BLOGWATCHER_DB=/data/blogwatcher-cli.db ghcr.io/julientant/blogwatcher-cli scan
```

### Coming from the original project

Were you already running `Hyaxia/blogwatcher`? Carry your existing store over by renaming it into the new location:

```bash
mv ~/.blogwatcher/blogwatcher.db ~/.blogwatcher-cli/blogwatcher-cli.db
```

Note that the executable was also renamed — it is now `blogwatcher-cli` rather than the old `blogwatcher`.

## Working with the CLI

### Subscriptions

| Goal | Command |
|---|---|
| Track a new blog (feed auto-detected) | `blogwatcher-cli add "My Blog" https://example.com` |
| Track a blog, naming the feed yourself | `blogwatcher-cli add "My Blog" https://example.com --feed-url https://example.com/feed.xml` |
| Track a feedless site via scraping | `blogwatcher-cli add "My Blog" https://example.com --scrape-selector "article h2 a"` |
| Show everything you're subscribed to | `blogwatcher-cli blogs` |
| Drop a subscription | `blogwatcher-cli remove "My Blog" --yes` |
| Load many subscriptions from OPML | `blogwatcher-cli import subscriptions.opml` |

### Fetching and triage

| Goal | Command |
|---|---|
| Refresh every subscription | `blogwatcher-cli scan` |
| Refresh a single subscription | `blogwatcher-cli scan "My Blog"` |
| Show only what you haven't read | `blogwatcher-cli articles` |
| Show the full history | `blogwatcher-cli articles --all` |
| Narrow to one blog | `blogwatcher-cli articles --blog "My Blog"` |
| Narrow to one category | `blogwatcher-cli articles --category "Engineering"` |
| Flag one article as read | `blogwatcher-cli read 1` |
| Flag one article back to unread | `blogwatcher-cli unread 1` |
| Clear the whole inbox | `blogwatcher-cli read-all` |
| Clear one blog's inbox | `blogwatcher-cli read-all --blog "My Blog" --yes` |

## Configuration via environment

Any flag has an environment-variable twin under the `BLOGWATCHER_` namespace. The most useful ones:

| Variable | Effect |
|---|---|
| `BLOGWATCHER_DB` | Where the SQLite file is kept |
| `BLOGWATCHER_WORKERS` | How many feeds are fetched in parallel (defaults to 8) |
| `BLOGWATCHER_SILENT` | Suppresses per-feed scan chatter, printing only the final summary |
| `BLOGWATCHER_YES` | Auto-confirms prompts that would otherwise wait for input |
| `BLOGWATCHER_CATEGORY` | Sets a standing category filter for article listings |

## What a session looks like

Listing subscriptions:

```
$ blogwatcher-cli blogs
Tracked blogs (1):

  xkcd
    URL: https://xkcd.com
    Feed: https://xkcd.com/atom.xml
    Last scanned: 2026-04-03 10:30
```

Running a scan:

```
$ blogwatcher-cli scan
Scanning 1 blog(s)...

  xkcd
    Source: RSS | Found: 4 | New: 4

Found 4 new article(s) total!
```

Reviewing the inbox:

```
$ blogwatcher-cli articles
Unread articles (2):

  [1] [new] Barrel - Part 13
       Blog: xkcd
       URL: https://xkcd.com/3095/
       Published: 2026-04-02
       Categories: Comics, Science

  [2] [new] Volcano Fact
       Blog: xkcd
       URL: https://xkcd.com/3094/
       Published: 2026-04-01
       Categories: Comics
```

## Things worth knowing

- Feed discovery is automatic: skip `--feed-url` and the tool inspects the homepage to locate its RSS or Atom endpoint.
- A `--scrape-selector` acts as a safety net — when no feed responds, the CLI parses the page's HTML using that CSS selector instead.
- Whatever categories a feed declares get retained, which is what makes `--category` filtering possible later.
- OPML files exported from readers such as Feedly, Inoreader, or NewsBlur can be ingested wholesale via `import`.
- The default store path is `~/.blogwatcher-cli/blogwatcher-cli.db`; relocate it with the `--db` flag or the `BLOGWATCHER_DB` variable.
- Append `--help` to any subcommand (`blogwatcher-cli <command> --help`) to see its full flag set.
