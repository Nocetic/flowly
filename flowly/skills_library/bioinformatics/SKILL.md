---
name: bioinformatics
description: "Analyze biological sequences — DNA/RNA/protein. GC content, reverse-complement, transcription and translation (codon→amino acid), ORF finding, FASTA parsing, k-mer/composition counts, and the basics of alignment and BLAST workflows. Includes a stdlib sequence toolkit. Use when the user has a DNA/RNA/protein sequence to analyze, wants GC content, reverse-complement, to translate/transcribe, find ORFs, parse FASTA, or understand a sequence-analysis workflow."
metadata: {"flowly":{"emoji":"🧬","tags":["science","bioinformatics","dna","rna","protein","sequence","genomics","fasta"],"requires":{"bins":["python3"]},"category":"science","related_skills":["statistical-analysis","chemistry","arxiv","reproducible-research"]}}
---

# Bioinformatics — Read the Sequence, Respect the Conventions

Sequence analysis is exact bookkeeping: the right strand, the right reading frame, the right genetic code. Small convention errors (wrong frame, forgetting the complement is reverse, RNA vs DNA) silently produce wrong proteins. This skill does the standard sequence operations correctly and explains the workflow for the heavier tasks (alignment, BLAST) that need real tools/databases.

## What this skill produces

**Chat-first.** Default: the computed result — GC%, reverse-complement, translated protein, ORFs, composition — with the convention stated (strand, frame, code). The `seqtools.py` helper does the core operations. For alignment/BLAST/large genomes, give the workflow and point to the right tool (Biopython, BLAST, BWA), since those need databases/packages.

## When to use

- "GC content / base composition of this sequence?"
- "Reverse-complement this DNA." / "What's the complementary strand?"
- "Transcribe / translate this." / "What protein does this code for?"
- "Find the ORFs / reading frames."
- "Parse this FASTA." / "How long / how many sequences?"
- "How do I align these / BLAST this?" (workflow guidance)

## The conventions you must keep straight

- **DNA bases** A,T,G,C; **RNA** uses U instead of T. Pairing: A–T (A–U in RNA), G–C.
- **Reverse-complement:** complement each base **and reverse** the order (because strands are antiparallel, read 5′→3′). Complement-without-reversing is a classic bug. `seqtools.py revcomp`.
- **Transcription** (DNA→mRNA): the mRNA matches the *coding* strand with T→U (i.e. it's the complement of the template strand). Be explicit about which strand you're given.
- **Translation** (mRNA→protein): read **codons** (triplets) in a fixed **reading frame** using the genetic code; start at AUG (Met), stop at UAA/UAG/UGA. There are **3 forward frames** (offset 0/1/2) and 3 reverse frames — 6 total.
- **ORF (open reading frame):** a stretch from a start codon to an in-frame stop, long enough to be a plausible gene. Scan all 6 frames.

## Core operations

- **GC content** = (G+C)/length × 100 — relates to melting temp/stability and varies by organism.
- **Composition / k-mers** — base or codon frequency counts; useful for QC and motif spotting.
- **Length & N50** for assemblies; **FASTA** = `>header` line then sequence line(s); parse into (id, seq) records.
- **Tm (rough)** for short primers: Wallace rule 2(A+T)+4(G+C) °C (≤14 bp); use nearest-neighbor models for real primer design.

## Heavier workflows (use real tools)

- **Pairwise/multiple alignment** — Needleman-Wunsch (global), Smith-Waterman (local); in practice use Biopython/EMBOSS/MAFFT. Explain scoring (match/mismatch/gap) but don't hand-roll for real data.
- **BLAST** — search a sequence against a database to find homologs; web BLAST or local. Workflow: pick the right BLAST (blastn/blastp/blastx), database, and read E-value/identity/coverage.
- **Read mapping / variant calling / assembly** — BWA/bowtie, samtools, GATK, SPAdes; these are pipelines, not one-liners. Mention `reproducible-research` for capturing the environment/versions.
- **Biopython** is the standard library for parsing (FASTA/GenBank), translation tables, and I/O — recommend it for anything beyond the basics.

## The toolkit

`scripts/seqtools.py` (stdlib; standard genetic code):
```bash
python3 scripts/seqtools.py gc ATGCGCTA
python3 scripts/seqtools.py revcomp ATGC                  # -> GCAT
python3 scripts/seqtools.py transcribe ATGGAA             # DNA coding -> mRNA
python3 scripts/seqtools.py translate AUGGAAUAA           # mRNA -> protein (or DNA, auto)
python3 scripts/seqtools.py orf ATGAAATAGCC --min-aa 1    # ORFs in all 6 frames
python3 scripts/seqtools.py composition ATGCATGC
python3 scripts/seqtools.py fasta seqs.fa                 # parse + summarize
```
Stdlib only; sequences inline or via FASTA.

## Chat output format

```
**Sequence analysis** (DNA, 12 bp)

Seq: ATGGAATTCTAA
GC content: 33.3% · length 12 bp
Reverse-complement: TTAGAATTCCAT
Translation (frame 0, as mRNA AUG…): Met-Glu-Phe-Stop → "MEF*"
ORF found: ATG…TAA, 3 aa (MEF). EcoRI site (GAATTC) present.
Convention: read as coding strand, frame 0, standard genetic code.
```

## Workflow

1. **Identify the molecule** (DNA/RNA/protein) and what strand/frame you're given — state assumptions.
2. **Run the operation** (`seqtools.py`): GC/composition, revcomp, transcribe, translate, or ORF scan (all 6 frames).
3. **Keep conventions explicit** (5′→3′, which frame, standard vs alt code).
4. **For alignment/BLAST/pipelines**, lay out the workflow and the proper tool (Biopython/BLAST/BWA) — don't fake results that need a database.
5. **Deliver** result + stated conventions; route stats to `statistical-analysis`, chemistry (e.g. molecular weight) to `chemistry`, papers to `arxiv`, reproducibility to `reproducible-research`.

## Key pitfalls

- **Complement without reversing.** Reverse-complement = complement AND reverse; forgetting the reverse gives the wrong strand.
- **Wrong reading frame.** A 1-base offset changes every codon — scan all 3 (or 6) frames; don't assume frame 0.
- **DNA vs RNA (T vs U).** Translate from mRNA; mixing T/U or skipping transcription mis-codes.
- **Ignoring strandedness.** Genes can be on either strand — check both (reverse frames) for ORFs.
- **Hand-rolling alignment/BLAST for real data.** Use established tools + databases; don't fabricate E-values or homology.
- **Off-by-one / 0- vs 1-based coordinates.** Bio tools differ (BED 0-based, GFF 1-based) — state which.
- **Tiny-ORF noise.** Set a minimum length; a 2-codon "ORF" is almost certainly noise.

## Quick reference

- DNA A-T/G-C; RNA U for T. Reverse-complement = complement + reverse (5′→3′).
- Transcription DNA→mRNA (T→U, complement of template). Translation: codons in a frame; start AUG, stop UAA/UAG/UGA.
- 6 reading frames (3 fwd + 3 rev); ORF = AUG→in-frame stop, above a min length.
- GC% = (G+C)/len. Primer Tm rough = 2(A+T)+4(G+C). FASTA = `>id` + sequence.
- Real work → Biopython (parse/translate), BLAST (homology), BWA/samtools (mapping); capture env via reproducible-research.
