#!/usr/bin/env python3
"""Dockerfile linter — heuristic scan for common best-practice issues.
Stdlib only. Chat-ready markdown. Not a substitute for hadolint/docker build;
catches the frequent mistakes.

Usage:
    dockerlint.py Dockerfile
"""
from __future__ import annotations

import argparse
import os
import re
import sys


def lint(lines):
    issues = []  # (severity, line_no, message)
    instructions = []  # (line_no, verb, rest)
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w+)\s+(.*)$", line)
        if not m:
            continue
        instructions.append((i, m.group(1).upper(), m.group(2)))

    froms = [(i, rest) for i, v, rest in instructions if v == "FROM"]
    # base image pinning
    for i, rest in froms:
        img = rest.split(" AS ")[0].strip().split()[0]
        if ":" not in img.split("/")[-1]:
            issues.append(("🔴", i, f"base image '{img}' has no tag — pins to :latest implicitly (non-reproducible)"))
        elif img.endswith(":latest"):
            issues.append(("🔴", i, f"base image '{img}' uses :latest — pin a specific version (or digest)"))

    # COPY . before dependency install
    dep_files = ("package.json", "package-lock.json", "requirements.txt", "go.mod",
                 "pom.xml", "Gemfile", "Cargo.toml", "yarn.lock", "pnpm-lock")
    install_re = re.compile(r"\b(npm (ci|install)|pip install|go mod download|bundle install|"
                            r"cargo build|mvn |yarn|pnpm install|poetry install)\b")
    copy_all_line = None
    install_line = None
    for i, v, rest in instructions:
        if v in ("COPY", "ADD") and re.match(r"^(--\S+\s+)*\.\s", rest + " "):
            if copy_all_line is None:
                copy_all_line = i
        if v == "RUN" and install_re.search(rest):
            install_line = i
    if copy_all_line and install_line and copy_all_line < install_line:
        issues.append(("🟠", copy_all_line, "`COPY . .` appears before the dependency install — "
                       "busts the dep cache on every source change. Copy the manifest + install first."))

    # USER non-root (in the final stage)
    user_dirs = [(i, rest) for i, v, rest in instructions if v == "USER"]
    if not user_dirs:
        issues.append(("🟠", froms[-1][0] if froms else 1,
                       "no USER instruction — container runs as root. Add a non-root USER."))
    elif user_dirs[-1][1].strip() in ("root", "0"):
        issues.append(("🟠", user_dirs[-1][0], "final USER is root — switch to a non-root user."))

    # apt-get hygiene
    for i, v, rest in instructions:
        if v == "RUN" and "apt-get install" in rest:
            if "--no-install-recommends" not in rest:
                issues.append(("🟡", i, "apt-get install without --no-install-recommends (larger image)"))
            if "rm -rf /var/lib/apt/lists" not in rest:
                issues.append(("🟡", i, "apt-get without cleaning /var/lib/apt/lists in the same RUN (bloats layer)"))
        if v == "RUN" and "apt-get upgrade" in rest:
            issues.append(("🟡", i, "apt-get upgrade in image — pin packages instead for reproducibility"))

    # secrets in ENV/ARG
    secret_re = re.compile(r"\b(password|passwd|secret|api[_-]?key|token|private[_-]?key|aws_secret)\b", re.I)
    for i, v, rest in instructions:
        if v in ("ENV", "ARG") and secret_re.search(rest):
            issues.append(("🔴", i, f"{v} appears to set a secret — bakes into image layers permanently. "
                           "Use build secrets / runtime env."))

    # shell-form CMD/ENTRYPOINT
    for i, v, rest in instructions:
        if v in ("CMD", "ENTRYPOINT") and not rest.strip().startswith("["):
            issues.append(("🟠", i, f"{v} in shell form — wraps in /bin/sh -c so SIGTERM won't reach the app. "
                           "Use exec form [\"...\"]."))

    # ADD for non-url, non-tar
    for i, v, rest in instructions:
        if v == "ADD" and not re.search(r"https?://|\.tar", rest):
            issues.append(("🟡", i, "ADD used for plain files — prefer COPY (ADD only for URLs / tar auto-extract)"))

    # HEALTHCHECK presence (informational)
    if not any(v == "HEALTHCHECK" for _, v, _ in instructions):
        issues.append(("🟡", 0, "no HEALTHCHECK — the platform can't tell when the container is ready/alive"))

    return issues, len(froms) > 1


def main():
    ap = argparse.ArgumentParser(description="Dockerfile linter")
    ap.add_argument("dockerfile")
    a = ap.parse_args()
    if not os.path.exists(a.dockerfile):
        sys.exit(f"no such file: {a.dockerfile}")
    lines = open(a.dockerfile, encoding="utf-8", errors="replace").read().splitlines()
    issues, multistage = lint(lines)

    print(f"**Dockerfile lint — {a.dockerfile}**" + ("  (multi-stage ✅)" if multistage else "") + "\n")
    if not issues:
        print("✅ No common issues found. (Heuristic scan — still `docker build` and scan the image.)")
        return
    order = {"🔴": 0, "🟠": 1, "🟡": 2}
    for sev, ln, msg in sorted(issues, key=lambda x: (order[x[0]], x[1])):
        loc = f"L{ln}: " if ln else ""
        print(f"{sev} {loc}{msg}")
    # check .dockerignore alongside
    d = os.path.dirname(os.path.abspath(a.dockerfile))
    if not os.path.exists(os.path.join(d, ".dockerignore")):
        print("🟡 no .dockerignore next to the Dockerfile — add one (exclude .git, node_modules, secrets).")
    print("\n_Heuristic — for thorough checks use hadolint and an image scanner (Trivy/Scout)._")


if __name__ == "__main__":
    main()
