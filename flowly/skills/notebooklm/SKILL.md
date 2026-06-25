---
name: notebooklm
description: "Drive Google NotebookLM from the shell: create notebooks, add sources, ask grounded questions, and generate audio overviews, study guides, quizzes, mind maps and more."
homepage: https://github.com/teng-lin/notebooklm-py
metadata: {"flowly":{"emoji":"📓","tags":["notebooklm","research","audio-overview","podcast","google","study-guide","sources"],"requires":{"bins":["notebooklm"]},"install":[{"id":"pipx","kind":"pipx","package":"notebooklm-py[browser]","bins":["notebooklm"],"label":"Install notebooklm-py (pipx)"},{"id":"uv","kind":"uv-tool","package":"notebooklm-py[browser]","bins":["notebooklm"],"label":"Install notebooklm-py (uv tool)"}],"related_skills":["research","obsidian","lab-notebook","summarize"]}}
---

# Google NotebookLM

This skill drives the `notebooklm` command-line tool so the agent can use Google
NotebookLM as a source-grounded research workspace: add documents/links, ask
questions answered *only* from those sources, and generate studio artifacts
(audio overviews / "podcasts", study guides, quizzes, mind maps, slide decks).

All work happens under the **user's own Google account** — notebooks, sources
and generated artifacts live in their NotebookLM, exactly as if they used the
website. Treat it as live, personal data.

## Important: unofficial tool

`notebooklm` is the community `notebooklm-py` CLI. Google has **no public
consumer API**, so it drives NotebookLM through the user's authenticated
browser session and undocumented endpoints. Consequences to be honest about:

- It can **break without warning** when Google changes the web app — that is the
  tool's problem, not Flowly's; report the failure and stop, don't improvise.
- It is **not affiliated with Google** and automating a consumer product touches
  Google's Terms of Service. The user runs it with their own account at their
  own discretion — never use it on an account that isn't the operator's.
- **Rate limits apply**; heavy batch use may be throttled.

## Setup checklist

Before any command, confirm the environment is ready:

1. The `notebooklm` binary is on PATH. Install once with either:
   - `uv tool install "notebooklm-py[browser]"`  (recommended), or
   - `pipx install "notebooklm-py[browser]"`
   The `[browser]` extra pulls Playwright; the first login downloads Chromium
   (~170 MB).
2. The user is signed in: `notebooklm login` opens a browser for Google
   sign-in. Reusing an existing Chrome session also works:
   `notebooklm login --browser-cookies chrome`.
3. Verify auth before doing real work: `notebooklm auth check --test --json`.
   If it reports not-authenticated, stop and ask the user to run
   `notebooklm login` — do not attempt to log in on their behalf with guessed
   credentials.

## Pick this skill when

- The user wants a **source-grounded** answer — "based on these PDFs/links,
  what…" — rather than the model's open knowledge.
- They want a NotebookLM **audio overview / podcast**, study guide, quiz,
  briefing, mind map or slide deck built from specific sources.
- They are curating a research notebook over time (adding sources, querying it).

## Reach for something else when

- The note is just the agent's own bookkeeping → use `memory_append`.
- They want their personal Markdown notes (not Google's notebooks) → use the
  `obsidian` skill.
- A quick web lookup with no notebook needed → use web search / fetch tools.

## Command cookbook

Run everything through the `exec` tool. Long-running generation supports
`--wait` to block until the artifact is ready; without it, poll separately.

**Auth & account**

```bash
notebooklm login                       # interactive Google sign-in
notebooklm auth check --test --json    # verify session
notebooklm profile list                # list signed-in Google accounts
notebooklm profile switch <name>       # change active account
```

**Notebooks**

```bash
notebooklm create "My Research"        # create a notebook (prints its id)
notebooklm use <notebook_id>           # select the active notebook
notebooklm metadata --json             # dump notebook + sources as JSON
```

**Sources** (PDF, text, Markdown, Word, EPUB, URLs, audio/video/images)

```bash
notebooklm source add "https://example.com/article"
notebooklm source add "./paper.pdf"
notebooklm source add-research "large language models"   # auto-research + import
```

**Ask (grounded chat)**

```bash
notebooklm ask "What are the key themes across these sources?"
notebooklm ask --prompt-file ./long_question.txt
```

**Generate studio artifacts** (use `--wait` so the file is ready when it returns)

```bash
notebooklm generate audio "keep it conversational" --wait    # audio overview / podcast
notebooklm generate video --style whiteboard --wait
notebooklm generate quiz --difficulty hard
notebooklm generate flashcards
notebooklm generate mind-map
notebooklm generate slide-deck
# report templates: briefing, study guide, blog post, or a custom prompt
```

**Download artifacts**

```bash
notebooklm download audio ./overview.mp3
notebooklm download quiz --format markdown ./quiz.md
notebooklm download mind-map ./mindmap.json
```

`notebooklm language list` shows the supported output languages (50+).

## Operating rules

1. Always confirm auth (`auth check`) before a multi-step task; surface a clear
   "please run `notebooklm login`" message rather than failing cryptically.
2. Adding sources and generating artifacts writes to the user's real NotebookLM
   and consumes their quota — for bulk or destructive actions (deleting
   notebooks/sources) get explicit confirmation first.
3. Prefer `--wait` for generation so you can hand the user a finished file;
   otherwise tell them it's still rendering and poll before downloading.
4. When the tool errors due to a Google change, report the actual error and
   stop — do not retry blindly or fabricate results. NotebookLM answers are only
   trustworthy when they come from the tool, grounded in the user's sources.
5. Cite which notebook/sources an answer came from when relaying it.
