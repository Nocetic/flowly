"""Post-write delta lint for file tools.

Runs in-process syntax checks on .py / .json / .yaml / .toml after a
write or edit. Uses the post-first / pre-lazy strategy: lint the
post-write state first; only if it has errors AND we have pre-write
content do we lint the pre-state and surface only NEW errors. Filters
out pre-existing problems so the agent doesn't chase inherited damage.

The diagnostic-diff pattern borrows from Cline's
``getNewDiagnosticProblems``, OpenCode's WriteTool, and Claude Code's
``DiagnosticTrackingService``.

In-process linters are microseconds per call (ast.parse, json.loads).
Hot path (clean write) runs exactly one lint.
"""

from __future__ import annotations

import ast as _ast
import json as _json
import os
from typing import Callable, Optional


_SKIP = "__SKIP__"


def _lint_python(content: str) -> tuple[bool, str]:
    try:
        _ast.parse(content)
        return True, ""
    except SyntaxError as e:
        loc = f" (line {e.lineno}, column {e.offset})" if e.lineno else ""
        return False, f"{type(e).__name__}: {e.msg}{loc}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _lint_json(content: str) -> tuple[bool, str]:
    try:
        _json.loads(content)
        return True, ""
    except _json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg} (line {e.lineno}, column {e.colno})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _lint_yaml(content: str) -> tuple[bool, str]:
    try:
        import yaml as _yaml
    except ImportError:
        return True, _SKIP
    try:
        _yaml.safe_load(content)
        return True, ""
    except _yaml.YAMLError as e:
        return False, f"YAMLError: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _lint_toml(content: str) -> tuple[bool, str]:
    try:
        import tomllib as _toml
    except ImportError:
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except ImportError:
            return True, _SKIP
    try:
        _toml.loads(content)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


LINTERS: dict[str, Callable[[str], tuple[bool, str]]] = {
    ".py": _lint_python,
    ".json": _lint_json,
    ".yaml": _lint_yaml,
    ".yml": _lint_yaml,
    ".toml": _lint_toml,
}


def is_lintable(path: str) -> bool:
    """True if a linter exists for this extension."""
    return os.path.splitext(path)[1].lower() in LINTERS


def _check(path: str, content: str) -> tuple[Optional[bool], str]:
    """Run the linter for this extension on the given content.

    Returns (ok, message):
      - (None, "") — no linter for this extension, or optional dep missing
      - (True, "") — clean
      - (False, error_msg) — syntax error
    """
    ext = os.path.splitext(path)[1].lower()
    linter = LINTERS.get(ext)
    if linter is None:
        return None, ""
    ok, err = linter(content)
    if err == _SKIP:
        return None, ""
    return ok, err


def check_delta(
    path: str,
    pre_content: Optional[str],
    post_content: str,
) -> Optional[str]:
    """Post-write lint with pre-write baseline filtering.

    Returns a human-readable warning string if the agent should be told
    something, or None when the write is clean (no warning to surface).

    Strategy (post-first / pre-lazy):
      1. Lint post-content. Clean → return None (hot path).
      2. Errors found and we have pre_content → lint pre-content too.
         - Pre clean / unavailable / no-linter → all post errors are new.
         - Pre also broken → set-difference; if every post error existed
           pre-edit, surface "pre-existing, file still broken" so the
           agent knows nothing got worse.
      3. No pre_content (new file) → return all post errors verbatim.
    """
    post_ok, post_msg = _check(path, post_content)
    if post_ok is None or post_ok:
        return None

    if pre_content is None:
        return f"Syntax warning: {post_msg}"

    pre_ok, pre_msg = _check(path, pre_content)
    if pre_ok is None or pre_ok or not pre_msg:
        return f"Syntax warning: {post_msg}"

    if pre_msg.strip() == post_msg.strip():
        return (
            "Pre-existing syntax error — this edit didn't introduce new ones "
            f"but the file is still broken: {post_msg}"
        )

    return f"Syntax warning (new error introduced by this edit): {post_msg}"
