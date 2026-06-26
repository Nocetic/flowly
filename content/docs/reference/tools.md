---
title: Tools
eyebrow: Reference
description: The built-in tools the Flowly agent can call. Many register only when their integration is configured.
---

Tools are the functions the agent calls to act on the world — reading files, running commands, searching the web, controlling the desktop, posting to channels, and more. Core tools are always on; integration tools register only when their credentials/config are present. Configure them in a session with `/permissions` (execution policy), `/integrations`, and `/channels`.

> [!NOTE]
> Many tools below only appear when enabled. For example, the Google tools register when `channels.email.enabled` is true, and `linear` registers when `integrations.linear.apiKey` is set.

## Files & memory

| Tool | What it does |
|---|---|
| `read_file` | Read a file (workspace + Downloads/Desktop/Documents + Flowly home; secrets denied). |
| `write_file` | Write content to a file. |
| `edit_file` | Exact find/replace in a file. |
| `list_dir` | List a directory. |
| `memory_append` | Append a durable note to `MEMORY.md` (de-duplicated). |
| `memory_search` | Search the memory index (keyword + vector). |
| `memory_recall` | Recall the most relevant governed memories for the current context. |
| `memory_get` | Fetch a specific memory entry by id. |
| `knowledge_graph` | Add/query the temporal knowledge graph (add/query/invalidate/search/timeline/merge/stats). |
| `session_search` | Search across past sessions. |

## Shell & system

| Tool | What it does | Gated by |
|---|---|---|
| `exec` | Run a shell command (allowlist + approvals + sandbox). | `tools.exec.enabled` |
| `process` | Manage long-running background processes. | — |
| `docker` | Manage Docker containers. | — |
| `system` | System resource / process monitor (cpu/memory/disk/network/processes/ports). | — |
| `codex_session` | Delegate a coding turn to the Codex app-server. | `tools.codexSession.enabled` |

## Web & research

| Tool | What it does | Gated by |
|---|---|---|
| `web_search` | Web search (Brave), returns titles/URLs/snippets. | `BRAVE_API_KEY` or Flowly proxy |
| `web_fetch` | Fetch a URL → markdown/text with query-relevant extraction. | — |
| `x_search` | Grok-backed research over X/Twitter. | xAI OAuth or `XAI_API_KEY` |

## Browser & desktop

| Tool | What it does | Gated by |
|---|---|---|
| `browser_tab` | Drive your real Chrome via the Flowly extension. | `tools.browserTab.enabled` + extension |
| `browser_plan` | Explicit plan + evidence layer for browser tasks. | registers with `browser_tab` |
| `computer` | macOS desktop control (mouse / keyboard / Accessibility tree). | `tools.computer.enabled`, macOS |
| `screenshot` | Capture a display to a file. | — |

## Media

| Tool | What it does | Gated by |
|---|---|---|
| `image_generate` | Text-to-image via FAL; the result rides the assistant's reply. | `tools.imageGeneration.enabled` + key |
| `video_analyze` | Analyze a video via a multimodal model. | — |
| `artifact` | Create/manage renderable artifacts (HTML/SVG/Markdown/form/chart/code). | artifact store |
| `voice_call` | Place / manage a voice call. | `integrations.voice.enabled` |

See [Image generation](../features/image-generation.md) for the full image_generate guide (models, sizes, delivery).

## Channels & automation

| Tool | What it does | Gated by |
|---|---|---|
| `message` | Send a message to a channel (Telegram / WhatsApp / …). | gateway bus |
| `cron` | Schedule a prompt to run later. | — |
| `board_add` / `board_list` / `board_get` / `board_update` | Capture and manage cards on the [task board](/docs/features/board). | — |
| `board_run` | Run a card, or split a goal into parallel sub-cards. | — |
| `delegate_to` | Hand a task to a CLI-subprocess agent or team. | `flowly setup agents` |
| `spawn` / `builtin_agent` | Run an in-process subagent (writer / researcher / coder). | — |
| `skill_view` / `skill_manage` | Read / author skills. | — |

## Integrations

| Tool | What it does | Gated by |
|---|---|---|
| `email` | Gmail read/send/reply/search. | `channels.email.enabled` + `gmail.json` |
| `google_calendar` | Calendar list/get/create/update/delete/search. | `channels.email.enabled` + `gmail.json` |
| `google_contacts` | Contacts search/list (read-only). | same |
| `google_drive` | Drive list/search/read/info/create. | same |
| `google_tasks` | Tasks lists/tasks/create/complete/delete. | same |
| `linear` | Linear issues/projects/teams/comments. | `integrations.linear.apiKey` |
| `trello` | Trello boards/lists/cards. | `integrations.trello.{apiKey,token}` |
| `x` | Post/delete/search/timeline on X/Twitter. | `integrations.x.*` |
| `obsidian_search` / `obsidian_read` / `obsidian_list` / `obsidian_write` / `obsidian_append` | Search, read, list, write, and append notes in your Obsidian vault. | `integrations.obsidian.enabled` |
| `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service` | Home Assistant control. | `integrations.homeAssistant.{url,token}` |

## Related

- [Slash commands](slash-commands.md) — `/permissions`, `/integrations`
- [Sandbox & approvals](../using-flowly/sandbox-and-approvals.md)
- [MCP](../features/mcp.md) — add external tools
- [Integrations](../integrations/linear.md)
