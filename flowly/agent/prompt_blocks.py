"""Reusable system-prompt blocks (P3.1 + P3.2).

Three main exports, all consumed by ``context.ContextBuilder.build_system_prompt``:

  * :func:`build_platform_block` — live OS + per-OS command cheatsheet so
    the model doesn't try ``ls ~/Desktop`` on Windows or ``open -a`` on
    Linux. Flowly-specific anti-hallucination win — most agent
    frameworks don't ship a per-OS cheatsheet in the prompt.
  * :func:`build_discipline_block` — three tool-use / grounding
    blocks, XML-tagged so they survive context truncation and
    register as instruction (not narrative) to the model.
  * :func:`detect_platform` — pure helper the two above share, also
    exposed for tests and future tool-guidance additions.

The blocks are intentionally self-contained strings — no dependencies
on the agent runtime — so they're easy to snapshot-test and to reuse
in subagent prompt builders if we ever want.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


OsKind = Literal["windows", "macos", "linux", "other"]


@dataclass(frozen=True)
class PlatformInfo:
    """Snapshot of the runtime the agent is executing in.

    ``wsl`` / ``termux`` / ``docker`` aren't separate OsKinds because the
    LLM still needs Linux command guidance there — we just flag them so
    the prompt can add a one-line translation hint on top of the normal
    Linux block (e.g. ``/mnt/c/...`` on WSL).
    """

    kind: OsKind
    label: str  # human-readable, e.g. "Windows 11 (amd64)"
    shell: str  # "cmd.exe" / "zsh" / "bash" etc
    is_wsl: bool = False
    is_termux: bool = False
    is_docker: bool = False
    extras: dict[str, str] = field(default_factory=dict)


def detect_platform() -> PlatformInfo:
    """Identify the OS + shell the agent is running on.

    Called once per ``build_system_prompt`` invocation — cheap (single
    ``platform.system()`` + a few ``os.path.exists`` checks). The
    result feeds both the platform block and, via `extras`, is
    accessible for tests + future per-OS tool-guidance.
    """
    system = platform.system()
    release = platform.release()
    machine = platform.machine() or ""
    arch = machine.lower()

    is_wsl = False
    is_termux = False
    is_docker = False
    try:
        # WSL signals: kernel release has "microsoft" OR the Windows
        # drive is mounted at /mnt/c. Either one alone is enough.
        if "microsoft" in release.lower() or os.path.exists("/mnt/c/Windows"):
            is_wsl = True
    except Exception:
        pass
    try:
        is_termux = os.path.exists("/data/data/com.termux")
    except Exception:
        pass
    try:
        is_docker = os.path.exists("/.dockerenv")
    except Exception:
        pass

    if system == "Windows":
        kind: OsKind = "windows"
        label = f"Windows {release}" + (f" ({arch})" if arch else "")
        # ComSpec is set on every Windows box; %SHELL% is a cygwin-ism,
        # uncommon but honoured if present.
        shell = os.environ.get("ComSpec") or os.environ.get("SHELL") or "cmd.exe"
    elif system == "Darwin":
        kind = "macos"
        # Translate Darwin 24.x to a friendly macOS marketing name only
        # when we're confident — otherwise fall back to the kernel
        # string. Avoids shipping a stale mapping into the prompt.
        label = f"macOS (Darwin {release})" + (f" {arch}" if arch else "")
        shell = os.environ.get("SHELL", "/bin/zsh")
    elif system == "Linux":
        kind = "linux"
        label = f"Linux {release}" + (f" ({arch})" if arch else "")
        shell = os.environ.get("SHELL", "/bin/bash")
    else:
        kind = "other"
        label = f"{system} {release}" + (f" ({arch})" if arch else "")
        shell = os.environ.get("SHELL", "/bin/sh")

    return PlatformInfo(
        kind=kind,
        label=label,
        shell=shell,
        is_wsl=is_wsl,
        is_termux=is_termux,
        is_docker=is_docker,
        extras={
            "system": system,
            "release": release,
            "arch": arch,
        },
    )


# ---------------------------------------------------------------------------
# P3.1 — Platform block
# ---------------------------------------------------------------------------
#
# One block per OS, kept as constants so snapshot-tests can pin the
# exact wording. Each one is short, example-heavy, and names the
# commands the model should actually call — not general advice.
#


_PLATFORM_BLOCK_WINDOWS = """\
## Platform — Windows

You are on Windows. Use Windows-native commands, NOT Unix.

Shell + paths:
- Default shell is cmd.exe; PowerShell via `powershell -Command "..."` when needed.
- Path separator: backslash `\\` (e.g. `C:\\Users\\alice\\Desktop\\note.txt`).
- Home: `%USERPROFILE%`  (NOT `$HOME` / `~`).
- Desktop: `%USERPROFILE%\\Desktop`.
- Downloads: `%USERPROFILE%\\Downloads`.

File operations (cmd.exe):
- List files: `dir /B`  (NOT `ls`).
- Create dir: `mkdir "<path>"` (quote paths with spaces).
- Delete file: `del "<path>"`. Delete dir: `rmdir /S /Q "<path>"`.
- Copy: `copy "<src>" "<dst>"` or `xcopy` for trees.
- Find text: `findstr /S /I "<pattern>" <path>`  (NOT `grep`).

Launch apps + open files:
- Open app by name: `start "" "<app.exe>"`  (e.g. `start "" "chrome.exe"`).
- Open app via Start-menu entry: `start "" "shell:AppsFolder\\<AUMID>"`.
- Open file with its default app: `start "" "C:\\path\\to\\file.pdf"`.
- Open Explorer at a folder: `explorer.exe "C:\\Users\\alice\\Desktop"`.
- NEVER use `open -a` (that is macOS) or `xdg-open` (that is Linux).

Environment:
- Env vars in cmd: `%VAR%`. In PowerShell: `$env:VAR`.
- List env: `set` (cmd) or `Get-ChildItem Env:` (PowerShell).

If the user references a Unix-style path (`~/Desktop`), translate it
to `%USERPROFILE%\\Desktop` before running a command. Never paste a
Unix path into a Windows command."""


_PLATFORM_BLOCK_MACOS = """\
## Platform — macOS

You are on macOS. Use POSIX + Darwin-specific commands.

Shell + paths:
- Default shell is zsh (bash available). Path separator: `/`.
- Home: `$HOME` or `~`  → `/Users/<username>`.
- Desktop: `~/Desktop`.
- Downloads: `~/Downloads`.

File operations:
- List files: `ls -la` (hidden files with `-a`).
- Create dir: `mkdir -p <path>` (create parents).
- Delete file: `rm <path>`. Delete dir: `rm -rf <path>`.
- Copy: `cp -r <src> <dst>`. Move: `mv <src> <dst>`.
- Find text: `grep -r "<pattern>" <path>`.
- Find files: `find <path> -name "<glob>"`.

Launch apps + open files:
- Open app: `open -a 'Google Chrome'`  (NOT `start`, NOT `xdg-open`).
- Open app with a URL/file: `open -a 'Safari' https://example.com`.
- Open file with default app: `open ~/Documents/foo.pdf`.
- Reveal in Finder: `open -R <path>`.
- List running apps: `osascript -e 'tell application "System Events" to get name of (every process whose background only is false)'`.

Environment + package managers:
- Env vars: `$HOME`, `$USER`, `$PATH`.
- Homebrew: `brew install <pkg>`; binaries at `/opt/homebrew/bin` (Apple silicon) or `/usr/local/bin` (Intel).

If the user asks to "open X", prefer `open -a 'X'` by name before
falling back to locating the .app bundle manually."""


_PLATFORM_BLOCK_LINUX = """\
## Platform — Linux

You are on Linux. Use POSIX commands.

Shell + paths:
- Default shell is typically bash or sh. Path separator: `/`.
- Home: `$HOME` or `~`.
- Desktop: `~/Desktop` (may not exist on some distros — check with `ls ~/Desktop 2>/dev/null` before using).
- Downloads: `~/Downloads` (same caveat).
- Use `$XDG_DESKTOP_DIR` / `$XDG_DOWNLOAD_DIR` from `~/.config/user-dirs.dirs` for i18n-safe paths when available.

File operations:
- List files: `ls -la`.
- Create dir: `mkdir -p <path>`.
- Delete file: `rm <path>`. Delete dir: `rm -rf <path>`.
- Copy: `cp -r <src> <dst>`. Move: `mv <src> <dst>`.
- Find text: `grep -r "<pattern>" <path>`.
- Find files: `find <path> -name "<glob>"`.

Launch apps + open files:
- Open file/url with default app: `xdg-open <file>`  (NOT `open`, NOT `start`).
- Open a browser at a URL: `xdg-open 'https://example.com'`.
- Launch a specific app by binary name: invoke it directly (e.g. `firefox`, `code`).
- If xdg-utils is missing, fall back to invoking the binary directly and tell the user.

Environment + package managers:
- Env vars: `$HOME`, `$USER`, `$PATH`, `$DISPLAY` (for GUI).
- apt / dnf / pacman depending on distro — detect before installing.

Never assume the user has a graphical session; headless servers won't
have `xdg-open` or `$DISPLAY`."""


_PLATFORM_BLOCK_OTHER = """\
## Platform — Unknown

You are on an unidentified OS ({label}). Treat the system as POSIX-ish
and verify first — run a quick `uname -a` or equivalent before
issuing platform-specific commands."""


def _extras_hint(info: PlatformInfo) -> str:
    """One-line add-on when we detect WSL / Termux / Docker.

    Not an OS block of its own — the base Linux block still applies —
    it just points out the path translation or missing tools.
    """
    if info.is_wsl:
        return (
            "\n\nNOTE — WSL detected. The Windows host filesystem is mounted "
            "at `/mnt/c/` (C: drive), `/mnt/d/`, etc. When the user "
            "references a Windows path like `C:\\Users\\alice\\Desktop\\foo.txt`, "
            "translate it to `/mnt/c/Users/alice/Desktop/foo.txt` before acting."
        )
    if info.is_termux:
        return (
            "\n\nNOTE — Termux (Android) detected. Many POSIX tools may be "
            "missing; prefer pure-Python fallbacks via `exec` where possible."
        )
    if info.is_docker:
        return (
            "\n\nNOTE — Docker container detected. The host filesystem is not "
            "directly visible — only files mounted into the container are accessible."
        )
    return ""


def build_platform_block(info: PlatformInfo | None = None) -> str:
    """Render the per-OS command cheatsheet + live OS label.

    Consumed by ContextBuilder near the top of the system prompt so the
    model sees the right flavour of commands before it reads the tool
    schemas. Always starts with a live "You are on ..." line so the
    model can't confuse the advice with a historical context file.
    """
    info = info or detect_platform()
    header = f"_Runtime: {info.label}, shell `{info.shell}`._"
    if info.kind == "windows":
        body = _PLATFORM_BLOCK_WINDOWS
    elif info.kind == "macos":
        body = _PLATFORM_BLOCK_MACOS
    elif info.kind == "linux":
        body = _PLATFORM_BLOCK_LINUX
    else:
        body = _PLATFORM_BLOCK_OTHER.format(label=info.label)
    extras = _extras_hint(info)
    return f"{header}\n\n{body}{extras}"


# ---------------------------------------------------------------------------
# P3.2 — Tool-use discipline
# ---------------------------------------------------------------------------
#
# Kept as single triple-quoted strings. XML tags survive context
# truncation better than headings and register to the model as
# instruction rather than narrative. One place to tune the wording.
#


# Enforcement rule — bans intent announcements in any language.
# An earlier formulation said "if you say 'Let me check' you MUST
# call the tool", which read to models as permission to say the
# phrase as long as they followed through. Flowly users want
# genuinely silent tool calls, not preamble-then-call. Stated
# language-agnostically so Turkish / German / etc. preambles are
# covered without hardcoding specific phrases.
TOOL_USE_ENFORCEMENT_BLOCK = """\
# Tool-use enforcement

You MUST use your tools to take action — do not describe what you would do
or plan to do without actually doing it.

Do NOT announce your intent in ANY language before calling a tool. Any
sentence that reads as "I will now do X", "let me check", "one moment
while I look", "checking now", or the equivalent in the user's
language, is a preamble — not progress. Skip it. Just call the tool.
The tool result IS the progress; narrate AFTER the tool returns, not
before.

Never end your turn with a promise of future action — execute it now.

Keep working until the task is actually complete. Do not stop with a summary
of what you plan to do next time. If you have tools available that can
accomplish the task, use them instead of telling the user what you would do.

Every response should either (a) contain tool calls that make progress, or
(b) deliver a final result to the user. Responses that only describe
intentions without acting are not acceptable."""


# Mandatory-tool-use block — grounds answers the model tends to
# fabricate from training data. Tool names match Flowly's registry
# (exec, read_file, list_dir, web_search, web_fetch, web_extract).
MANDATORY_TOOL_USE_BLOCK = """\
<mandatory_tool_use>
NEVER answer these from memory or mental computation — ALWAYS use a tool:
- Current time / date / timezone → use `exec` (`date` on POSIX, `echo %date% %time%` on Windows)
- System state: OS, CPU, memory, disk, processes, listening ports → use `exec`
- File contents, sizes, line counts → use `read_file` or `exec` (`wc -l`, `type`, `cat`)
- Whether a file or path exists → use `list_dir` or `exec` (`ls`, `dir`, `test -e`)
- Current facts: weather, news, software versions, prices, schedules → use `web_search` (then `web_fetch` to read a single page, or `web_extract` to pull clean content from several pages / JS-heavy sites at once)
- Your own location in the filesystem → use `exec` (`pwd`, `cd`)
- Git history, branches, diffs, remotes → use `exec` (`git ...`)
- Arithmetic, hashes, encodings beyond a few digits → use `exec`

Your memory and user profile describe the USER, not the system you are
running on. The execution environment may differ from what the user profile
says about their personal setup — always trust live tool output over the
profile.
</mandatory_tool_use>"""


# Missing-context block — tells the model to look things up
# instead of guessing, and to label assumptions when it must
# proceed without full information.
MISSING_CONTEXT_BLOCK = """\
<missing_context>
- If required context is missing, do NOT guess or hallucinate an answer.
- Use the appropriate lookup tool when missing information is retrievable
  (`read_file`, `list_dir`, `web_search`, `exec`, etc).
- Ask a clarifying question only when the information cannot be retrieved by
  tools.
- If you must proceed with incomplete information, label assumptions
  explicitly ("Assuming X, …") so the user can correct you.
</missing_context>"""


# Act-don't-ask block — when a question has an obvious default
# interpretation, prefer acting to asking "what do you mean by X?".
# Kept separate from mandatory_tool_use so the model treats
# "don't answer from memory" and "don't stall on trivial
# clarification" as distinct mandates.
ACT_DONT_ASK_BLOCK = """\
<act_dont_ask>
When a question has an obvious default interpretation, act on it
immediately instead of asking for clarification. Examples:
- "What time is it?" → run `date` via `exec`, don't guess
- "Is port 443 open?" → check THIS machine, don't ask "open where?"
- "What's in my Downloads?" → `list_dir ~/Downloads`, don't confirm path

Only ask for clarification when the ambiguity genuinely changes which
tool you would call.
</act_dont_ask>"""


def build_discipline_block() -> str:
    """Assemble the four discipline sub-blocks.

    Returned as a single string with blank-line separators so the
    ContextBuilder can drop it in via a single ``parts.append(...)``.
    The quartet covers four distinct failure modes:

      1. Tool-use enforcement → "I created the file" without a tool call.
      2. Mandatory tool use   → "Today is Tuesday" fabricated from training data.
      3. Missing context      → "The file at X contains Y" without reading it.
      4. Act, don't ask       → stalling on trivial clarifications instead of
                                running the obvious tool.

    This is the SOLE canonical home for these directives. Any duplicate
    phrasing of these rules inside ``context.py`` (identity header,
    per-tool guidance) should be deleted when found — having the same
    rule in multiple places with slightly different wording made the
    model oscillate between formulations.
    """
    return "\n\n".join([
        TOOL_USE_ENFORCEMENT_BLOCK,
        MANDATORY_TOOL_USE_BLOCK,
        MISSING_CONTEXT_BLOCK,
        ACT_DONT_ASK_BLOCK,
    ])


# ---------------------------------------------------------------------------
# Agency block — the universal, principle-framed behavioural base
# ---------------------------------------------------------------------------
#
# This is the POSITIVE counterpart to ``build_discipline_block()``. It ships to
# every model in every session and gives the agent its working character:
# act over explain, finish the job, ground claims in tools, stay honest, use
# judgment. Capable models (Claude, Grok) run on this alone — they follow it
# faithfully without the explicit "never do X" enforcement, which only made
# them feel boxed-in. The strict ``build_discipline_block()`` is then layered on
# top ONLY for the model families that actually need the hammer (see
# ``model_needs_strict_discipline``). Mirrors the upstream split: a strong
# identity + a short "finish the job" guidance for everyone, tool-use
# enforcement gated to weaker families.
#
# Framed as principles with rationale, not a prohibition list — a model
# generalises from "why" far better than from a wall of "never". Keep it tight:
# it's in the cached prefix, paid once and amortised across every turn.
AGENCY_BLOCK = """\
# How you work

You are agentic. When the user asks for something, you carry it all the way to a
real result — not a description of one. Bias toward doing over explaining.

- **Act, then report.** For anything that needs an action or a live lookup, use
  the tool and let the result speak — skip the "let me check…" preamble. Answer
  conversational or explanatory questions directly, no tool needed.
- **Finish the job.** Keep working until the task is actually done: a working
  artifact backed by real tool output, not a plan or a stub. On multi-step work,
  carry every step through — don't stop after step one to ask whether to continue.
- **Ground your answers.** When a tool can give you the truth — time, files,
  system state, current facts, math — get it from the tool rather than memory.
  Trust live tool output over anything a stored profile claims.
- **Stay honest.** If a path is blocked or a tool fails, say so plainly and try
  another approach — never invent a plausible-looking result. Admit uncertainty
  when you have it; a labelled assumption ("Assuming X…") beats a confident guess.
- **Use judgment.** When a request has an obvious reading, act on it. Ask a
  clarifying question only when the ambiguity genuinely changes which tool you'd
  call — or before a destructive, high-stakes action (see safety rules).
- **Reach for the right tool.** When a first-class tool exists for an action, use
  it directly instead of a shell workaround; the tool schema wins if prose and
  schema ever disagree."""


def build_agency_block() -> str:
    """Return the universal agency block (principle-framed working character).

    Shipped to every model. Pairs with the gated ``build_discipline_block()``:
    this gives the agentic baseline, that adds the explicit enforcement for
    weaker model families."""
    return AGENCY_BLOCK


# Model families that are action-oriented enough to run on the agency block
# alone — they follow it faithfully and the strict enforcement only made them
# feel mechanical. Everything else (weaker families AND unknown/local/None,
# conservatively) gets ``build_discipline_block()`` on top. Mirrors the
# upstream gating, which exempts only its strongest provider from enforcement.
_STRONG_DISCIPLINE_EXEMPT_FAMILIES = frozenset({"claude", "xai"})


def model_needs_strict_discipline(model: str | None) -> bool:
    """True when *model* should also receive the strict discipline block.

    Capable, action-oriented families (Claude, xAI/Grok) run on the agency
    block alone → False. Weak families (OpenAI, Google, Chinese open-weight)
    and unknown/local/unresolved models default to True — better to over-steer
    an unknown model than to under-steer it into narrating or fabricating."""
    return not (detect_model_families(model) & _STRONG_DISCIPLINE_EXEMPT_FAMILIES)


# ---------------------------------------------------------------------------
# Model-family aware guidance
# ---------------------------------------------------------------------------
#
# The four ``build_discipline_block()`` sub-blocks are a baseline that
# applies to every model. On top of that, some model families have
# documented failure modes that justify a small, family-specific
# add-on, gated on the model family. Tuned for Flowly's tool surface
# and the OpenRouter model catalogue defined in
# flowly-app/lib/plans/allowlist.ts.
#
# Three rules govern this section:
#
#   1. **No duplication.** If a rule already lives in the discipline
#      block (mandatory_tool_use, missing_context, act_dont_ask), do
#      NOT repeat it here. Two slightly different phrasings of the
#      same rule make models oscillate — only NEW behaviour goes
#      into family blocks.
#
#   2. **Claude gets nothing.** Anthropic models are already action-
#      oriented and follow the discipline block faithfully. Adding
#      extras has shown no measurable improvement and burns tokens.
#      ``build_model_family_block("claude-...")`` returns an empty
#      string by design.
#
#   3. **Patterns are substrings, lowercased.** The detector tolerates
#      provider prefixes (``anthropic/claude-...``), bare names
#      (``claude-haiku-4.5``), and short aliases (``claude``) without
#      needing per-alias entries. New model families are added by
#      extending ``_MODEL_FAMILY_PATTERNS``; the model id matched by
#      one or more patterns gets the corresponding block(s).
#
# Each block is a module constant so callers can snapshot-test the
# exact wording without invoking the dispatcher.


# Extras for OpenAI GPT / Codex models. Addresses three failure modes
# the Flowly discipline block does NOT already cover:
#
#   * tool_persistence — GPT models historically stop early on empty
#     or partial results, treating "no rows" as a final answer instead
#     of trying a different query.
#   * prerequisite_checks — GPT will happily skip discovery steps and
#     feed a guessed value into a downstream call ("I'll just assume
#     the file is at /tmp/data.csv").
#   * verification — GPT will claim "Done" after a write without
#     reading it back, and will state file sizes / line counts /
#     command output without ever having run the producing tool.
#
# The three sister blocks (mandatory_tool_use, missing_context,
# act_dont_ask) are NOT included here because Flowly's discipline
# block already carries them. Repeating them would re-introduce the
# oscillation that motivated splitting the rules out in the first
# place.
OPENAI_EXECUTION_EXTRAS_BLOCK = """\
# Execution discipline (extras)

<tool_persistence>
- Use tools whenever they improve correctness, completeness, or grounding.
- Do not stop early when another tool call would materially improve
  the result.
- If a tool returns empty or partial results, retry with a different
  query, a narrower range, or a different tool before giving up.
- Keep calling tools until: (1) the task is complete, AND (2) you
  have verified the result by actually checking it (a follow-up
  `read_file`, `list_dir`, `exec`, `browser_tab` screenshot, etc.).
</tool_persistence>

<prerequisite_checks>
- Before taking an action, check whether prerequisite discovery,
  lookup, or context-gathering steps are needed.
- Do not skip prerequisite steps just because the final action seems
  obvious.
- If a task depends on output from a prior step, resolve that
  dependency first — never feed a placeholder, guessed path, or
  invented value into a tool call.
</prerequisite_checks>

<verification>
Before finalizing your response:
- Correctness: does the output satisfy every stated requirement?
- Grounding: are factual claims backed by tool outputs? If you stated
  a file size, line count, command output, URL, or any external fact,
  you should have a tool call that produced it. Do NOT claim a write
  succeeded without re-reading the file.
- Formatting: does the output match the requested format or schema?
- Safety: if the next step has side effects (file writes, shell
  commands, email send, calendar create, browser click on a confirm
  button, etc.), confirm scope before executing. The destructive-action
  list in your identity header is non-negotiable.
</verification>"""


# Operational discipline for Google Gemini / Gemma models. Addresses
# behaviours we've observed in the field and from upstream guidance:
#
#   * Relative-path bug — Gemini will produce ``../foo`` style paths
#     that depend on the shell's cwd, which then breaks under
#     ``exec`` because the cwd is workspace-relative.
#   * Sequential-tool bias — Gemini emits one tool call per turn even
#     when three independent reads could fit in one round-trip,
#     bloating latency.
#   * Paragraph ramble — Gemini's chat tuning favours long narrative
#     explanations; on Flowly's chat surfaces this reads as filler.
#   * Hung-on-prompt CLI calls — Gemini omits ``-y`` / ``--yes`` and
#     the spawned process waits forever for stdin.
#   * Plan-but-don't-execute — Gemini frequently ends with a numbered
#     plan and stops, instead of running the steps it just listed.
#
# Gemma (smaller open-weight) shares enough of these failure modes
# to justify the same block. If a future variant diverges we'll
# split the patterns.
GOOGLE_OPERATIONAL_BLOCK = """\
# Operational directives

The rules below address documented Gemini failure modes — follow
them strictly:

- **File paths.** `read_file`, `write_file`, `edit_file`, and
  `list_dir` are sandboxed to the workspace; pass paths relative to
  the workspace root for those. For files OUTSIDE the workspace
  (Desktop, Downloads, /tmp, /Users/...), use the absolute path via
  `exec` — for example `exec(command="cat /Users/alice/Desktop/foo.txt")`.
  Never feed a bare relative path like `"../foo"` to `exec`; it
  depends on the shell's working directory and produces flaky results.

- **Verify before edit.** Use `read_file`, `list_dir`, or `exec`
  (`ls`, `cat`, `wc -l`, `git status`) to check file contents and
  project structure BEFORE making changes. Never guess at what a
  file contains.

- **Dependency checks.** Before importing or invoking a library /
  CLI tool, confirm it is actually available — read `package.json`,
  `requirements.txt`, `pyproject.toml`, `Cargo.toml`, etc., or run
  `command -v <tool>` via `exec`. Do not assume.

- **Conciseness.** Keep explanatory text brief — a few sentences,
  not paragraphs. Focus on actions and results over narration. The
  user reads your final reply, not your reasoning steps.

- **Parallel tool calls.** When you need to perform multiple
  independent operations (reading three files, checking three ports,
  fetching three URLs), emit ALL the tool calls in a single response
  rather than one per turn. Sequential calls cost a full LLM
  round-trip each; parallel calls finish in one.

- **Non-interactive commands.** When invoking CLIs via `exec`, pass
  non-interactive flags (`-y`, `--yes`, `--non-interactive`,
  `--no-input`) so the command doesn't hang waiting for a prompt
  no one can answer.

- **Keep going.** Work autonomously until the task is fully
  resolved. Don't stop with a plan — execute it. If you've finished
  planning and have the tools to execute, execute now."""


# Language and output discipline for Chinese open-weight models
# (Kimi / Moonshot, Qwen, MiniMax, DeepSeek, Xiaomi MiMo, GLM / Z.AI).
# Flowly needs a dedicated block because 6 of the 13 OpenRouter
# models in the user-facing allowlist come from this family and they
# share a set of post-training drifts that the discipline block alone
# does not catch:
#
#   * Code-switching into Chinese — even when the system prompt and
#     user message are entirely English/Turkish, these models will
#     occasionally drop a 您好 / 这是 into the reply or wrap the
#     whole answer in Chinese.
#   * Policy boilerplate — heavy "本人作为AI助手" / "I should note that..."
#     preambles that the user did not ask for.
#   * Emoji decoration — sprinkling 🌟✨💡 as visual filler.
#   * Tool name translation — calling ``执行`` instead of ``exec``,
#     which the schema matcher rejects byte-by-byte.
#   * Verbose acknowledgement preambles — "Of course!", "好的，", etc.,
#     before the actual action.
#
# Brand / identifier / path discipline is also covered here so users
# in Turkish / English don't get "GitHub" rendered as "吉特哈布" or
# variable names transliterated.
CHINESE_OPENWEIGHT_LANGUAGE_BLOCK = """\
# Language and output discipline

The rules below address known failure modes when running open-weight
models with heavy Chinese pre-training. Follow them strictly:

- **Match the user's language exactly.** Whatever language the user
  wrote to you in — Turkish, English, German, French, Spanish,
  Arabic, etc. — reply in the same language. NEVER reply in Chinese
  unless the user actually wrote to you in Chinese. NEVER mix
  Chinese characters into a non-Chinese reply (no 您好, 这是, 抱歉,
  好的, 当然 inside an otherwise English or Turkish answer).

- **No content / safety disclaimers.** Do NOT prepend or append
  meta-commentary about what you can or cannot discuss, content
  policies, or generic AI-assistant apologies. The user asked a
  question; answer it directly. Avoid phrases like "I should mention
  that…", "As an AI, I cannot…", "It is important to note that…",
  and Chinese-style "本人作为AI助手…" preambles. Only state limits
  when a tool genuinely fails or a destructive action genuinely
  requires consent (see the CRITICAL SAFETY list in your identity
  header).

- **No emoji decoration.** Do not sprinkle 🌟 ✨ 💡 🎉 ✅ into replies
  as visual filler. Emojis are acceptable ONLY when they carry
  meaning the user explicitly asked for (e.g. "add an emoji to each
  bullet").

- **Tool names are literals.** Always call tools by their exact
  English schema names — `exec`, `read_file`, `write_file`,
  `list_dir`, `web_search`, `web_fetch`, `web_extract`, `browser_tab`, `cron`,
  `artifact`, `knowledge_graph`, `builtin_agent`, etc. Do NOT
  translate tool names or argument keys (no `执行`, `读取文件`,
  `网页搜索`, `定时任务`). The runtime matches on the schema name
  byte-for-byte; a translated call silently fails.

- **Brand and platform names verbatim.** When mentioning third-party
  products (Slack, Discord, Linear, Notion, GitHub, Telegram,
  Stripe, Google Drive, Trello, Home Assistant, etc.), use their
  English / Latin-alphabet names as written. Do not translate or
  transliterate them.

- **Code, identifiers, paths stay ASCII.** Variable names, function
  names, file paths, CLI flags, and environment variables stay in
  their original form regardless of reply language. Comments and
  explanations follow the user's reply language; the code itself
  does not.

- **No verbose acknowledgement preambles.** Skip "好的,", "当然,",
  "明白了,", and their English equivalents ("Sure!", "Got it!",
  "Of course!", "Absolutely!"). Get to the action or the answer.
  The user can see you understood from the fact that you actually
  did the thing."""


# Substring patterns that identify a model's family. The detector
# lowercases the input and matches against every entry; a model id
# may match multiple families in principle (we return all matches),
# though the current OpenRouter catalogue has no overlap.
#
# When adding a new model:
#   1. Pick its family. If none fits, add a new family + block.
#   2. Add a pattern that's specific enough to match the new ids and
#      generic enough to survive minor renames. Provider prefix +
#      vendor stem is usually safest (e.g. ``"anthropic/"`` covers
#      every future Claude even if Anthropic changes the version
#      suffix scheme).
#   3. If the new family needs a guidance block, add one to
#      ``build_model_family_block`` and ship it together. A pattern
#      with no block injects nothing — that's fine for families
#      where the discipline baseline is enough (xAI Grok today).
_MODEL_FAMILY_PATTERNS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "anthropic/"),
    "openai": ("gpt", "openai/", "codex"),
    "google": ("gemini", "gemma", "google/"),
    "xai":    ("grok", "x-ai/", "xai/"),
    "chinese": (
        # Z.AI / Zhipu — GLM family
        "glm", "z-ai/", "zhipu/",
        # Moonshot — Kimi family
        "kimi", "moonshot",
        # Alibaba — Qwen
        "qwen",
        # MiniMax
        "minimax",
        # DeepSeek
        "deepseek",
        # Xiaomi — MiMo
        "mimo", "xiaomi/",
    ),
}


ModelFamily = Literal["claude", "openai", "google", "xai", "chinese"]


def detect_model_families(model: str | None) -> frozenset[str]:
    """Return every family tag that matches *model*.

    A model id may, in principle, match more than one family (e.g. a
    future hybrid routed through a non-canonical provider prefix);
    we return all of them so callers can compose multiple guidance
    blocks deterministically. Empty / None input returns an empty
    frozenset — callers should treat that as "no guidance to inject"
    rather than raising, because the agent loop calls this for cron
    jobs and tests where the model may not be resolved yet.
    """
    if not model:
        return frozenset()
    lower = model.lower()
    return frozenset(
        family for family, patterns in _MODEL_FAMILY_PATTERNS.items()
        if any(p in lower for p in patterns)
    )


def build_model_family_block(model: str | None) -> str:
    """Return the family-specific guidance block(s) for *model*.

    Returns an empty string when no family matches OR when the only
    matching family has no block (currently: claude, xai). Callers
    should treat the empty string as "skip ``parts.append(...)``"
    so the system prompt doesn't carry an empty separator.

    Family-to-block mapping (also documented near each block):

      * claude          → "" (action-oriented baseline, no extras)
      * openai          → OPENAI_EXECUTION_EXTRAS_BLOCK
      * google          → GOOGLE_OPERATIONAL_BLOCK
      * xai             → "" (Grok's English+tool baseline is solid)
      * chinese         → CHINESE_OPENWEIGHT_LANGUAGE_BLOCK

    Multiple matches concatenate with blank-line separators in the
    fixed order [openai, google, chinese] so prompt caching sees a
    stable serialisation. Today nothing matches two families at
    once; the ordering matters only as defence against future
    cross-vendor routing aliases.
    """
    families = detect_model_families(model)
    blocks: list[str] = []
    if "openai" in families:
        blocks.append(OPENAI_EXECUTION_EXTRAS_BLOCK)
    if "google" in families:
        blocks.append(GOOGLE_OPERATIONAL_BLOCK)
    if "chinese" in families:
        blocks.append(CHINESE_OPENWEIGHT_LANGUAGE_BLOCK)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Channel-specific platform hints
# ---------------------------------------------------------------------------
#
# Each channel has its own rendering rules and media-delivery contract.
# Tell the agent which surface its reply is going to before it composes
# anything — otherwise it ships markdown into WhatsApp (which doesn't
# render it), drops a multi-paragraph essay into SMS (~1600 char cap),
# or forgets the email subject thread.
#
# Conventions inherited from the established platform-hint pattern:
#
#   * **MEDIA:/absolute/path** — channels that have a native attachment
#     channel intercept this token at delivery time and convert it to
#     the right native form (photo / file / voice / inline preview).
#     On channels with no attachment surface (cli, sms) the agent is
#     told NOT to emit MEDIA: tags so they don't render as literal
#     text in the user's terminal.
#
#   * **Markdown** — explicitly stated per channel because the model's
#     default (chat-tuned, markdown-heavy) is wrong on half of them.
#     WhatsApp, SMS, email render markdown as literal asterisks/hashes.
#
#   * **Length / character limits** — only stated where they actually
#     bite. SMS at ~1600 chars is the only hard one in the current
#     channel set; everything else is bounded by the response model's
#     own token budget.
#
# Voice mode (iOS push-to-talk → ``voiceMode: true`` payload) is
# SEPARATE. ``VOICE_MODE_BLOCK`` handles every aspect of TTS-friendly
# output and explicitly forbids markdown / emoji / URLs. When voice
# mode is on, the channel hint is suppressed in
# ``build_system_prompt`` to avoid two overlapping rule sets competing
# for the same behaviour.


_PLATFORM_HINT_TELEGRAM = """\
# Channel — Telegram

You are on Telegram. Markdown formatting renders natively (the
adapter converts Markdown V2 on send). Supported: **bold**, *italic*,
`inline code`, ```code blocks```, [links](url), ## headers, bullet
lists, numbered lists. Pipe-style tables do NOT render — prefer
labelled key:value pairs or compact bullet lists; any tables you
emit get rewritten into bullet form on the way out.

Media delivery: include `MEDIA:/absolute/path/to/file` anywhere in
your reply and the adapter sends it as a native attachment. Images
(.png, .jpg, .webp) become photos, .ogg becomes a voice bubble,
.mp4 plays inline, other extensions arrive as documents. Markdown
image syntax ``![alt](https://...)`` is auto-downloaded and sent as
a native photo too.

Keep replies focused — Telegram messages are read on phones; long
walls of text don't scan well. Aim for short sentences and visible
structure when the answer is multi-part."""


_PLATFORM_HINT_WHATSAPP = """\
# Channel — WhatsApp

You are on WhatsApp. WhatsApp does NOT render markdown — write in
plain text. Asterisks render as literal asterisks; backticks render
as literal backticks; headers like ``# Title`` show up as ``# Title``.
Use line breaks and short paragraphs for structure, not formatting
syntax.

Media delivery: include `MEDIA:/absolute/path/to/file` to send a
native attachment. Images (.jpg, .png, .webp) appear as photos,
videos (.mp4, .mov) play inline, other files arrive as downloadable
documents. Markdown image URLs ``![alt](url)`` are also rewritten to
native photos.

WhatsApp users expect chat-style responses — short, conversational,
no document-style sections."""


_PLATFORM_HINT_IMESSAGE = """\
# Channel — iMessage

You are on iMessage. iMessage renders NO markdown — asterisks,
backticks and ``# headers`` all show as literal characters. The
adapter flattens markdown on send, but compose in plain text from
the start: short paragraphs, line breaks for structure, no tables.

Paragraph breaks (blank lines) split your reply into separate
message bubbles — use them deliberately; one thought per bubble
reads naturally in Messages.

Media delivery: include `MEDIA:/absolute/path/to/file` to send a
native attachment (photo, video, file). Attachment delivery is
best-effort on iMessage — if it fails the user gets a short notice
with the filename instead.

You can only message people who already have a conversation with
this Mac's Messages app — you cannot start a chat with a brand-new
contact. iMessage users expect texting style: short, conversational,
no document-style sections."""


_PLATFORM_HINT_DISCORD = """\
# Channel — Discord

You are in a Discord server or DM. Discord renders Markdown
natively — **bold**, *italic*, `inline code`, ```code fences```
(with language tag for syntax highlighting), > blockquotes, and
||spoilers|| all work. Headers (``## Title``) render too.

Media delivery: include `MEDIA:/absolute/path/to/file` to send a
native attachment. Images upload as photo attachments, audio /
video / other files attach as downloads. Markdown image URLs
``![alt](url)`` also become native attachments.

Discord allows long replies — code blocks up to 2000 chars work
well — but readers still scan rather than read top-to-bottom. Lead
with the answer, put detail below."""


_PLATFORM_HINT_SLACK = """\
# Channel — Slack

You are in a Slack workspace (DM or channel). Slack uses *its own*
markdown flavour: ``*bold*`` (single asterisks), ``_italic_``
(underscores), ``` `inline code` ```, ``` ```fenced blocks``` ```,
``<url|label>`` links, ``> quote`` blockquotes. Standard Markdown
double-asterisk ``**bold**`` does NOT bold in Slack — use single
asterisks. The adapter normalises common variants but stick to
Slack mrkdwn when in doubt.

Media delivery: include `MEDIA:/absolute/path/to/file` to upload a
native attachment. Images upload as photo previews; other files as
download links. Markdown image URLs ``![alt](url)`` also upload.

Slack messages can be long but threads are preferred — when a reply
exceeds a few paragraphs, ask the user if they want the rest as a
thread reply or a doc attachment."""


_PLATFORM_HINT_EMAIL = """\
# Channel — Email

You are replying via email. Use plain text only — no Markdown,
no asterisks, no hashes, no fenced code blocks. Email clients render
some Markdown variants and silently strip others; the safest contract
is to act like every reply will be read in a plain-text viewer.

Structure with paragraphs and line breaks. Use blank lines between
sections; do not use ``### Headers`` (they show as literal ``###``).
Lists are fine as ``- item`` or ``1. item`` lines — they read
naturally even unformatted.

Media: include `MEDIA:/absolute/path/to/file` to attach a file. The
adapter attaches it natively; the subject line of the user's email
is preserved for threading.

Do NOT add greetings (``Hi <name>,``) or sign-offs (``Best, Flowly``)
unless the user asked for them. The user's own email client adds
their signature; double sign-offs read as spam to most readers.
Keep replies concise but complete — email is a written medium,
readers expect prose, not chat fragments."""


_PLATFORM_HINT_WEB = """\
# Channel — Flowly Desktop / iOS

You are in the Flowly Desktop app, the Flowly iOS app, or another
gateway-connected client. The renderer supports the full Markdown
surface — headings, bold / italic, fenced code blocks (with syntax
highlighting), tables, blockquotes, ordered + unordered lists, and
inline LaTeX / math when relevant.

Media delivery: include `MEDIA:/absolute/path/to/file` to surface a
file inline. Images render as inline previews, audio / video play
inline, PDFs and HTML render as rich previews. Local file paths
MUST be absolute. Do not use Markdown image syntax ``![alt](/path)``
for local files — only ``MEDIA:/absolute/path`` works for them.
Remote ``![alt](https://...)`` URLs render as inline images.

Long-form output: when your reply is a substantial document
(report, essay, code review), it is automatically captured as a
user-visible artifact and shows up as a preview card. Don't
manually re-paste the same content as a wall of text after the
card."""


_PLATFORM_HINT_DESKTOP = """\
# Channel — Flowly Desktop app

You are in the Flowly desktop app — the native Electron application
running on the user's computer (sessions arrive with a ``desktop:``
key; this is neither the iOS app nor a browser). The user is at
their machine, in the app's chat window or its side-chat pane.

The renderer supports the full Markdown surface — headings, bold /
italic, fenced code blocks (with syntax highlighting), tables,
blockquotes, ordered + unordered lists, and inline LaTeX / math
when relevant.

Media delivery: include `MEDIA:/absolute/path/to/file` to surface a
file inline. Images render as inline previews, audio / video play
inline, PDFs and HTML render as rich previews. Local file paths
MUST be absolute. Do not use Markdown image syntax ``![alt](/path)``
for local files — only ``MEDIA:/absolute/path`` works for them.
Remote ``![alt](https://...)`` URLs render as inline images.

Long-form output: when your reply is a substantial document
(report, essay, code review), it is automatically captured as a
user-visible artifact and shows up as a preview card. Don't
manually re-paste the same content as a wall of text after the
card."""


_PLATFORM_HINT_CLI = """\
# Channel — CLI / terminal

You are running in a terminal session. The user reads your output
directly in their shell — no rich rendering layer in between. Avoid
heavy Markdown that won't render well as text: skip multi-level
headers, avoid pipe tables (the user sees raw pipes), prefer plain
bullet lists with ``-`` over fancy formatting.

Do NOT emit ``MEDIA:/absolute/path`` tags — only messaging adapters
intercept those, so on the CLI they render as literal text. When
you produce a file (write_file, artifact export, exec output saved
somewhere), just state its absolute path in plain text; the user
can open it from their terminal.

Keep responses tight — the terminal is a streaming surface and
long replies scroll past quickly."""


_PLATFORM_HINT_CRON = """\
# Channel — Scheduled cron run

You are running as a scheduled cron job. There is NO user present —
you cannot ask questions, request clarification, or wait for a
follow-up. Execute the task fully and autonomously, making
reasonable judgement calls where the prompt leaves ambiguity.

Your final response is automatically delivered to the job's
configured destination (Telegram message, email, web UI, etc.).
Put the primary content of the answer DIRECTLY in your response —
do not preamble with "Hi, I'm Flowly running your scheduled task";
the user already knows what they scheduled.

If the task genuinely cannot be completed (auth expired, external
service down, missing data), state what went wrong in one or two
sentences and what the user can do to unblock it. Do not silently
no-op."""


PLATFORM_HINTS: dict[str, str] = {
    "telegram":  _PLATFORM_HINT_TELEGRAM,
    "whatsapp":  _PLATFORM_HINT_WHATSAPP,
    "imessage":  _PLATFORM_HINT_IMESSAGE,
    "discord":   _PLATFORM_HINT_DISCORD,
    "slack":     _PLATFORM_HINT_SLACK,
    "email":     _PLATFORM_HINT_EMAIL,
    "web":       _PLATFORM_HINT_WEB,
    "desktop":   _PLATFORM_HINT_DESKTOP,
    "cli":       _PLATFORM_HINT_CLI,
    "cron":      _PLATFORM_HINT_CRON,
}


def build_platform_hint(channel: str | None) -> str:
    """Return the channel-specific platform hint, or "" if none applies.

    Empty / unknown / None input returns "" so the caller can skip
    ``parts.append`` on a falsy result. The empty case is
    indistinguishable byte-for-byte from "no platform hint layer
    existed at all", which preserves the prompt-cache contract on
    channels we don't yet have a hint for.

    The voice-mode block (``VOICE_MODE_BLOCK``) is a separate layer
    and supersedes channel hints — when ``voiceMode: true`` is set
    on the inbound chat, callers should skip this function and use
    ``build_voice_mode_block()`` instead. They address overlapping
    concerns (output rendering, media delivery, length); shipping
    both at once produces oscillation between markdown rules.
    """
    if not channel:
        return ""
    key = channel.strip().lower()
    return PLATFORM_HINTS.get(key, "")


# ---------------------------------------------------------------------------
# Voice mode block (P4 — opt-in via voice_mode=True)
# ---------------------------------------------------------------------------
#
# Injected INSTEAD OF the Tool Call Style block when the current turn
# came in with ``voiceMode: true`` on ``chat.send``. The LLM's reply
# will be synthesised to speech (TTS) and played through the user's
# speaker, so anything that looks wrong on-screen but fine on-paper
# (markdown, emoji, bare URLs) has to be stripped. Backend also runs
# a response post-processor as defense-in-depth — see ``voice_postproc``.
#
# iOS already handles STT (via /api/stt/transcribe) and TTS (via
# /api/tts/stream + ElevenLabs) client-side; the only thing the
# backend owes the voice experience is a well-shaped text response.

VOICE_MODE_BLOCK = """\
# Voice Mode

Your response will be synthesised to speech (TTS) and played aloud.
The listener never sees the text — write EXACTLY as you would speak.

Length — this is the #1 rule:
- Answer in 1 to 3 short sentences. Typical reply: 20 to 40 words.
- Hard cap: 60 words. Never exceed it unless the user literally
  asked "tell me a story" or "read me the article".
- If a full answer needs more, give the shortest useful version and
  offer to send the rest as text, then stop.
- One idea per sentence. Short sentences sound natural spoken.

Tool calls — stay silent:
- Call tools WITHOUT any preamble in any language. No "let me
  check", no "one moment", no equivalent in the user's language.
  Just call the tool.
- After the tool returns, state the actual result in one or two
  sentences. That IS the response.
- Narrate only when the user explicitly asked you to walk them
  through what you're doing, or when a destructive action genuinely
  needs a heads-up before the call.

Strict format rules:
- NO markdown: no **bold**, no _italic_, no `#` headers, no bullet
  lists, no backtick code, no tables. Flowing sentences only.
- NO emoji. NO asterisks. NO parentheticals.
- NO bare URLs. If you need to reference a link, offer to send it
  as written text — never read `https://...` aloud.
- Numbers: say them the way a speaker of the user's language
  naturally would in conversation, not as digits or 24-hour clock.
- Dashes / colons / slashes don't speak well — use commas and
  natural pauses.

Match the user's language exactly. Whatever language the user spoke
in, reply in the same language."""


def build_voice_mode_block() -> str:
    """Return the voice-mode prompt section.

    Thin accessor so callers read through a function rather than a
    module constant — keeps the site-of-use consistent with
    ``build_platform_block`` and ``build_discipline_block``.
    """
    return VOICE_MODE_BLOCK


__all__ = [
    "OsKind",
    "PlatformInfo",
    "detect_platform",
    "build_platform_block",
    "build_discipline_block",
    "build_agency_block",
    "model_needs_strict_discipline",
    "build_voice_mode_block",
    "build_model_family_block",
    "detect_model_families",
    "ModelFamily",
    "build_platform_hint",
    "PLATFORM_HINTS",
    "TOOL_USE_ENFORCEMENT_BLOCK",
    "MANDATORY_TOOL_USE_BLOCK",
    "MISSING_CONTEXT_BLOCK",
    "ACT_DONT_ASK_BLOCK",
    "OPENAI_EXECUTION_EXTRAS_BLOCK",
    "GOOGLE_OPERATIONAL_BLOCK",
    "CHINESE_OPENWEIGHT_LANGUAGE_BLOCK",
    "VOICE_MODE_BLOCK",
]
