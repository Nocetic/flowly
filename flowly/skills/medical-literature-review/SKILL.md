---
name: medical-literature-review
description: "Run a medical/clinical literature review — build a PubMed/database search strategy (MeSH, Boolean, filters), screen and select studies (PRISMA-style inclusion/exclusion), assess risk of bias, extract data into an evidence table, and synthesize findings across studies. Use when the user wants a literature search on a clinical topic, a systematic/scoping review, to find and summarize medical studies, build an evidence table, or screen papers. Pairs with clinical-evidence (appraisal) and literature-review (general)."
metadata: {"flowly":{"emoji":"📚","tags":["health","literature-review","pubmed","systematic-review","prisma","evidence-table","search-strategy"],"requires":{"bins":[]},"category":"health","related_skills":["clinical-evidence","literature-review","scientific-peer-review","arxiv"]}}
---

# Medical Literature Review — Find It Systematically, Synthesize It Honestly

A medical literature review is only trustworthy if it's **reproducible and unbiased**: a documented search strategy, explicit inclusion criteria, and risk-of-bias assessment — so the conclusion reflects the body of evidence, not a convenient subset. This skill builds the search, screens transparently (PRISMA), extracts into an evidence table, and synthesizes — while appraisal of each study lives in `clinical-evidence`.

> **Not medical advice.** This supports research and understanding of the literature, not clinical decisions for an individual. Verify against primary sources and current guidelines; send patient-specific decisions to a clinician.

## What this skill produces

**Chat-first.** Default: a search strategy and/or a synthesized summary — what the body of evidence shows, the key studies in an evidence table, consistency/heterogeneity, and gaps — with a calibrated bottom line. Offer a full evidence table or PRISMA-style writeup as a file. Cite + date everything.

## When to use

- "Find the literature / studies on \<clinical topic\>."
- "Build a PubMed search for \<question\>." / "What's the search strategy?"
- "Do a systematic / scoping review of \<topic\>."
- "Summarize what the studies say about \<intervention/condition\>."
- "Screen these papers / build an evidence table."

## Step 1 — Frame & scope (PICO + review type)

- **PICO** the question (Population, Intervention, Comparison, Outcome) — it drives the search terms and inclusion criteria. (→ `clinical-evidence`.)
- **Review type:** *systematic* (exhaustive, protocol-driven, on a tight question), *scoping* (map the breadth of a broad area), or *narrative* (expert overview). Set expectations — a true systematic review is a large, protocol-registered (PROSPERO) effort.

## Step 2 — Build the search strategy (reproducible)

- **Sources:** PubMed/MEDLINE (primary), Embase, Cochrane Library (for trials/reviews), plus Google Scholar and trial registries (ClinicalTrials.gov) to catch grey/unpublished literature.
- **MeSH + free text:** combine controlled vocabulary (MeSH terms) with keyword synonyms for each PICO concept; OR within a concept, AND across concepts.
  > (term1 OR term2 OR "MeSH term"[Mesh]) AND (interventionA OR interventionB) AND (outcome...)
- **Filters:** study type (RCT, meta-analysis), date range, species (humans), language — but document them, since filters can exclude relevant work.
- **Document the exact query, database, and date run** — that's what makes it reproducible. (→ `arxiv` for preprints / biomedical preprint servers like medRxiv/bioRxiv.)

## Step 3 — Screen transparently (PRISMA flow)

1. **Identification** — total records found (per source), deduplicate.
2. **Screening** — title/abstract against pre-set **inclusion/exclusion criteria** (population, design, outcome, language, date). Exclude with reasons.
3. **Eligibility** — full-text review of the remainder; record exclusion reasons.
4. **Included** — the final set.
Report the numbers at each step (the PRISMA flow diagram). Pre-specifying criteria prevents cherry-picking; ideally two reviewers screen independently.

## Step 4 — Extract into an evidence table

One row per study, columns for: author/year, design, n, population, intervention/comparison, key outcomes + effect sizes (with CIs), follow-up, risk of bias, funding/COI. A clean table makes patterns (and inconsistencies) visible and is the backbone of the synthesis.

## Step 5 — Assess risk of bias & synthesize

- **Risk of bias** per study (RoB 2 for RCTs, Newcastle-Ottawa for observational); the synthesis must weight higher-quality studies more. (Per-study appraisal → `clinical-evidence`.)
- **Synthesis:** Do studies agree? Quantify heterogeneity if meta-analyzing (I²); explore why they differ (population, dose, outcome definition). Note **publication bias** (funnel plot asymmetry; missing negative trials).
- **Bottom line + gaps:** state the overall direction and certainty, and what's missing (under-studied populations, short follow-up, surrogate outcomes) — gaps are a key output.

## Chat output format

```
**Lit review — does intervention X help condition Y?**

Search (PubMed, run 2026-06-08):
  ("Y"[Mesh] OR "Y") AND (X OR X-class) AND (RCT OR meta-analysis) — humans, 2015–
  → 142 records → 38 after title/abstract → 9 full-text → 6 included.

Evidence table (6 studies):
| Study | Design | n | Effect (outcome) | RoB |
| Smith'22 | RCT | 800 | RR 0.78 (0.66–0.92) | low |
| ... |

Synthesis: 5/6 favor X (consistent); 1 null (smaller, different population).
Pooled direction favors X, moderate certainty. Gap: no >2yr follow-up; mostly
high-income settings. ⚠️ Not medical advice — verify vs current guidelines.
```

## Workflow

1. **PICO + review type**; set inclusion/exclusion criteria up front.
2. **Build & document** the search (MeSH + free text, sources, filters, date).
3. **Screen PRISMA-style** with counts and exclusion reasons; deduplicate.
4. **Extract** included studies into an evidence table.
5. **Assess RoB** (→ `clinical-evidence`) and **synthesize** (consistency, heterogeneity, publication bias, gaps).
6. **Deliver** strategy + table + synthesis + bottom line, cited and dated, with the not-medical-advice guard. General (non-medical) reviews → `literature-review`; methodology critique → `scientific-peer-review`.

## Key pitfalls

- **Unsystematic / undocumented search.** A non-reproducible search invites bias — record the exact query, source, and date.
- **Cherry-picking studies.** Pre-specify inclusion criteria and screen transparently; report what you excluded and why.
- **Ignoring grey/unpublished literature.** Negative trials go unpublished — check registries and consider publication bias.
- **No risk-of-bias weighting.** Treating all studies equally lets bad ones drive the conclusion — appraise and weight.
- **Synthesizing heterogeneous studies as if identical.** Different populations/outcomes/doses can't be lumped uncritically; explore heterogeneity.
- **Fabricating citations or effect sizes.** Never invent a study, PMID, or number — cite primary sources; mark uncertainty.
- **Overstating the conclusion.** Calibrate to the evidence quality and note the gaps; "insufficient evidence" is a real finding.
- **Stale evidence.** Date the search; guidelines and trials update.

## Quick reference

- PICO → search terms + inclusion criteria. Review type: systematic / scoping / narrative.
- Search: MeSH + free-text synonyms, OR within concept / AND across; PubMed+Embase+Cochrane+registries; document query+date.
- PRISMA: identification → screening (title/abstract) → eligibility (full text) → included, with counts + reasons.
- Evidence table: study, design, n, population, effect+CI, follow-up, RoB, COI.
- Synthesize with RoB weighting; check heterogeneity (I²) and publication bias; state gaps + calibrated certainty.
- Appraisal → clinical-evidence; general reviews → literature-review. Cite + date; not medical advice.
