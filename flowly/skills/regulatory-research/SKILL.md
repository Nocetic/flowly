---
name: regulatory-research
description: "Research and summarize a regulation or regulatory question — what the rule requires, who it applies to (jurisdiction + trigger), the concrete obligations, deadlines, penalties, enforcement, and recent/upcoming changes — with rigorous source-and-date citation for every claim. Use when the user asks what a law/regulation requires, whether it applies to them, compliance obligations, penalties, or wants a regulatory summary across jurisdictions. Pair with deep-research for broad multi-source sweeps."
metadata: {"flowly":{"emoji":"⚖️","tags":["legal","regulatory","compliance","regulation","jurisdiction","obligations","research"],"requires":{"bins":[]},"category":"legal","related_skills":["privacy-review","contract-review","policy-drafting","deep-research"]}}
---

# Regulatory Research — What the Rule Says, Who It Binds, and What to Do

Regulatory research is high-stakes because the answer drives real compliance decisions — and regulations are **jurisdiction-specific, time-sensitive, and frequently amended**. The cardinal discipline is therefore **citation**: every factual claim carries a source and a date, because an uncited regulatory "fact" is worse than useless — it's a liability. The output is a defensible summary, not a confident-sounding guess.

> **Not legal advice.** This is informational research to help the user understand a regulation and frame questions for counsel — not a legal opinion or a compliance determination. Laws change and vary by jurisdiction; verify against the primary source and consult a qualified professional for decisions.

## What this skill produces

**Chat-first.** Default: a structured summary — what the rule is, applicability (does it apply to *you*?), the key obligations, deadlines, penalties, and what's changed/changing — each line cited and dated. Offer a full file for a multi-regulation comparison or a compliance-obligation matrix.

## When to use

- "What does \<regulation\> require?" / "Summarize \<law\>."
- "Does \<regulation\> apply to us?" (jurisdiction/threshold/trigger questions)
- "What are the compliance obligations / deadlines / penalties for X?"
- "How does \<rule\> differ between \<jurisdiction A\> and \<B\>?"
- "What's changing in \<area\> regulation?" / "Any new rules on X?"
- "What do we need to do to comply with \<requirement\>?"

## The non-negotiable: cite everything, date everything

- **Every claim → a source.** Name the instrument (e.g. "GDPR Art. 33", "CCPA §1798.150", "SEC Rule 10b-5"), and link the **primary source** (the official text/regulator site) over a secondary summary.
- **Date every claim.** State the version/as-of date. Note if a rule is in force, pending, or recently amended — and the effective date.
- **Distinguish primary vs secondary.** Statute/regulation/official guidance > regulator FAQ > law-firm summary > news. Flag when you're relying on a secondary read.
- **Flag uncertainty explicitly.** "The text is ambiguous on X; guidance suggests Y (cite), but verify." Never paper over a gap with confident phrasing.
- **Never invent a citation, section number, penalty figure, or deadline.** If you can't source it, say so. A fabricated reg citation is the worst possible failure mode here.

For broad, multi-source gathering, use `deep-research` (fan-out + adversarial verification), then bring the findings into this skill's structured, cited format.

## The structure of a regulatory summary

1. **What it is** — the regulation's name, the body that issues/enforces it, and its purpose in one line.
2. **Applicability (the "does it apply to me" test)** — the *triggers*: jurisdiction (where are the users/activity/entity?), thresholds (revenue, headcount, data volume, sector), and activities covered. This is usually the user's real question — answer it first and precisely.
3. **Key obligations** — what regulated parties must actually *do*, as concrete requirements (not paraphrased platitudes).
4. **Deadlines & timelines** — effective dates, reporting periods, notification windows, transition periods.
5. **Penalties & enforcement** — fines (with the figures + basis), other consequences, the enforcing authority, and how actively it's enforced.
6. **Recent & upcoming changes** — amendments, pending bills, guidance updates — with dates. Regulation is a moving target.
7. **Practical implications** — what this means for the user's situation and the next compliance step (route execution to `policy-drafting`, `privacy-review`, `contract-review`).

## Jurisdiction discipline

- **Applicability follows the activity, not the HQ.** Many regimes are extraterritorial (GDPR, some sanctions, sector rules) — the question is where the users/data/activity are, not where the company sits.
- **Don't blur jurisdictions.** US-federal ≠ US-state ≠ EU ≠ UK ≠ sector-specific. Keep them separate and labeled; a requirement in one is not a requirement in another.
- **For multi-jurisdiction asks, compare side by side** (a matrix), highlighting where obligations diverge.
- **Note federal/state (or EU/member-state) interaction** — which floor applies, which can be stricter.

## Chat output format

```
**<Regulation> — summary** (as of 2026-06-06; verify against primary source)

📌 What: <name>, enforced by <authority>. Purpose: <one line>. [source]
🎯 Applies to you? Triggers: <jurisdiction/threshold/activity>.
   → Likely YES because <…> / NO because <…> / DEPENDS on <fact to confirm>. [source]
📋 Key obligations:
   1. <concrete requirement> [Art./§ + source]
   2. <…> [source]
⏰ Deadlines: <effective date / reporting window / notice period>. [source]
⚖️ Penalties: up to <figure + basis>; enforced by <authority>. [source]
🆕 Changes: <recent/pending amendment + date>. [source]

➡️ Practical: <next step>. ⚠️ Confirm <ambiguous point> with counsel.
```

Every bullet ends with a source; the as-of date leads the whole summary.

## Workflow

1. **Pin the question + jurisdiction(s)** — which rule, which place, whose activity. Clarify if vague (this changes everything).
2. **Find the primary source** (official statute/regulation/regulator guidance); use `deep-research` for the sweep, then verify against primary text.
3. **Answer applicability first** — the triggers, precisely, for the user's facts.
4. **Extract obligations, deadlines, penalties** as concrete, cited items.
5. **Check for changes** — amendments/pending/guidance, with dates.
6. **Translate to practical implications**; route execution to `policy-drafting`/`privacy-review`/`contract-review`.
7. **Deliver** the cited, dated summary; flag ambiguities and what needs counsel.

## Key pitfalls

- **Uncited claims.** The single worst failure. No source + date = don't state it.
- **Fabricated citations / figures / deadlines.** Never invent a section number or a penalty amount; say "couldn't source" instead.
- **Stale information.** Regulations change; always lead with the as-of date and check for amendments.
- **Jurisdiction blur.** Treating a state/EU/sector rule as universal. Label and separate jurisdictions.
- **Answering the wrong question.** The user usually wants "does this apply to *me*" — nail applicability before reciting the rule.
- **Secondary sources as gospel.** Law-firm blogs and news can be wrong or outdated; verify against the primary text and say when you couldn't.
- **False confidence on ambiguity.** Where the rule is genuinely unclear, say so and point to counsel — don't manufacture certainty.
- **Giving a compliance determination.** Inform and frame; the decision and sign-off belong to a qualified professional.

## Quick reference

- **Cite + date every claim**; primary source over secondary; link the official text.
- Lead with **applicability** (jurisdiction × threshold × activity) — usually the real question.
- Applicability follows the **activity/users/data**, not the company HQ (extraterritorial regimes exist).
- Keep jurisdictions **separate and labeled**; compare in a matrix for multi-region asks.
- Capture: what · applies-to · obligations · deadlines · penalties · changes · practical step.
- Never invent a citation, section, figure, or date — "couldn't source it" is the honest answer.
- Broad sweep → `deep-research`; execution → `policy-drafting` / `privacy-review` / `contract-review`; decisions → counsel.
