#!/usr/bin/env python3
"""Regex tester — run a pattern against samples; show matches, groups, named
groups, validation, and substitutions. Stdlib `re`. Chat-ready markdown.

Pass samples as args, or '-' to read lines from stdin.

Usage:
    regex_test.py '(\\d{4})-(\\d{2})-(\\d{2})' "2026-06-08" "bad"
    regex_test.py '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$' --validate a@b.com "x@y"
    regex_test.py '\\bid=(?P<id>\\d+)' "id=42 id=99" --flags i
    regex_test.py 'colou?r' "color colour" --sub "X"
    cat file | regex_test.py 'PATTERN' -
"""
from __future__ import annotations

import argparse
import re
import sys

FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}


def build_flags(s):
    f = 0
    for ch in s or "":
        if ch in FLAG_MAP:
            f |= FLAG_MAP[ch]
        else:
            sys.exit(f"unknown flag '{ch}' (use i/m/s/x/a)")
    return f


def samples_from(args):
    out = []
    for s in args:
        if s == "-":
            out.extend(line.rstrip("\n") for line in sys.stdin)
        else:
            out.append(s)
    return out


def show_match(m):
    parts = [f"'{m.group(0)}'"]
    if m.groups():
        parts.append("groups=" + ", ".join(f"{i+1}:{g!r}" for i, g in enumerate(m.groups())))
    if m.groupdict():
        parts.append("named={" + ", ".join(f"{k}={v!r}" for k, v in m.groupdict().items()) + "}")
    return "  ".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Regex tester")
    ap.add_argument("pattern")
    ap.add_argument("samples", nargs="*", help="strings to test, or '-' for stdin")
    ap.add_argument("--flags", default="", help="combo of i m s x a")
    ap.add_argument("--validate", action="store_true", help="full-match (fullmatch) instead of search")
    ap.add_argument("--sub", help="replacement: show re.sub result per sample")
    a = ap.parse_args()

    flags = build_flags(a.flags)
    try:
        rx = re.compile(a.pattern, flags)
    except re.error as e:
        sys.exit(f"❌ invalid regex: {e}")

    samples = samples_from(a.samples) if a.samples else []
    print(f"**Regex** `{a.pattern}`" + (f"  flags={a.flags}" if a.flags else "")
          + (f"  (groups: {rx.groups})" if rx.groups else "") + "\n")
    if not samples:
        print("_(no samples given — pass strings to test, or '-' for stdin)_")
        return

    for s in samples:
        disp = s if len(s) <= 60 else s[:57] + "..."
        if a.sub is not None:
            result, n = rx.subn(a.sub, s)
            print(f"  '{disp}' → '{result}'  ({n} replacement(s))")
            continue
        if a.validate:
            m = rx.fullmatch(s)
            if m:
                print(f"  ✓ VALID  '{disp}'" + (("  " + show_match(m)) if (m.groups() or m.groupdict()) else ""))
            else:
                print(f"  ✗ invalid '{disp}'")
        else:
            ms = list(rx.finditer(s))
            if not ms:
                print(f"  ✗ no match  '{disp}'")
            else:
                print(f"  ✓ '{disp}' → {len(ms)} match(es)")
                for m in ms:
                    print(f"       {show_match(m)}")


if __name__ == "__main__":
    main()
