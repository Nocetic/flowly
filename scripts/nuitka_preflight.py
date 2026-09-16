"""Validate the exact Nuitka arguments before starting expensive compilation.

Run after ``uv sync --locked --group nuitka`` with ``uv run --no-sync python``.
Pass --check-only to validate without compiling; all other arguments go to Nuitka.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def include_requirements(arguments: list[str], entry_text: str, *, semantic: bool) -> tuple[set[str], set[str]]:
    # entry.py also declares conditional semantic-runtime includes. Check those
    # actual directives, not a second package list that can drift out of date.
    directives = re.findall(r"^#\s+nuitka-project: (\S+)", entry_text, re.MULTILINE)
    effective = arguments + (directives if semantic else [])
    modules: set[str] = set()
    distributions: set[str] = set()
    for argument in effective:
        option, _, value = argument.partition("=")
        if option in {"--include-package", "--include-module", "--include-package-data"}:
            modules.add(value.split(":", 1)[0])
        elif option == "--include-distribution-metadata":
            distributions.add(value)
    return modules, distributions


def validate(arguments: list[str], root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    compiler = next(p["version"] for p in lock["package"] if p["name"] == "nuitka")
    for distribution, expected in (("Nuitka", compiler), ("flowly-ai", project["project"]["version"])):
        try:
            installed = importlib.metadata.version(distribution)
            if installed != expected:
                errors.append(f"{distribution}: installed {installed}, expected {expected}")
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"missing distribution: {distribution}")

    modules, distributions = include_requirements(
        arguments,
        (root / "flowly/cli/entry.py").read_text(),
        semantic=os.environ.get("FLOWLY_NUITKA_NO_SEMANTIC", "0") != "1",
    )
    for module in sorted(modules):
        try:
            if importlib.util.find_spec(module) is None:
                errors.append(f"missing module: {module}")
        except (ImportError, AttributeError, ValueError) as exc:
            errors.append(f"cannot resolve {module}: {exc}")
    for distribution in sorted(distributions):
        try:
            importlib.metadata.distribution(distribution)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"missing metadata: {distribution}")
    for argument in arguments:
        option, _, value = argument.partition("=")
        source = value.split("=", 1)[0]
        if option == "--include-data-dir" and not (root / source).is_dir():
            errors.append(f"missing data directory: {source}")
        elif option == "--include-data-files" and not list(root.glob(source)):
            errors.append(f"no data files match: {source}")

    # Resolve lazy transport imports before compilation. No connection is opened.
    for module in ("mcp.client.sse", "mcp.client.streamable_http", "mcp.server.mcpserver"):
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append(f"transport import failed ({module}): {type(exc).__name__}: {exc}")
    return errors


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    check_only = "--check-only" in arguments
    arguments = [arg for arg in arguments if arg != "--check-only"]
    if not arguments:
        print("Pass the complete Nuitka build arguments.", file=sys.stderr)
        return 2
    os.chdir(ROOT)
    errors = validate(arguments)
    if errors:
        print("Nuitka preflight failed before compilation:\n- " + "\n- ".join(errors), file=sys.stderr)
        return 1
    print("Nuitka preflight passed: compiler, includes, metadata, data files and MCP transports.", flush=True)
    if check_only:
        return 0
    return subprocess.run([sys.executable, "-m", "nuitka", *arguments], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
