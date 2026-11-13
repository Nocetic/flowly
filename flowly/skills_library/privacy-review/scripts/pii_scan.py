#!/usr/bin/env python3
"""PII & tracker scanner — seed a data inventory for a privacy review.

Scans a file, directory, or stdin for likely PII patterns and common
third-party trackers/SDKs. Heuristic: finds candidates to investigate, not a
definitive PII census. Expect false positives/negatives. Not legal advice.

Stdlib only. Prints chat-ready markdown.

Usage:
    pii_scan.py ./src
    pii_scan.py file.txt
    cat data.json | pii_scan.py -
"""
from __future__ import annotations

import argparse
import os
import re
import sys

PII = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone (intl/US)": r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)",
    "SSN (US)": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "credit-card-shaped": r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)",
    "IPv4": r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)",
    "date-of-birth field": r"\b(date[_ ]?of[_ ]?birth|dob|birth[_ ]?date)\b",
    "passport/national-id field": r"\b(passport|national[_ ]?id|nin|tax[_ ]?id|ssn|sin)\b",
    "name field": r"\b(first[_ ]?name|last[_ ]?name|full[_ ]?name|surname)\b",
    "address field": r"\b(street[_ ]?address|postal[_ ]?code|zip[_ ]?code|home[_ ]?address)\b",
    "geolocation": r"\b(latitude|longitude|geolocation|gps[_ ]?coord)\b",
    "auth secret": r"\b(password|api[_ ]?key|secret|access[_ ]?token|private[_ ]?key)\b",
}

SPECIAL = {  # GDPR special-category indicators
    "health data": r"\b(health|medical|diagnosis|patient|prescription|disability)\b",
    "biometric": r"\b(biometric|fingerprint|face[_ ]?id|facial[_ ]?recognition|retina)\b",
    "sensitive (religion/ethnicity/sexuality)": r"\b(religion|ethnicit|race|sexual orientation|political)\b",
    "children's data": r"\b(child|minor|under[_ ]?13|coppa|parental consent)\b",
}

TRACKERS = {
    "Google Analytics": r"google-analytics|gtag\(|ga\.js|googletagmanager|UA-\d|G-[A-Z0-9]{8,}",
    "Meta/Facebook Pixel": r"facebook\.net/.*fbevents|fbq\(|connect\.facebook|meta pixel",
    "Google Ads / DoubleClick": r"doubleclick|googleadservices|googlesyndication",
    "TikTok Pixel": r"tiktok.*pixel|analytics\.tiktok",
    "Segment": r"segment\.(com|io)|analytics\.track\(",
    "Mixpanel": r"mixpanel",
    "Amplitude": r"amplitude",
    "Hotjar / session replay": r"hotjar|fullstory|logrocket|mouseflow|smartlook",
    "Sentry (may capture PII)": r"sentry",
    "Stripe (payment)": r"stripe",
    "Intercom": r"intercom",
}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".html", ".css",
            ".yml", ".yaml", ".env", ".sql", ".java", ".rb", ".go", ".php", ".csv", ".xml", ".toml", ".ini"}


def iter_text(path):
    if path == "-":
        yield "<stdin>", sys.stdin.read()
        return
    if os.path.isfile(path):
        yield path, _read(path)
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in TEXT_EXT:
                continue
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > 5_000_000:
                    continue
            except OSError:
                continue
            yield fp, _read(fp)


def _read(fp):
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser(description="PII & tracker scanner")
    ap.add_argument("path", help="file, directory, or '-' for stdin")
    ap.add_argument("--examples", type=int, default=2, help="example matches to show per category")
    a = ap.parse_args()

    pii_hits = {k: {"count": 0, "files": set(), "ex": []} for k in PII}
    special_hits = {k: {"count": 0, "files": set()} for k in SPECIAL}
    tracker_hits = {k: set() for k in TRACKERS}
    scanned = 0

    for fp, text in iter_text(a.path):
        if not text:
            continue
        scanned += 1
        low = text.lower()
        for name, pat in PII.items():
            for m in re.finditer(pat, text):
                pii_hits[name]["count"] += 1
                pii_hits[name]["files"].add(fp)
                if len(pii_hits[name]["ex"]) < a.examples:
                    s = m.group(0)
                    # redact the middle of literal values
                    if name in ("email", "phone (intl/US)", "SSN (US)", "credit-card-shaped", "IPv4"):
                        s = s[:3] + "…" + s[-2:] if len(s) > 6 else "…"
                    pii_hits[name]["ex"].append(s)
        for name, pat in SPECIAL.items():
            for _ in re.finditer(pat, low):
                special_hits[name]["count"] += 1
                special_hits[name]["files"].add(fp)
        for name, pat in TRACKERS.items():
            if re.search(pat, low):
                tracker_hits[name].add(fp)

    print(f"**PII & tracker scan** ({scanned} file(s)) — heuristic, verify; not legal advice\n")

    found = {k: v for k, v in pii_hits.items() if v["count"]}
    if found:
        print("📦 Likely PII detected:")
        for name, v in sorted(found.items(), key=lambda x: -x[1]["count"]):
            ex = f" e.g. {', '.join(v['ex'])}" if v["ex"] else ""
            print(f"- {name}: {v['count']} match(es) in {len(v['files'])} file(s){ex}")
    else:
        print("📦 No common PII patterns matched (does not mean none exists).")

    sfound = {k: v for k, v in special_hits.items() if v["count"]}
    if sfound:
        print("\n🔴 Special-category / children's data indicators (stricter rules):")
        for name, v in sorted(sfound.items(), key=lambda x: -x[1]["count"]):
            print(f"- {name}: {v['count']} hit(s) in {len(v['files'])} file(s)")

    tfound = {k: v for k, v in tracker_hits.items() if v}
    if tfound:
        print("\n⚠️ Third-party trackers / SDKs (consent + 'sharing/sale' exposure):")
        for name, files in tfound.items():
            print(f"- {name} ({len(files)} file(s))")

    # quick flags
    flags = []
    if pii_hits["auth secret"]["count"]:
        flags.append("possible secrets/credentials in scanned files — check for hardcoded keys")
    if pii_hits["credit-card-shaped"]["count"]:
        flags.append("card-shaped numbers found — confirm PCI scope / tokenization")
    if sfound:
        flags.append("special-category/children data present — likely needs DPIA + explicit consent")
    if tfound:
        flags.append("trackers present — needs consent management + privacy-notice disclosure")
    if flags:
        print("\n🚩 Flags:")
        for f in flags:
            print(f"- {f}")

    print("\n_Heuristic scan: false positives (e.g. version numbers as 'cards') and false negatives "
          "(free-text/semantic PII) are expected. Use as a starting inventory, then map by hand._")


if __name__ == "__main__":
    main()
