# Flowly Skills

This directory contains built-in skills that extend Flowly's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

## Core skills

Every agent has these: they are synced into `~/.flowly/skills` (`sync.py`).

| Skill | Description |
|-------|-------------|
| `apple-notes` | Manage Apple Notes via the memo CLI on macOS — create, search, edit, move, export. |
| `apple-reminders` | Apple Reminders via the remindctl CLI on macOS — add, list, complete, manage lists. |
| `arxiv` | Search arXiv papers by keyword, author, category, or ID. No API key. |
| `compact` |  |
| `concept-diagrams` | Generate flat, minimal light/dark-aware SVG diagrams as standalone HTML files, using a unified educational… |
| `contract-review` | Review a contract for risk — NDA, MSA, SaaS/subscription, employment, consulting, lease, and similar agreem… |
| `data-visualization` | Choose and build the right chart for data — pick the chart type from the data shape and the message, apply… |
| `duckduckgo-search` | Free web search via DuckDuckGo — text, news, images, videos. No API key needed. Prefer the `ddgs` CLI when… |
| `excel-author` | Build auditable Excel workbooks headless with openpyxl — blue/black/green cell conventions, formulas over h… |
| `exposure-cleanup` | Consent-gated workflow for reducing a person's exposure on data brokers and people-search sites. Use when t… |
| `findmy` | Track Apple devices and AirTags via the FindMy.app on macOS using AppleScript + screenshot. |
| `flowlets` | Advanced flowlet reference: live data sources, drill-down screens, data tables, multi-series charts, agent… |
| `flowly-browser` | Browser automation playbook — Sheets/Notion/GitHub recipes + canvas-app keyboard shortcuts. ALWAYS read bef… |
| `github` | Interact with GitHub using the `gh` CLI. Use `gh issue`, `gh pr`, `gh run`, and `gh api` for issues, PRs, C… |
| `google-workspace` | Google Workspace (Drive, Gmail, Calendar, Sheets, Docs, Chat) via the `gws` CLI. Read files, send emails, m… |
| `humanizer` | Humanize text: strip AI-isms and add real voice. |
| `imessage` | Send and receive iMessage / SMS via the imsg CLI on macOS Messages.app. |
| `language-tutor` | Personal language tutor: spaced-repetition vocabulary, daily lessons, level-appropriate conversation practi… |
| `linear` | Linear: manage issues, projects, teams, documents via GraphQL + curl (or stdlib Python helper). |
| `llm-wiki` | Build/query an interlinked markdown knowledge base (Karpathy's LLM Wiki pattern). Compounding research over… |
| `macos-computer-use` | Drive macOS desktop apps through Flowly's computer tool: AX snapshots, title-based actions, typing, screens… |
| `maps` | Geocoding, POIs, routes, timezones via OpenStreetMap/OSRM. Free, no API key, Python stdlib only. |
| `mcp-usage` | Use MCP services to complete user tasks, request connections or permission review, and diagnose MCP failure… |
| `memento-flashcards` | Spaced-repetition flashcards: create from facts or text, chat-grade free-text answers, quiz from YouTube tr… |
| `memory-import` | Import saved memory/profile dumps from ChatGPT or Google Gemini into Flowly's governed memory review queue. |
| `newsletter-editor` | Produce a newsletter issue — turn a pile of links/notes into a structured issue with a compelling subject l… |
| `notion` | Notion API + ntn CLI: pages, databases (data sources), markdown, Workers. Requires NOTION_API_KEY. |
| `nutrition-analysis` | Analyze nutrition and energy balance — estimate BMR and TDEE, set calorie targets for a goal (cut/maintain/… |
| `obsidian` | Read, search, create, and edit Obsidian vault notes (Markdown + wikilinks), and turn notes into review-gate… |
| `ocr-and-documents` | Extract text from PDFs and scanned documents (pymupdf for fast text, marker-pdf for OCR/equations/forms). |
| `plan` | Plan mode: write a markdown plan to .flowly/plans/, no execution. |
| `plugin-creator` | Create a new Flowly plugin from a conversational description. Use when the user asks to "create a plugin",… |
| `pptx-author` | Build PowerPoint decks headless with python-pptx. Pairs with excel-author for model-backed decks where ever… |
| `skill-creator` | Create or update AgentSkills. Use when designing, structuring, or packaging skills with scripts, references… |
| `summarize` | Summarize or extract text/transcripts from URLs, podcasts, and local files (great fallback for “transcribe… |
| `teams` | Send messages to a Microsoft Teams channel via an Incoming Webhook. Outbound-only (Faz 1): bot can post int… |
| `watchers` | Poll RSS, JSON APIs, and GitHub with watermark dedup. |
| `weather` | Get current weather and forecasts (no API key required). |
| `youtube-content` | YouTube transcripts → structured outputs (chapters, summaries, threads, blog posts). |

## The library

The other skills ship in `flowly/skills_library/` and are not installed:
`flowly skills library` lists them, `flowly skills install <name>` adds one
(no network), and the agent can do both with `skill_manage`. An agent installed
before a skill moved there loses its unchanged copy on the next sync and keeps
a copy its owner changed.
