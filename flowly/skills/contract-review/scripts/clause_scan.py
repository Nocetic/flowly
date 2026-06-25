#!/usr/bin/env python3
"""Contract clause scanner — triage which standard clauses are present/missing
and flag risky language. Keyword/heuristic-based; a first-pass aid, NOT a
substitute for reading the contract. Not legal advice.

Stdlib only. Reads a file path or '-' for stdin. Prints chat-ready markdown.

Usage:
    clause_scan.py contract.txt
    clause_scan.py contract.txt --side customer
    cat contract.txt | clause_scan.py -
"""
from __future__ import annotations

import argparse
import re
import sys

# clause -> list of indicator patterns (any match => present)
CLAUSES = {
    "Term & termination": [r"\bterm\b", r"terminat", r"renew"],
    "Termination for convenience": [r"for convenience", r"without cause", r"for any reason"],
    "Limitation of liability": [r"limitation of liability", r"liabilit", r"in no event shall"],
    "Liability cap": [r"shall not exceed", r"aggregate liabilit", r"liability.{0,30}cap", r"maximum.{0,20}liabilit"],
    "Indemnification": [r"indemnif", r"hold harmless", r"defend"],
    "IP ownership": [r"intellectual property", r"\bownership\b", r"work made for hire", r"\bIP\b"],
    "Confidentiality": [r"confidential", r"non-disclosure", r"\bNDA\b", r"proprietary information"],
    "Payment terms": [r"payment", r"\bfees?\b", r"invoice", r"net 30", r"net thirty"],
    "Warranties / disclaimer": [r"warrant", r"as is", r"disclaim"],
    "Data & privacy": [r"personal data", r"\bPII\b", r"privacy", r"data protection", r"GDPR", r"CCPA"],
    "Breach notification": [r"breach notif", r"notify.{0,30}breach", r"security incident"],
    "Governing law / venue": [r"governing law", r"jurisdiction", r"\bvenue\b", r"governed by the laws"],
    "Dispute resolution / arbitration": [r"arbitrat", r"dispute", r"mediation"],
    "Assignment": [r"assign", r"successors and assigns"],
    "Non-compete / non-solicit": [r"non-?compet", r"non-?solicit", r"exclusiv"],
    "SLA / service levels": [r"service level", r"\bSLA\b", r"uptime", r"availability"],
    "Force majeure": [r"force majeure", r"acts of god", r"beyond.{0,20}reasonable control"],
    "Insurance": [r"insurance", r"insured", r"coverage of at least"],
    "Data deletion / return on exit": [r"deletion", r"return.{0,30}data", r"destroy.{0,20}data"],
}

# risky language -> (note, pattern)
RISKY = [
    ("uncapped / unlimited liability", r"unlimited liabilit|without limitation.{0,30}liabilit|no.{0,10}limit.{0,20}liabilit"),
    ("sole discretion", r"sole discretion"),
    ("perpetual obligation", r"perpetual|in perpetuity"),
    ("irrevocable grant", r"irrevocab"),
    ("auto-renewal", r"automatically renew|auto-?renew|evergreen"),
    ("unilateral change", r"may (modify|amend|change).{0,40}(at any time|sole discretion)|reserves the right to (modify|change|amend)"),
    ("one-way indemnity (hold harmless)", r"hold harmless"),
    ("waiver of jury / class action", r"waiv.{0,20}(jury|class)"),
    ("broad license to your data/content", r"(perpetual|worldwide|royalty-free).{0,40}licen[cs]e"),
    ("liquidated damages / penalties", r"liquidated damages|penalt"),
    ("most-favored / exclusivity lock-in", r"most favo|exclusiv"),
]

# clauses whose ABSENCE is risky for the named side
PROTECTIONS = ["Liability cap", "Confidentiality", "Breach notification",
               "Data deletion / return on exit", "Indemnification",
               "Governing law / venue", "Insurance"]


def scan(text):
    low = text.lower()
    present, missing = [], []
    for clause, pats in CLAUSES.items():
        hit = any(re.search(p, low) for p in pats)
        (present if hit else missing).append(clause)
    risky = [(note, len(re.findall(p, low))) for note, p in RISKY if re.search(p, low)]
    return present, missing, risky


def main():
    ap = argparse.ArgumentParser(description="Contract clause triage scanner")
    ap.add_argument("file", help="contract text file, or '-' for stdin")
    ap.add_argument("--side", default="", help="user's side, e.g. customer/vendor/employee (tailors notes)")
    a = ap.parse_args()

    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8", errors="replace").read()
    if len(text.strip()) < 50:
        sys.exit("input too short — provide the contract text")

    present, missing, risky = scan(text)
    print("**Contract clause scan** (triage — verify by reading; not legal advice)\n")
    if a.side:
        print(f"_Reviewing from the **{a.side}** side._\n")

    print(f"✅ Present ({len(present)}): " + ", ".join(present) if present else "✅ Present: none detected")
    print()
    missing_prot = [m for m in missing if m in PROTECTIONS]
    other_missing = [m for m in missing if m not in PROTECTIONS]
    if missing_prot:
        print("⚠️ **Missing protections (read closely):** " + ", ".join(missing_prot))
    if other_missing:
        print("• Not detected: " + ", ".join(other_missing))

    if risky:
        print("\n🚩 Risky language detected:")
        for note, count in sorted(risky, key=lambda x: -x[1]):
            print(f"- {note}" + (f" (×{count})" if count > 1 else ""))
    else:
        print("\n🚩 Risky language: none of the common red-flag phrases matched (still read it).")

    print("\n_Keyword heuristic — misses paraphrased/unusual drafting and can false-positive. "
          "Confirm every hit and miss against the actual clauses._")


if __name__ == "__main__":
    main()
