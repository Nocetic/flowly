"""Validate the plugin-creator skill's templates produce loadable plugins.

The skill's payoff is teaching the agent to write WORKING plugins.
That's hard to test end-to-end (requires an LLM), but we can verify
the static fixtures inside SKILL.md:

  1. Every Python code block parses after placeholder substitution
  2. Every YAML manifest block parses after placeholder substitution
  3. The skill is discoverable by SkillsLoader and surfaces as
     ``available`` in the system-prompt summary

If any of these break, the agent will write broken plugins from the
templates and fail downstream.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml


SKILL_PATH = Path(__file__).parent.parent / "flowly" / "skills" / "plugin-creator" / "SKILL.md"


# Placeholder substitutions that turn the templates into valid code.
# Keep in sync with the angle-bracket placeholders inside SKILL.md.
PYTHON_PLACEHOLDERS: dict[str, str] = {
    "<one-line description>": "Test plugin",
    "<your text here, can be multiline markdown>": "Hello",
    "<command-name>": "ping",
    "<command_name>": "ping",
    "<what it does>": "Does a thing",
    "<ENV_VAR>": "TEST_API_KEY",
    "<base_url>": "https://api.example.com",
    "<tool_name>": "test_search",
    "<what to search>": "search query",
    "<one-line description for the agent>": "Search test",
    "<hardcoded_url_or_placeholder>": "https://example.com/webhook",
    "<input_hint>": "args",
    "<tool_to_guard>": "exec",
    "<param_to_inspect>": "command",
    "<plugin>": "test_plugin",
    "<one paragraph orienting the agent: who the user is, what tools are relevant, when to prefer them over generic tools>": "Test context",
    "<skill-name>": "test-skill",
    "<skill_name>": "test_skill",
    "<when to load this>": "Test description",
    "<plugin_name>": "test-plugin",
    "<plugin_dir>": "test-plugin",
}

YAML_PLACEHOLDERS: dict[str, str] = {
    "<slug>": "test-plugin",
    "<description>": "Test description",
    "<command-name>": "ping",
    "<tool_name>": "test_search",
    "<event_name>": "post_tool_call",
    "<command_name>": "ping",
    "<ENV_VAR>": "TEST_API_KEY",
    "<auth source>": "API key",
    "<Why this is needed>": "API access",
    "<why this is needed>": "API access",
}


def _substitute(text: str, mapping: dict[str, str]) -> str:
    out = text
    for placeholder, value in mapping.items():
        out = out.replace(placeholder, value)
    return out


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"plugin-creator skill missing at {SKILL_PATH}"
    return SKILL_PATH.read_text()


@pytest.fixture(scope="module")
def python_blocks(skill_text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", skill_text, re.DOTALL)


@pytest.fixture(scope="module")
def yaml_blocks(skill_text: str) -> list[str]:
    return re.findall(r"```yaml\n(.*?)```", skill_text, re.DOTALL)


def test_skill_file_exists(skill_text: str):
    assert "name: plugin-creator" in skill_text
    assert "description:" in skill_text


def test_skill_has_python_templates(python_blocks: list[str]):
    # We expect at least 7 code templates (A through H, with manifest yaml).
    # If a future edit drops below 6, that's almost certainly accidental
    # deletion — flag it.
    assert len(python_blocks) >= 6, (
        f"Expected ≥6 python templates in plugin-creator skill, got "
        f"{len(python_blocks)}"
    )


def test_skill_has_yaml_manifests(yaml_blocks: list[str]):
    assert len(yaml_blocks) >= 3, (
        f"Expected ≥3 YAML manifest examples, got {len(yaml_blocks)}"
    )


def test_every_python_template_parses(python_blocks: list[str]):
    """Each code template must be valid Python after placeholder fill-in."""
    failures: list[str] = []
    for i, block in enumerate(python_blocks, 1):
        code = _substitute(block, PYTHON_PLACEHOLDERS)
        try:
            ast.parse(code)
        except SyntaxError as e:
            failures.append(f"Block {i}: {e}\n--- code ---\n{code[:300]}")
    assert not failures, "\n\n".join(failures)


def test_every_yaml_manifest_parses(yaml_blocks: list[str]):
    failures: list[str] = []
    for i, block in enumerate(yaml_blocks, 1):
        text = _substitute(block, YAML_PLACEHOLDERS)
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as e:
            failures.append(f"Block {i}: {e}\n--- yaml ---\n{text[:200]}")
    assert not failures, "\n\n".join(failures)


def test_skill_discoverable_by_loader():
    """SkillsLoader must surface plugin-creator as available so the
    agent's system prompt advertises it."""
    from flowly.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=Path.cwd())
    skills = loader.list_skills(filter_unavailable=False)
    names = [s["name"] for s in skills]
    assert "plugin-creator" in names, (
        f"plugin-creator not discovered. Found: {names}"
    )


def test_skill_loadable_content():
    """load_skill returns the markdown body so the agent can read it."""
    from flowly.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=Path.cwd())
    content = loader.load_skill("plugin-creator")
    assert content is not None and len(content) > 5_000, (
        "plugin-creator skill content suspiciously small or missing"
    )
    # Sanity-check key sections are present
    assert "register_tool" in content
    assert "register_hook" in content
    assert "register_command" in content
    assert "plugin.yaml" in content
    # Make sure each template label exists so the agent can reference them
    for label in ("Template A", "Template B", "Template C", "Template D",
                  "Template E", "Template F", "Template G", "Template H"):
        assert label in content, f"{label} missing from plugin-creator skill"


def test_skill_warns_against_secrets_in_init():
    """The skill MUST tell the agent not to write API keys into __init__.py.
    If this guidance gets accidentally removed in an edit, the agent will
    start producing insecure plugins. Catch it here."""
    text = SKILL_PATH.read_text()
    assert "Don't put secrets in" in text or "don't put secrets in" in text.lower(), (
        "Skill missing 'no secrets in __init__.py' guidance"
    )


def test_skill_mentions_restart_requirement():
    """Plugins only load on gateway startup. The skill must say so —
    otherwise users wonder why their plugin isn't working."""
    text = SKILL_PATH.read_text()
    assert "restart" in text.lower(), (
        "Skill missing gateway-restart instruction"
    )


def test_skill_lists_reserved_command_names():
    """Reserved slash commands (/new /clear /compact /help) must be
    flagged so the agent doesn't silently fail when picking a name."""
    text = SKILL_PATH.read_text()
    for reserved in ("new", "clear", "compact", "help"):
        # Look for them in the reserved-names context
        assert reserved in text, f"reserved name {reserved!r} not mentioned"


def test_skill_emphasizes_config_json_enable_step():
    """The single most common failure mode is the agent forgetting to
    add the plugin to plugins.enabled in config.json. The skill must
    flag this prominently — not buried at the bottom — because user
    plugins do not load without it.

    Caught a real failure: agent created a /saat plugin, wrote files,
    ran py_compile, told user to restart — but never edited config.json.
    User restarted, /saat did nothing.
    """
    text = SKILL_PATH.read_text()
    # Must mention config.json explicitly
    assert "config.json" in text, "Skill missing config.json mention"
    # Must mention plugins.enabled by exact key
    assert "plugins.enabled" in text or "plugins.enabled" in text.replace(" ", ""), (
        "Skill missing 'plugins.enabled' reference"
    )
    # Must call out that user plugins are opt-in
    assert "opt-in" in text.lower() or "opt in" in text.lower(), (
        "Skill must mention user plugins are opt-in"
    )


def test_skill_warns_against_skill_manage_for_plugins():
    """Real failure observed: agent confused skill_manage (creates a
    SKILL at ~/.flowly/skills/) with plugin creation, producing a
    garbage skill while also writing the actual plugin elsewhere. The
    skill must explicitly tell the agent these tools are NOT for
    plugin creation."""
    text = SKILL_PATH.read_text()
    assert "skill_manage" in text, (
        "Skill must mention skill_manage to warn against using it"
    )


def test_skill_pins_kind_to_standalone():
    """Real failure observed: agent invented `kind: command` in the
    manifest. Only `standalone` is valid in v1; `backend`/`exclusive`
    are reserved. Anything else falls back with a warning. The skill
    must hardcode the value to prevent invented variants."""
    text = SKILL_PATH.read_text()
    # Look for the explicit prohibition language
    must_have_phrases = [
        "ALWAYS this exact value",  # the inline manifest comment
        "MUST be `standalone`",      # the explicit rule
    ]
    for phrase in must_have_phrases:
        assert phrase in text, f"Skill missing kind enforcement phrase: {phrase!r}"


def test_skill_warns_about_read_file_workspace_restriction():
    """Real failure observed: agent tried `read_file ~/.flowly/config.json`,
    got 'Access denied — path outside workspace', then improvised three
    fallback iterations. The skill must tell the agent up-front that
    config.json is outside workspace and provide the right tool."""
    text = SKILL_PATH.read_text()
    # Must mention the workspace restriction so the agent doesn't
    # waste an iteration trying read_file
    assert "outside the workspace" in text.lower() or "outside workspace" in text.lower(), (
        "Skill must warn that ~/.flowly/config.json is outside workspace"
    )


def test_config_recipe_uses_atomic_write_pattern():
    """The config-mutation recipe MUST be corruption-resistant.

    A naive `open(p, "w").write(...)` truncates the file before write —
    if the write is interrupted (disk full, OOM, kill), config.json is
    left as zero bytes and the user loses ALL their settings.

    The hardened pattern is:
      1. Backup original to .bak before mutating
      2. Write new content to a temp file in the same dir
      3. fsync the temp file
      4. os.replace() to atomically rename over the original

    This test pins those four properties so they don't get accidentally
    removed in a future skill edit."""
    text = SKILL_PATH.read_text()

    # Recipe must use tempfile + os.replace for atomic writes
    assert "tempfile" in text, "Skill recipe missing tempfile import (atomic write)"
    assert "os.replace" in text, "Skill recipe missing os.replace (atomic rename)"

    # Recipe must back up the original before mutating
    assert "shutil.copy" in text or ".bak" in text, (
        "Skill recipe missing backup step before mutation"
    )

    # Recipe must fsync to ensure write hits disk before rename
    assert "fsync" in text, (
        "Skill recipe missing fsync — write may not survive power loss"
    )

    # Recipe must verify the write succeeded by reading back
    assert "verification" in text.lower() or "verify" in text.lower(), (
        "Skill recipe missing post-write verification"
    )


def test_config_recipe_forbids_unsafe_alternatives():
    """The skill must explicitly tell the agent NOT to use jq, sed, awk,
    or shell echo — all common ways to corrupt JSON. If the agent
    decides to be 'creative' and skip the recipe, the skill should at
    least have warned against the worst alternatives."""
    text = SKILL_PATH.read_text()
    # The skill must call these out by name as forbidden
    forbidden_mentioned = sum(1 for tool in ("jq", "sed", "awk", "echo") if tool in text)
    assert forbidden_mentioned >= 3, (
        f"Skill should explicitly warn against jq/sed/awk/echo as JSON "
        f"corruption sources; only {forbidden_mentioned} mentioned"
    )
