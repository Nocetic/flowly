"""Build the model-facing prompt behind Flowly's ``/learn`` command.

The command is intentionally thin: it turns the user's source description into
an ordinary agent turn. The live agent then inspects the named material with the
tools already available in the session and writes a reusable skill through
``skill_manage``.
"""

from __future__ import annotations


_DRY_RUN_FLAGS = ("--dry-run", "--dry_run", "-n")

_FLOWLY_SKILL_RULES = """\
Write the skill in Flowly's skill format:

- Create one directory-safe, lowercase skill name with hyphens, no spaces, and
  at most 64 characters.
- The SKILL.md file must start with YAML frontmatter containing both `name` and
  `description`. Keep `description` specific enough that Flowly can decide when
  to load the skill.
- Keep the main SKILL.md focused. Put bulky API notes, examples, schemas, or
  templates in `references/`, `scripts/`, `templates/`, or `assets/` files via
  `skill_manage(action="write_file")`.
- Prefer Flowly tool names in instructions: `read_file`, `list_dir`, `web_fetch`,
  `web_search`, `web_extract`, `exec`, `skill_view`, and `skill_manage`. Do not write shell-only
  instructions when a Flowly tool is the normal path.
- Include concrete triggers, prerequisites, the normal workflow, edge cases, and
  one verification check.
- Do not invent commands, flags, endpoints, credentials, paths, or APIs. If a
  detail is not visible in the source material, say what is known and leave the
  uncertain part as a decision point.
- Do not create a plugin. Plugins are Python packages; this command creates or
  updates markdown skills only.
"""


def parse_learn_args(raw_args: str) -> tuple[bool, str]:
    """Return ``(dry_run, source)`` for ``/learn`` arguments."""
    remaining = (raw_args or "").lstrip()
    dry_run = False

    while remaining:
        matched_flag = False
        for flag in _DRY_RUN_FLAGS:
            if remaining == flag:
                return True, ""
            if remaining.startswith(flag) and remaining[len(flag)].isspace():
                dry_run = True
                remaining = remaining[len(flag):].lstrip()
                matched_flag = True
                break
        if not matched_flag:
            break

    if dry_run and remaining.startswith("-- "):
        remaining = remaining[3:].lstrip()

    return dry_run, remaining.strip()


def build_learn_prompt(user_request: str, *, dry_run: bool = False) -> str:
    """Return the instruction submitted to the agent for ``/learn``.

    ``user_request`` is free-form: paths, URLs, pasted notes, or a reference to
    the current conversation. Empty input means "distill what we just did".
    """
    source = (user_request or "").strip()
    if not source:
        source = (
            "the reusable workflow from this conversation. Review the recent "
            "steps, tool usage, corrections, and final outcome, then preserve "
            "the parts that would help on a future similar task."
        )

    if dry_run:
        return (
            "The user invoked `/learn --dry-run`. They want Flowly to design a "
            "reusable skill from the material below, but not save or modify "
            "anything.\n\n"
            "SOURCE TO LEARN FROM:\n"
            f"{source}\n\n"
            "Work plan:\n"
            "1. Inspect the source material. Use `read_file` and `list_dir` for "
            "local files, `web_fetch` for exact URLs, `web_search` when "
            "discovery is needed, and the current conversation when the user "
            "refers to what just happened. If a path is outside the file tools' "
            "readable area, use `exec` only for read-only inspection.\n"
            "2. Decide whether this should become a new skill or an update to "
            "an existing agent-created skill. You may use "
            "`skill_manage(action=\"list\")` to check for obvious duplicates.\n"
            "3. DRY RUN MODE: do not persist anything. You MUST NOT call "
            "`skill_manage(action=\"create\")`, `skill_manage(action=\"patch\")`, "
            "`skill_manage(action=\"edit\")`, `skill_manage(action=\"write_file\")`, "
            "`skill_manage(action=\"remove_file\")`, "
            "`skill_manage(action=\"archive\")`, "
            "`skill_manage(action=\"restore\")`, or "
            "`skill_manage(action=\"delete\")`.\n"
            "4. Draft the exact skill content that would be saved. Include the "
            "proposed skill name, whether it would be created or updated, the "
            "full SKILL.md body, and any supporting files with relative paths.\n"
            "5. Include one verification check the user can run after applying "
            "the draft.\n\n"
            f"{_FLOWLY_SKILL_RULES}\n\n"
            "Final response: clearly label this as a dry run, then report the "
            "proposed skill name, create/update decision, full SKILL.md draft, "
            "supporting file drafts, and the exact follow-up `/learn` command "
            "the user can run to apply it."
        )

    return (
        "The user invoked `/learn`. They want Flowly to turn the material below "
        "into a reusable skill and save it.\n\n"
        "SOURCE TO LEARN FROM:\n"
        f"{source}\n\n"
        "Work plan:\n"
        "1. Inspect the source material. Use `read_file` and `list_dir` for "
        "local files, `web_fetch` for exact URLs, `web_search` when discovery is "
        "needed, and the current conversation when the user refers to what just "
        "happened. If a path is outside the file tools' readable area, use `exec` "
        "only for read-only inspection.\n"
        "2. Decide whether this should become a new skill or an update to an "
        "existing agent-created skill. Use `skill_manage(action=\"list\")` before "
        "creating so obvious duplicates are not created.\n"
        "3. Save the result with `skill_manage`: use `create` for a new skill, "
        "`patch` or `edit` for an existing one, and `write_file` for supporting "
        "resources. Do not write directly into `~/.flowly/skills` with shell "
        "commands.\n"
        "4. After saving, verify the written skill is usable. At minimum, check "
        "that the name, description, workflow, prerequisites, and verification "
        "section are present.\n\n"
        f"{_FLOWLY_SKILL_RULES}\n\n"
        "Final response: report the skill name, whether it was created or "
        "updated, where it was saved, and the reusable workflow it captured in "
        "one short paragraph."
    )
