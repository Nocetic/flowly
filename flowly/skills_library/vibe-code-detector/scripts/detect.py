#!/usr/bin/env python3
# =============================================================================
#  vibe-code-detector — passive AI-builder / "vibe code" fingerprint scanner
#  Part of Flowly (Nocetic/flowly). Stdlib only. No third-party dependencies.
#
#  RESPONSIBLE USE (read scripts alongside SKILL.md):
#   - Purpose: heuristic fingerprinting of the TOOLS/TECH a PUBLIC site was built
#     with, from publicly served assets only. Passive: issues ordinary GET
#     requests (like a browser loading the page). It performs NO authentication
#     testing, NO exploitation, and NO unauthorized access.
#   - Results are PROBABILISTIC, not a statement of authorship or quality.
#     False positives are expected (shared stacks such as shadcn/Tailwind/Vercel).
#     Absence of signals does NOT prove human authorship.
#   - Do NOT use output for consequential or discriminatory decisions (hiring,
#     grading, procurement, public accusation). Do not present output as fact.
#   - Any credential this tool notices in a shipped bundle is reported MASKED and
#     is for awareness / responsible disclosure to the site owner only — never
#     for access or exploitation.
#   - Provided as-is, no warranty. Not legal advice. Nocetic is not liable for
#     misuse. Respect the target site's Terms of Service and local law.
# =============================================================================
"""
Usage:
  python3 detect.py <url> [--json] [--max-js N] [--timeout S]
  python3 detect.py --html-file page.html [--url https://site] [--headers-file h.json] [--json]

Default output is a human-readable report. --json emits a machine-readable object.
--html-file mode analyzes already-fetched markup (use it when a live fetch is
blocked, e.g. Cloudflare, and you grabbed the HTML with the browser instead).
"""

import argparse
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (compatible; FlowlyVibeDetect/1.0; passive fingerprint; "
      "+https://github.com/Nocetic/flowly)")

SIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "signatures.json")


# ----------------------------------------------------------------------------- fetch
def _fetch(url, timeout, max_bytes):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(max_bytes)
        headers = {k.lower(): v for k, v in r.headers.items()}
        final = r.geturl()
        status = getattr(r, "status", r.getcode())
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return status, final, headers, raw.decode("utf-8", "replace")


# --------------------------------------------------------------------- corpus build
_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.I | re.S)

# hosts whose bundles are worth fetching for deeper signals
_JS_FETCH_HOSTS = ("supabase", "gpteng", "lovable", "vercel", "netlify",
                   "cloudfront", "amazonaws", "stackblitz", "webcontainer")


def build_corpus(html, base_url, headers, max_js, timeout, max_bytes, allow_js_fetch):
    script_srcs = _SCRIPT_SRC_RE.findall(html)
    inline = "\n".join(_INLINE_SCRIPT_RE.findall(html))

    js_corpus = [inline]
    fetched = []
    if allow_js_fetch and base_url:
        base_host = (urlparse(base_url).hostname or "").lower()
        ranked = []
        for src in script_srcs:
            absu = urljoin(base_url, src)
            host = (urlparse(absu).hostname or "").lower()
            same = host == base_host
            interesting = any(k in host or k in absu.lower() for k in _JS_FETCH_HOSTS)
            # prefer same-origin app bundles, then known-CDN bundles
            score = (2 if same else 0) + (1 if interesting else 0)
            if score:
                ranked.append((score, absu))
        ranked.sort(key=lambda x: -x[0])
        seen = set()
        for _, absu in ranked:
            if len(fetched) >= max_js:
                break
            if absu in seen:
                continue
            seen.add(absu)
            try:
                _, _, _, body = _fetch(absu, timeout, max_bytes)
                js_corpus.append(body)
                fetched.append(absu)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                continue

    js = "\n".join(js_corpus)
    host = (urlparse(base_url).hostname or "") if base_url else ""
    return {
        "html": html,
        "js": js,
        "any": html + "\n" + js,
        "host": host,
        "headers": headers or {},
        "script_srcs": script_srcs,
        "fetched_js": fetched,
    }


# ------------------------------------------------------------------------- matching
def _mask(s):
    s = s.strip()
    if len(s) <= 8:
        return s[0] + "***" if s else "***"
    return s[:4] + "…" + s[-2:] + f" (len {len(s)})"


def _snippet(text, m, secret=False, context=True, width=48):
    val = m.group(0)
    if secret:
        return _mask(val)
    if not context:
        # security observations: show only the matched token, never surrounding
        # context (a wide window could sweep up an adjacent secret)
        v = re.sub(r"\s+", " ", val).strip()
        return v if len(v) <= 80 else v[:80] + "…"
    start = max(0, m.start() - width)
    end = min(len(text), m.end() + width)
    frag = text[start:end].replace("\n", " ").replace("\r", " ")
    frag = re.sub(r"\s+", " ", frag).strip()
    return ("…" + frag + "…") if len(frag) > 8 else frag


def match_signal(corpus, sig, secret=False, context=True):
    where = sig.get("where", "any")
    pat = sig.get("pattern")
    if where == "header":
        hval = corpus["headers"].get(sig.get("header", "").lower())
        if hval is None:
            return None
        if pat and not re.search(pat, hval, re.I):
            return None
        return {"where": "header:" + sig.get("header", ""),
                "sample": _snippet(hval, re.match(r".*", hval), secret, context)}
    text = corpus.get(where, "")
    if not text or not pat:
        return None
    m = re.search(pat, text, re.I)
    if not m:
        return None
    return {"where": where, "sample": _snippet(text, m, secret, context)}


# -------------------------------------------------------------------------- analyze
def analyze(corpus, sigs):
    evidence = []          # tier A + B authorship signals
    aesthetic = []         # tier C weak signals
    security = []          # passive security observations

    # Tier A — per-platform smoking guns
    platform_hits = {}
    for plat in sigs.get("tierA_platforms", []):
        for sig in plat["signals"]:
            hit = match_signal(corpus, sig)
            if hit:
                rec = {"tier": "A", "platform": plat["label"], "platform_id": plat["id"],
                       "note": sig.get("note", ""), **hit}
                evidence.append(rec)
                platform_hits.setdefault(plat["id"], {"label": plat["label"], "count": 0})
                platform_hits[plat["id"]]["count"] += 1

    # Tier B — stack signatures
    b_categories = set()
    for cat in sigs.get("tierB_stack", []):
        for sig in cat["signals"]:
            hit = match_signal(corpus, sig)
            if hit:
                evidence.append({"tier": "B", "category": cat["label"], "category_id": cat["id"],
                                 "note": sig.get("note", ""), **hit})
                b_categories.add(cat["id"])
                break  # one hit per B category is enough

    # Tier C — aesthetic tells (weighted, capped)
    c_weight = 0
    for cat in sigs.get("tierC_aesthetic", []):
        w = cat.get("weight", 1)
        for sig in cat["signals"]:
            hit = match_signal(corpus, sig)
            if hit:
                aesthetic.append({"tier": "C", "category": cat["label"], "category_id": cat["id"],
                                  "weight": w, "note": sig.get("note", ""), **hit})
                c_weight += w
                break

    # Passive security observations (masked)
    for item in sigs.get("security_passive", []):
        is_secret = item.get("secret", False)
        for sig in item["signals"]:
            hit = match_signal(corpus, sig, secret=is_secret, context=False)
            if hit:
                security.append({"label": item["label"], "id": item["id"],
                                 "severity": item.get("severity", "info"),
                                 "note": sig.get("note", ""), **hit})
                break

    # ---- tiered verdict (smoking-gun overrides; weak signals never decide alone)
    if platform_hits:
        top = max(platform_hits.values(), key=lambda p: p["count"])
        strong = top["count"] >= 2 or len(platform_hits) == 1
        verdict = "AI builder detected"
        platform = top["label"]
        confidence = "high" if strong else "medium-high"
        rationale = f"Tier-A fingerprint(s) matched for {platform}."
    elif len(b_categories) >= 2:
        verdict = "Likely AI-builder stack (specific tool unknown)"
        platform = None
        confidence = "medium"
        rationale = (f"{len(b_categories)} distinct AI-builder-stack signatures matched "
                     "(shadcn/Tailwind/Supabase/host etc.). This is the common vibe-code "
                     "stack but a hand-coded site can use it too.")
    elif len(b_categories) == 1 or c_weight >= 3:
        verdict = "Weak / inconclusive signals"
        platform = None
        confidence = "low"
        rationale = ("Only aesthetic tells or a single stack signature matched. "
                     "These are shared by many human-built sites — treat as a hint, not evidence.")
    else:
        verdict = "No AI-builder signals found"
        platform = None
        confidence = "none"
        rationale = ("No fingerprints matched. NOT proof of human authorship — signals can be "
                     "absent, stripped, or behind client-side rendering this scan didn't reach.")

    return {
        "verdict": verdict,
        "platform": platform,
        "confidence": confidence,
        "rationale": rationale,
        "evidence": evidence,
        "aesthetic_tells": aesthetic,
        "security_observations": security,
        "stack_signatures": sorted(b_categories),
        "aesthetic_weight": c_weight,
    }


DISCLAIMER = ("Heuristic & probabilistic — not proof of authorship or quality. False positives "
              "expected on shared stacks. Absence of signals is not proof of human authorship. "
              "Do not use for consequential/discriminatory decisions. Security notes are for "
              "responsible disclosure only, never exploitation.")


# --------------------------------------------------------------------------- output
def render_text(url, result, corpus):
    lines = []
    lines.append("=" * 68)
    lines.append("  VIBE-CODE / AI-BUILDER DETECTION  (passive, heuristic)")
    lines.append("=" * 68)
    lines.append(f"Target : {url}")
    if corpus["host"]:
        lines.append(f"Host   : {corpus['host']}")
    if corpus["fetched_js"]:
        lines.append(f"JS     : analyzed {len(corpus['fetched_js'])} bundle(s)")
    lines.append("")
    lines.append(f"VERDICT    : {result['verdict']}")
    if result["platform"]:
        lines.append(f"PLATFORM   : {result['platform']}")
    lines.append(f"CONFIDENCE : {result['confidence']}")
    lines.append(f"WHY        : {result['rationale']}")
    lines.append("")
    if result["evidence"]:
        lines.append("Fingerprints (authorship signals):")
        for e in result["evidence"]:
            who = e.get("platform") or e.get("category")
            lines.append(f"  [{e['tier']}] {who}  — {e['note']}")
            lines.append(f"        via {e['where']}: {e['sample']}")
    if result["aesthetic_tells"]:
        lines.append("")
        lines.append("Aesthetic tells (weak — supporting only):")
        for a in result["aesthetic_tells"]:
            lines.append(f"  · {a['category']}  (via {a['where']})")
    if result["security_observations"]:
        lines.append("")
        lines.append("Passive security observations (for responsible disclosure, values masked):")
        for s in result["security_observations"]:
            lines.append(f"  [{s['severity'].upper()}] {s['label']}")
            lines.append(f"        {s['note']}")
            lines.append(f"        via {s['where']}: {s['sample']}")
    lines.append("")
    lines.append("-" * 68)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Passive AI-builder / vibe-code fingerprint scanner.")
    ap.add_argument("url", nargs="?", help="URL to scan")
    ap.add_argument("--html-file", help="Analyze already-fetched HTML from this file instead of fetching live")
    ap.add_argument("--headers-file", help="JSON file of response headers to accompany --html-file")
    ap.add_argument("--url", dest="url_opt", help="Site URL (for host signals) when using --html-file")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    ap.add_argument("--max-js", type=int, default=4, help="Max JS bundles to fetch (default 4)")
    ap.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout seconds (default 15)")
    ap.add_argument("--max-bytes", type=int, default=3_000_000, help="Per-response byte cap (default 3MB)")
    ap.add_argument("--no-js", action="store_true", help="Do not fetch external JS bundles")
    args = ap.parse_args()

    try:
        with open(SIG_PATH, encoding="utf-8") as f:
            sigs = json.load(f)
    except (OSError, ValueError) as e:
        print(f"error: cannot load signatures.json: {e}", file=sys.stderr)
        return 2

    base_url = None
    headers = {}
    if args.html_file:
        try:
            with open(args.html_file, encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError as e:
            print(f"error: cannot read --html-file: {e}", file=sys.stderr)
            return 2
        base_url = args.url_opt or args.url
        if args.headers_file:
            try:
                with open(args.headers_file, encoding="utf-8") as f:
                    headers = {k.lower(): v for k, v in json.load(f).items()}
            except (OSError, ValueError) as e:
                print(f"warning: cannot read --headers-file: {e}", file=sys.stderr)
        allow_js = False
    else:
        target = args.url or args.url_opt
        if not target:
            ap.error("provide a URL, or --html-file")
        if not re.match(r"^https?://", target):
            target = "https://" + target
        try:
            _, base_url, headers, html = _fetch(target, args.timeout, args.max_bytes)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            print(f"error: fetch failed for {target}: {e}", file=sys.stderr)
            print("hint: if the site blocks bots (e.g. Cloudflare), fetch the HTML with the "
                  "browser and re-run with --html-file page.html --url " + target, file=sys.stderr)
            return 1
        allow_js = not args.no_js

    corpus = build_corpus(html, base_url, headers, args.max_js, args.timeout,
                          args.max_bytes, allow_js)
    result = analyze(corpus, sigs)

    if args.json:
        out = {
            "target": args.url or args.url_opt or args.html_file,
            "host": corpus["host"],
            "js_bundles_analyzed": corpus["fetched_js"],
            "disclaimer": DISCLAIMER,
            **result,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render_text(args.url or args.url_opt or args.html_file, result, corpus))
    return 0


if __name__ == "__main__":
    sys.exit(main())
