---
name: regex-builder
description: "Build, test, explain, and debug regular expressions — character classes, quantifiers, groups and backreferences, lookahead/lookbehind, anchors, and flags; with the common ready-made patterns (email, URL, dates, IPs) and the catastrophic-backtracking pitfalls. Includes a stdlib tester that runs a regex against sample strings and shows matches, groups, and substitutions. Use when the user wants a regex written, tested, explained, or fixed, or to extract/validate/replace text by pattern."
metadata: {"flowly":{"emoji":"🔤","tags":["data","regex","regular-expressions","text-processing","pattern-matching","parsing"],"requires":{"bins":["python3"]},"category":"data","related_skills":["sql-query","data-visualization","privacy-review"]}}
---

# Regex — Build It, Then Actually Test It

A regex that looks right and a regex that *is* right are different things — the only way to know is to run it against real and adversarial samples. This skill writes patterns, explains them piece by piece, and (crucially) **tests them with `regex_test.py`** so you ship a verified pattern, not a hopeful one. Default to readable patterns over clever ones.

## What this skill produces

**Chat-first.** Default: the regex, a plain-English breakdown of each part, and the **test results** against sample inputs (matches, captured groups, what it rejects). For replacement tasks, the substitution preview. Always note the flavor.

## When to use

- "Write a regex to match / extract / validate \<thing\>."
- "Why doesn't this regex work?" / "Fix this pattern."
- "Explain what this regex does."
- "Extract all \<X\> from this text." / "Find-and-replace by pattern."
- "Validate \<email/phone/URL/date/...\>."

## The building blocks

| Element | Means | | Element | Means |
|---|---|---|---|---|
| `.` | any char (except newline) | | `*` | 0 or more |
| `\d \w \s` | digit / word / whitespace | | `+` | 1 or more |
| `\D \W \S` | negations | | `?` | 0 or 1 (optional) |
| `[abc]` `[^abc]` | set / negated set | | `{n}` `{n,m}` | exact / range count |
| `[a-z0-9]` | ranges | | `*? +? ??` | **lazy** (minimal) |
| `^ $` | start / end (of string/line) | | `\b` | word boundary |
| `( )` | capture group | | `(?: )` | non-capturing group |
| `(?P<name> )` | named group | | `\1` | backreference |
| `a\|b` | alternation | | `\.` `\(` | escape a literal |
| `(?= )` `(?! )` | lookahead (pos/neg) | | `(?<= )` `(?<! )` | lookbehind |

## Principles

- **Anchor when validating.** To validate a *whole* string, wrap with `^...$` — otherwise the regex matches a substring and accepts "junk\<valid\>junk". For *extraction*, leave it unanchored.
- **Greedy vs lazy.** `.*` grabs as much as possible and backtracks; `.*?` grabs as little. `<.*>` on `<a><b>` matches the whole thing; `<.*?>` matches `<a>`. Pick deliberately.
- **Escape literals.** `.`, `*`, `+`, `?`, `(`, `)`, `[`, `]`, `{`, `}`, `^`, `$`, `|`, `\` are special — backslash them to match literally (e.g. `\.` for a real dot).
- **Prefer specific classes over `.`** — `[^,]+` (everything but a comma) is safer and faster than `.*?` for field parsing.
- **Use named groups** (`(?P<year>\d{4})`) for readable extraction instead of counting positions.
- **Flags:** `i` (ignore case), `m` (`^`/`$` match per line), `s` (dotall — `.` matches newline), `x` (verbose — whitespace/comments allowed for readability).

## Flavors differ — say which

PCRE/Python/JS/Go/.NET/grep mostly share basics but diverge on: lookbehind support, named-group syntax (`(?P<n>)` Python vs `(?<n>)` JS/.NET), `\d` unicode behavior, POSIX classes, and whether it's POSIX BRE/ERE (grep/sed). State the target; the helper uses Python's `re` (close to PCRE).

## Catastrophic backtracking (the dangerous pitfall)

Nested quantifiers over overlapping patterns — `(a+)+`, `(.*)*`, `(\w+\s*)+` — can blow up exponentially on certain inputs (ReDoS), hanging the process. Avoid nested/ambiguous quantifiers; make sub-patterns mutually exclusive; use possessive quantifiers/atomic groups where supported, or restructure. **Test against a long non-matching string** to catch it.

## Ready-made starting points (validate for your needs!)

- Email (pragmatic): `^[^@\s]+@[^@\s]+\.[^@\s]+$` (full RFC 5322 is famously huge — usually overkill).
- URL (rough): `https?://[^\s]+`
- IPv4: `\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b`
- ISO date: `\d{4}-\d{2}-\d{2}`
- Integer/decimal: `-?\d+(?:\.\d+)?`
These are starting points — tighten or loosen per the real requirement, and **test**.

## The tester

`scripts/regex_test.py` (stdlib `re`) runs a pattern against samples and shows matches, groups, named groups, and substitutions.

```bash
python3 scripts/regex_test.py '(\d{4})-(\d{2})-(\d{2})' "2026-06-08" "bad-date"
python3 scripts/regex_test.py '^[^@\s]+@[^@\s]+\.[^@\s]+$' --validate a@b.com "x@y" "no-at"
python3 scripts/regex_test.py '\bid=(?P<id>\d+)' "id=42 id=99" --flags i
python3 scripts/regex_test.py 'colou?r' "color colour" --sub "X"      # substitution preview
echo "multi\nline" | python3 scripts/regex_test.py '^line' - --flags m  # read text from stdin
```

## Chat output format

````
**Extract dates (YYYY-MM-DD)**

```regex
(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})
```
- `\d{4}` four-digit year (named `year`), then literal `-`, etc.

Tested against "Met on 2026-06-08, due 2026-12-01":
  ✓ 2026-06-08 → year=2026 month=06 day=08
  ✓ 2026-12-01 → year=2026 month=12 day=01
Note: validates format, not real calendar dates (would match 2026-13-40).
For whole-string validation add ^...$.
````

## Workflow

1. **Clarify the goal** (validate whole string vs extract substrings vs replace) and the **flavor**.
2. **Gather samples** — things it MUST match and things it MUST NOT (the rejects are what make it correct).
3. **Build** with specific classes, anchors if validating, named groups for extraction; keep it readable.
4. **Test with `regex_test.py`** against both sets, including an adversarial long string for backtracking.
5. **Explain** the pattern piece by piece; note limits (e.g. "format only, not semantic validity").
6. **Deliver** the verified pattern + breakdown + test evidence; route bulk text/data to `sql-query`/`data-visualization`, PII patterns to `privacy-review`.

## Key pitfalls

- **Not anchoring a validator.** Unanchored, it accepts garbage around a valid match — use `^...$` to validate.
- **Greedy when you meant lazy.** `.*` over-grabs; use `.*?` or a negated class to stop at the right delimiter.
- **Unescaped metacharacters.** `.` matches any char, not a dot; escape literals.
- **Catastrophic backtracking.** Nested ambiguous quantifiers hang on adversarial input — restructure and test with long non-matches.
- **Over-trusting ready-made patterns.** The "email regex" you copied may reject valid addresses or accept junk — test against your real cases.
- **Flavor mismatch.** Lookbehind/named-group syntax/unicode differ across engines — write for the target.
- **Regex for the wrong job.** Don't parse deeply nested/recursive structures (HTML, JSON, balanced brackets) with regex — use a real parser. Regex is for regular patterns.
- **Forgetting flags.** Case sensitivity, multiline `^`/`$`, and dotall change everything — set them explicitly.

## Quick reference

- Classes `\d \w \s` (+ negations), sets `[...]`/`[^...]`; quantifiers `* + ? {n,m}` (+ lazy `?`).
- Anchors `^ $ \b`; groups `( )` / `(?: )` / `(?P<name> )`; alternation `|`; escape literals with `\`.
- Lookarounds `(?=)(?!)(?<=)(?<!)`; flags i (case), m (multiline), s (dotall), x (verbose).
- Anchor to validate, leave open to extract; prefer `[^x]+` over `.*?`; name your groups.
- Avoid nested ambiguous quantifiers (ReDoS); don't parse recursive formats with regex.
- Always test with `regex_test.py` against must-match AND must-not-match samples.
