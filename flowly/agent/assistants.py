"""Assistant registry — user-definable in-process subagent templates.

An Assistant is a named, specialised subagent configuration: system prompt,
model, toolset, timeout, and whether its output should be parked in the
artifact store. The three legacy "builtin" agents (writer, researcher,
coder) are seeded in code; anything under ``~/.flowly/assistants/*.md``
extends or overrides that catalog at runtime.

Why this module exists
----------------------
- The LLM's ``builtin_agent`` / ``assistant`` tool previously had a
  hardcoded enum of 3 values. Adding a fourth required a code change +
  release. This registry lets users ship a ``seo-analyst.md`` file in
  ``~/.flowly/assistants/`` and the assistant shows up in the tool
  schema on the next reload.
- Desktop UI (planned) writes these files via ``fs.writeFile``; Python
  only reads + reloads. Single source of truth (disk), minimal coupling.
- The file format mirrors the ``SKILL.md`` convention already familiar
  to power users: YAML frontmatter + markdown body.

Terminology note: "Assistant" at the code level is deliberately distinct
from the ``config.json.agents.agents`` section (which holds CLI-based
subprocess delegates — Claude Code, Codex, etc.). Desktop UI surfaces
both under a single "Agents" tab with two sections ("Your agents" vs
"CLI Agents") but the implementations share nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Assistant:
    """A registered assistant template.

    Fields mirror the frontmatter schema users write on disk, with a few
    bookkeeping extras (``builtin``, ``source_path``) the registry fills
    in. ``system_prompt`` is the markdown body verbatim — it's injected
    into the child's system prompt ``_build_subagent_prompt``.

    Wall-clock timeout is intentionally NOT on this class: it's a fixed
    system-wide value (see ``subagent._SUBAGENT_WALL_CLOCK_TIMEOUT``) so
    users don't have to think about a number most of them don't
    understand. Per-tool / per-LLM-call timeouts + max_iterations are
    the first-line hang detectors; the wall-clock is the last-resort
    sanity cap.
    """

    name: str
    description: str
    model: str
    system_prompt: str
    # None → child inherits the standard subagent allowlist. Explicit set →
    # only these tools register (``artifact`` is always appended because
    # it's the canonical output channel for auto_save_artifact=True).
    allowed_tools: frozenset[str] | None = None
    auto_save_artifact: bool = False
    artifact_type: str = "markdown"
    # If True, final_result over ``_SUBAGENT_OUTPUT_CONTEXT_LIMIT_CHARS``
    # is parked as an internal context artifact (promotable to
    # user-visible if the user asks). Opt-in: only set for assistants
    # that structurally produce long output (researcher, writer).
    # Terse specialists (coder, reviewer) keep it False so every tiny
    # response is not wrapped in a <persisted-output> envelope it does
    # not need. User-defined assistants turn it on via their
    # frontmatter: ``cap_to_artifact: true``.
    cap_to_artifact: bool = False
    # If True, ``builtin_agent`` dispatches this specialist async:
    # the parent's tool call returns immediately with a "dispatched"
    # envelope, the parent emits a brief acknowledgement to the user
    # and ends its turn, and the specialist's final result is
    # delivered as a system message that wakes the parent on a later
    # turn. Use for work measured in minutes (deep research, long
    # writing) where blocking the parent for 2-10 minutes is the
    # wrong UX. Keep False for sub-second specialists where async
    # ceremony costs more than the actual work. User assistants set
    # this in frontmatter as ``async_dispatch: true``.
    async_dispatch: bool = False
    # True if seeded from ``_BUILTIN_ASSISTANTS`` (no source file). User
    # files with the same name override builtins and set this to False.
    builtin: bool = False
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Builtin seeds — the three hardcoded personas writer/researcher/coder.
# Migrating them here (from subagent._build_subagent_prompt) means the
# registry is the single source of truth for every assistant,
# builtin or custom. User writes ``~/.flowly/assistants/writer.md`` if
# they want to override the built-in writer prompt.
# ---------------------------------------------------------------------------


_BUILTIN_ASSISTANTS: list[Assistant] = [
    Assistant(
        name="writer",
        description=(
            "reshape provided source material into essay/doc/article. "
            "Use only when the caller supplies the content; "
            "for 'research and write' tasks call `researcher` (it writes too)."
        ),
        model="openrouter/anthropic/claude-haiku-4.5",
        # auto_save_artifact was ``True`` until 2026-04-23. That
        # produced unwanted artifacts on every builtin_agent(writer)
        # call — users asking simple questions ended up with an
        # artifact in their store they never asked for. Artifact
        # creation is now opt-in: the model calls ``artifact(...)``
        # explicitly when the user wants something persisted. Output
        # cap (>10K chars) still routes to an artifact for context
        # protection — that's observable but at least justified.
        auto_save_artifact=False,
        artifact_type="markdown",
        # Writers structurally produce multi-KB markdown drafts. Park
        # anything over the 6KB context limit so the parent stays lean.
        cap_to_artifact=True,
        # Writers can take a minute on a long piece. Async avoids a
        # parent that sits pending for the whole duration.
        async_dispatch=True,
        system_prompt=(
            "## Writer\n\n"
            "You are a writing specialist. Reshape the provided source "
            "material into a clean essay / doc / article in the user's "
            "language. Use headings, sections, bullet points.\n\n"
            "Return the piece as your final assistant text — plain "
            "markdown is fine. If the user (or the calling agent) "
            "explicitly asked you to save the output, use the "
            "`artifact` tool; otherwise just write the piece inline."
        ),
        builtin=True,
    ),
    Assistant(
        name="researcher",
        description=(
            "deep research + final markdown report (self-contained). "
            "researches AND writes — produces a finished report. "
            "Do NOT chain with writer afterwards."
        ),
        model="openrouter/anthropic/claude-haiku-4.5",
        # See ``writer`` note above — opt-in artifact from 2026-04-23.
        auto_save_artifact=False,
        artifact_type="markdown",
        # Research output is the archetype of "long structured text" —
        # deep research reports routinely exceed 10-20KB. Park them so
        # the parent only carries a preview + id, and the user can
        # promote the full report when they want to keep it.
        cap_to_artifact=True,
        # Research tasks routinely take 2-10 minutes of web_search /
        # web_fetch. Blocking the parent for that long produces a
        # pending-forever UX; async dispatch lets the parent ack the
        # user immediately and deliver the report when it lands.
        async_dispatch=True,
        system_prompt=(
            "## Researcher\n\n"
            "You are a research specialist. Use web_search and web_fetch "
            "extensively. Gather material from multiple perspectives, "
            "include sources and citations, then deliver ONE "
            "self-contained markdown report as your final assistant "
            "text. Write in the user's language.\n\n"
            "Return the report inline — do NOT call the `artifact` tool "
            "unless the caller explicitly asked you to persist the "
            "output. The parent agent will deliver your text to the "
            "user as-is; no rewrite needed."
        ),
        builtin=True,
    ),
    Assistant(
        name="coder",
        description="code review, refactoring, debugging",
        model="openrouter/anthropic/claude-haiku-4.5",
        auto_save_artifact=False,  # coder may edit files in place; artifact optional
        artifact_type="code",
        system_prompt=(
            "## Coder\n\n"
            "You are a code specialist. Read existing code before making changes.\n"
            "For reviews/analysis: create an artifact (type=\"code\" or \"markdown\").\n"
            "For actual code changes: use write_file/edit_file directly.\n"
            "Follow existing code conventions. Be precise."
        ),
        builtin=True,
    ),
]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    flags=re.DOTALL,
)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_REQUIRED_FIELDS = ("name", "description", "model")


def _parse_markdown(path: Path) -> tuple[Assistant | None, str | None]:
    """Parse a ``{name}.md`` file into an Assistant.

    Returns ``(Assistant, None)`` on success or ``(None, reason)`` on
    any validation failure. The reason string is propagated into the
    reload report so operators / the desktop UI can show "these three
    files failed to load because …" instead of silently dropping them.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, f"read error: {e}"

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, "missing frontmatter (expected '---' fenced YAML block)"

    fm_raw, body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as e:
        return None, f"invalid YAML frontmatter: {e}"
    if not isinstance(fm, dict):
        return None, "frontmatter must be a YAML mapping"

    missing = [f for f in _REQUIRED_FIELDS if not fm.get(f)]
    if missing:
        return None, f"missing required field(s): {', '.join(missing)}"

    name = str(fm["name"]).strip()
    if not _NAME_RE.match(name):
        return None, f"invalid name '{name}' (lowercase letters, digits, - and _ only)"

    allowed = fm.get("allowed_tools")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed):
            return None, "allowed_tools must be a list of tool name strings"
        allowed_set: frozenset[str] | None = frozenset(allowed)
    else:
        allowed_set = None

    # `timeout_seconds` used to be a per-assistant config field, but we
    # moved the wall-clock cap to a system-wide constant so users never
    # have to pick a number. Log a hint if an old .md still has it.
    if "timeout_seconds" in fm:
        logger.debug(
            f"[AssistantRegistry] {path.name}: "
            f"'timeout_seconds' is ignored (system-wide cap is used instead)"
        )

    art_type = str(fm.get("artifact_type", "markdown")).lower()
    auto_save = bool(fm.get("auto_save_artifact", False))
    cap_to_artifact = bool(fm.get("cap_to_artifact", False))
    async_dispatch = bool(fm.get("async_dispatch", False))

    asst = Assistant(
        name=name,
        description=str(fm["description"]),
        model=str(fm["model"]),
        system_prompt=body.strip(),
        allowed_tools=allowed_set,
        auto_save_artifact=auto_save,
        artifact_type=art_type,
        cap_to_artifact=cap_to_artifact,
        async_dispatch=async_dispatch,
        builtin=False,
        source_path=path,
    )
    return asst, None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ReloadReport:
    loaded: int
    overridden: list[str] = field(default_factory=list)  # builtin names user-overrode
    errors: list[dict[str, str]] = field(default_factory=list)  # {file, reason}

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "overridden": list(self.overridden),
            "errors": list(self.errors),
        }


class AssistantRegistry:
    """Seed-plus-disk catalogue of Assistant definitions.

    Built-in assistants are loaded unconditionally at init. Custom ones
    are (re)scanned from ``assistants_dir`` on each ``reload()`` call,
    and user files override builtins with the same name (with a
    warning).

    Thread-safety: the registry is read many, written once-per-reload.
    Reload is synchronous and guarded by a simple sentinel — concurrent
    callers serialise naturally because async handlers run on one loop.
    """

    def __init__(self, assistants_dir: Path):
        self._dir = assistants_dir
        self._by_name: dict[str, Assistant] = {}
        self._last_report: ReloadReport | None = None
        self.reload()

    # ── mutation ─────────────────────────────────────────────────────────

    def reload(self) -> ReloadReport:
        """Re-scan disk and rebuild the catalogue.

        Always starts from the builtin seed, then lays user files on
        top. A badly formed file is logged + collected into the report
        but does not abort the whole reload — other files keep loading.
        """
        self._by_name = {a.name: a for a in _BUILTIN_ASSISTANTS}
        overridden: list[str] = []
        errors: list[dict[str, str]] = []

        if self._dir.exists() and self._dir.is_dir():
            for md_path in sorted(self._dir.glob("*.md")):
                asst, err = _parse_markdown(md_path)
                if err is not None or asst is None:
                    errors.append({"file": md_path.name, "reason": err or "unknown"})
                    logger.warning(
                        f"[AssistantRegistry] skipped {md_path.name}: {err}"
                    )
                    continue
                if asst.name in self._by_name and self._by_name[asst.name].builtin:
                    overridden.append(asst.name)
                    logger.warning(
                        f"[AssistantRegistry] user file {md_path.name} "
                        f"overrides builtin assistant '{asst.name}'"
                    )
                self._by_name[asst.name] = asst

        report = ReloadReport(
            loaded=len(self._by_name),
            overridden=overridden,
            errors=errors,
        )
        self._last_report = report
        logger.info(
            f"[AssistantRegistry] reload: {report.loaded} assistant(s), "
            f"{len(overridden)} override(s), {len(errors)} error(s)"
        )
        return report

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, name: str) -> Assistant | None:
        return self._by_name.get(name)

    def all(self) -> list[Assistant]:
        """Return every assistant sorted by name (stable for tool schemas)."""
        return sorted(self._by_name.values(), key=lambda a: a.name)

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def last_report(self) -> ReloadReport | None:
        return self._last_report
