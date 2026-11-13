#!/usr/bin/env python3
"""Bioinformatics sequence toolkit — GC, reverse-complement, transcribe,
translate, ORF finding, composition, FASTA parse. Stdlib only. Chat-ready.

Usage:
    seqtools.py gc ATGCGCTA
    seqtools.py revcomp ATGC
    seqtools.py transcribe ATGGAA
    seqtools.py translate AUGGAAUAA        (DNA or RNA; auto)
    seqtools.py orf ATGAAATAGCC [--min-aa 1]
    seqtools.py composition ATGCATGC
    seqtools.py fasta seqs.fa
"""
from __future__ import annotations

import argparse
import os
import sys

COMP = {"A": "T", "T": "A", "G": "C", "C": "G", "U": "A", "N": "N",
        "a": "t", "t": "a", "g": "c", "c": "g", "u": "a", "n": "n"}

CODON = {
    "UUU": "F", "UUC": "F", "UUA": "L", "UUG": "L", "CUU": "L", "CUC": "L", "CUA": "L", "CUG": "L",
    "AUU": "I", "AUC": "I", "AUA": "I", "AUG": "M", "GUU": "V", "GUC": "V", "GUA": "V", "GUG": "V",
    "UCU": "S", "UCC": "S", "UCA": "S", "UCG": "S", "CCU": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "ACU": "T", "ACC": "T", "ACA": "T", "ACG": "T", "GCU": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "UAU": "Y", "UAC": "Y", "UAA": "*", "UAG": "*", "CAU": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "AAU": "N", "AAC": "N", "AAA": "K", "AAG": "K", "GAU": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "UGU": "C", "UGC": "C", "UGA": "*", "UGG": "W", "CGU": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "AGU": "S", "AGC": "S", "AGA": "R", "AGG": "R", "GGU": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}


def clean(s):
    return "".join(c for c in s if c.isalpha()).upper()


def revcomp(s):
    return "".join(COMP.get(c, "N") for c in reversed(s))


def to_rna(s):
    return s.upper().replace("T", "U")


def translate(rna, frame=0):
    prot = []
    for i in range(frame, len(rna) - 2, 3):
        prot.append(CODON.get(rna[i:i + 3], "X"))
    return "".join(prot)


def cmd_gc(a):
    s = clean(a.seq)
    gc = sum(c in "GC" for c in s)
    print(f"Length {len(s)} bp · GC content = {gc}/{len(s)} = **{gc/len(s)*100:.1f}%**" if s else "empty")


def cmd_revcomp(a):
    s = clean(a.seq)
    print(f"5'-{s}-3'\nrev-comp: 5'-{revcomp(s)}-3'")


def cmd_transcribe(a):
    s = clean(a.seq)
    print(f"DNA (coding): {s}\nmRNA:        {to_rna(s)}")


def cmd_translate(a):
    s = to_rna(clean(a.seq))
    prot = translate(s, a.frame)
    print(f"mRNA: {s}")
    print(f"Protein (frame {a.frame}): {prot}")
    # 3-letter readout of first codons
    three = {"M": "Met", "F": "Phe", "L": "Leu", "E": "Glu", "*": "Stop"}
    pretty = "-".join(three.get(p, p) for p in prot[:8])
    print(f"  {pretty}{'...' if len(prot) > 8 else ''}")


def find_orfs(seq, min_aa):
    seq = clean(seq)
    orfs = []
    for strand, s in (("+", seq), ("-", revcomp(seq))):
        rna = to_rna(s)
        for frame in range(3):
            i = frame
            while i < len(rna) - 2:
                if rna[i:i + 3] == "AUG":
                    j = i
                    prot = []
                    while j < len(rna) - 2:
                        aa = CODON.get(rna[j:j + 3], "X")
                        if aa == "*":
                            if len(prot) >= min_aa:
                                orfs.append((strand, frame, i, "".join(prot)))
                            break
                        prot.append(aa)
                        j += 3
                    i = j + 3
                else:
                    i += 3
    return orfs


def cmd_orf(a):
    orfs = find_orfs(a.seq, a.min_aa)
    print(f"**ORF scan** (6 frames, min {a.min_aa} aa)\n")
    if not orfs:
        print("No ORFs found above the threshold.")
        return
    for strand, frame, pos, prot in sorted(orfs, key=lambda o: -len(o[3])):
        print(f"  strand {strand} frame {frame} @pos {pos}: {len(prot)} aa → {prot}")


def cmd_composition(a):
    s = clean(a.seq)
    from collections import Counter
    c = Counter(s)
    total = len(s)
    print(f"**Composition** ({total} bases)\n")
    for base in sorted(c):
        print(f"  {base}: {c[base]} ({c[base]/total*100:.1f}%)")


def cmd_fasta(a):
    if not os.path.exists(a.file):
        sys.exit(f"no such file: {a.file}")
    records = []
    hid, seq = None, []
    for line in open(a.file, encoding="utf-8", errors="replace"):
        line = line.rstrip()
        if line.startswith(">"):
            if hid is not None:
                records.append((hid, "".join(seq)))
            hid = line[1:].strip(); seq = []
        elif line:
            seq.append(line)
    if hid is not None:
        records.append((hid, "".join(seq)))
    print(f"**FASTA: {a.file}** — {len(records)} sequence(s)\n")
    for hid, s in records[:20]:
        gc = sum(c in "GCgc" for c in s) / len(s) * 100 if s else 0
        print(f"  {hid[:50]}: {len(s)} bp, GC {gc:.1f}%")
    if len(records) > 20:
        print(f"  ... +{len(records)-20} more")
    if records:
        lens = [len(s) for _, s in records]
        print(f"\nTotal {sum(lens):,} bp · longest {max(lens):,} · shortest {min(lens):,}")


def main():
    ap = argparse.ArgumentParser(description="Bioinformatics sequence toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("gc", cmd_gc), ("revcomp", cmd_revcomp), ("transcribe", cmd_transcribe), ("composition", cmd_composition)):
        p = sub.add_parser(name); p.add_argument("seq"); p.set_defaults(fn=fn)
    p = sub.add_parser("translate"); p.add_argument("seq"); p.add_argument("--frame", type=int, default=0); p.set_defaults(fn=cmd_translate)
    p = sub.add_parser("orf"); p.add_argument("seq"); p.add_argument("--min-aa", type=int, default=1); p.set_defaults(fn=cmd_orf)
    p = sub.add_parser("fasta"); p.add_argument("file"); p.set_defaults(fn=cmd_fasta)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
