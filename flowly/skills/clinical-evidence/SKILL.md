---
name: clinical-evidence
description: "Appraise and synthesize clinical evidence — frame a PICO question, place studies on the evidence hierarchy, critically appraise an RCT/cohort/meta-analysis (bias, validity), interpret effect measures (RR, OR, ARR, NNT, hazard ratio, CI, p), and weigh guideline recommendations (GRADE). Use when the user asks whether a treatment/intervention works, to evaluate a clinical study, what the evidence says, to interpret RR/NNT/etc., or to weigh medical guidelines."
metadata: {"flowly":{"emoji":"🩺","tags":["health","clinical-evidence","evidence-based-medicine","appraisal","rct","meta-analysis","pico","grade"],"requires":{"bins":[]},"category":"health","related_skills":["medical-literature-review","statistical-analysis","scientific-peer-review","literature-review"]}}
---

# Clinical Evidence — What the Studies Actually Support

Evidence-based medicine is the disciplined weighing of *how good* the evidence is, not just *what it says*. A confident conclusion from a small, biased trial is worse than an honest "uncertain." This skill frames the question, ranks the evidence, appraises the study's validity, and interprets the effect measures so the answer reflects the real strength of support — with appropriate humility.

> **Not medical advice.** This is informational appraisal of published evidence to support understanding and clinical reasoning — it is **not** diagnosis, treatment advice, or a substitute for a qualified clinician who knows the individual patient. Always direct real medical decisions to a professional.

## What this skill produces

**Chat-first.** Default: a structured appraisal — the PICO question, evidence level, the key validity concerns, the effect size with its CI and a plain-language interpretation (e.g. NNT), and a calibrated bottom line ("good evidence", "weak/mixed", "insufficient"). Offer a fuller evidence table for a formal review.

## When to use

- "Does \<treatment\> work for \<condition\>?" / "What does the evidence say?"
- "Evaluate / appraise this study." / "Is this a good trial?"
- "Interpret this RR / OR / NNT / hazard ratio / CI."
- "What do the guidelines recommend, and how strong is it?"
- "Is this claim supported by evidence?"

## Step 1 — Frame with PICO

- **P**opulation/patient, **I**ntervention, **C**omparison, **O**utcome. A precise PICO sharpens the search and keeps the answer on-question. Note the outcome type (mortality vs surrogate marker — a drug that improves a lab value but not survival is weaker evidence).

## Step 2 — Place it on the evidence hierarchy

From strongest to weakest (for therapy questions):
1. **Systematic review / meta-analysis** of RCTs (highest — if well-conducted).
2. **Randomized controlled trial (RCT)** — randomization controls confounding.
3. **Cohort** (prospective > retrospective).
4. **Case-control.**
5. **Case series / case reports.**
6. **Expert opinion / mechanistic reasoning** (lowest).

Higher isn't automatically better — a sloppy meta-analysis of bad trials (garbage in, garbage out) can be weaker than one excellent RCT. Match the design to the question (RCT for therapy; cohort for prognosis/harm where RCTs are unethical; cross-sectional for prevalence).

## Step 3 — Critically appraise (is it valid?)

- **Randomization & allocation concealment** (RCT) — done properly, hidden from enrollers?
- **Blinding** — patients, clinicians, assessors (reduces performance/detection bias).
- **Attrition** — how much dropout, handled how (intention-to-treat is the rigorous analysis; per-protocol can flatter)?
- **Sample size & power** — was it powered to detect a meaningful effect? Small trials exaggerate.
- **Confounding** (observational) — measured and adjusted? Unmeasured confounding limits causal claims.
- **Outcomes** — patient-important vs surrogate; pre-registered vs cherry-picked; composite endpoints hiding a weak component.
- **Conflicts of interest / funding** and **publication bias** (negative trials go unpublished — check funnel plots in meta-analyses).
Tools: RoB 2 (RCTs), Newcastle-Ottawa (observational). (→ `scientific-peer-review` for deeper methodology critique.)

## Step 4 — Interpret the numbers honestly

- **Relative measures (RR, OR, HR)** sound big; **absolute measures (ARR)** tell the real-world impact. A "50% reduction" of a 2%→1% risk is a 1 percentage-point ARR — much less impressive. Always get the absolute effect.
- **NNT (number needed to treat) = 1/ARR** — how many patients you treat for one to benefit. NNH is the harm equivalent. The most clinically intuitive measure.
- **Confidence interval** — the plausible range; a CI for RR/OR crossing 1 (or for a difference crossing 0) means non-significant. The CI's *width* shows precision; lean on it over the bare p-value.
- **p-value** — evidence against the null, not effect size or importance; significant ≠ clinically meaningful, and non-significant ≠ "no effect" (could be underpowered).
- **Statistical vs clinical significance** — a tiny, precisely-measured effect can be significant but useless. Judge the magnitude against what matters to patients.

## Step 5 — Grade the bottom line (GRADE)

Rate overall certainty (High / Moderate / Low / Very low), downgrading for risk of bias, inconsistency, indirectness, imprecision, and publication bias; upgrading for large effects/dose-response. Separate the **certainty of evidence** from the **strength of a recommendation** (which also weighs values, harms, cost). State the certainty plainly.

## Chat output format

```
**Does drug X reduce events in condition Y?**

PICO: adults with Y · drug X · vs placebo · major events at 1 yr.
Evidence: 1 large double-blind RCT (n=4,000) + a meta-analysis of 5 RCTs.
Appraisal: low risk of bias (concealed, blinded, ITT); industry-funded (note).
Effect: RR 0.75 (95% CI 0.64–0.88) — relative 25% ↓. ARR 3% (12%→9%) → NNT ≈ 33.
Read: consistent, moderate-to-high certainty that X reduces events; absolute
benefit modest (treat ~33 for 1 to benefit). Weigh vs harms/cost for a given patient.
⚠️ Not medical advice — individual decisions belong with a clinician.
```

## Workflow

1. **PICO** the question; identify the outcome type (hard vs surrogate).
2. **Find & rank** the best available evidence (→ `medical-literature-review` for the search).
3. **Appraise validity** (bias, blinding, attrition, confounding, COI, publication bias).
4. **Interpret effects** in absolute terms (ARR/NNT), with CIs; separate statistical from clinical significance.
5. **GRADE the certainty**; state a calibrated bottom line (incl. "insufficient" when true).
6. **Deliver** with the not-medical-advice guard; route the literature search to `medical-literature-review`, stats depth to `statistical-analysis`, methodology critique to `scientific-peer-review`.

## Key pitfalls

- **Relative over absolute risk.** RR/OR exaggerate impact; always compute ARR/NNT for the real magnitude.
- **Treating significance as importance.** p<0.05 ≠ clinically meaningful; a CI shows the plausible effect — read it.
- **"No significant difference" = "no effect."** Could be underpowered — distinguish absence of evidence from evidence of absence.
- **Surrogate endpoints as if patient outcomes.** A lab-value improvement isn't proven mortality/morbidity benefit.
- **Hierarchy worship.** A poorly-run RCT/meta-analysis can be weaker than a strong one — appraise quality, don't just count the design.
- **Ignoring bias/COI/publication bias.** Funding and unpublished negatives skew the apparent effect.
- **Overconfidence.** Calibrate the conclusion to the evidence; "uncertain/insufficient" is a valid, important answer.
- **Giving medical advice.** Appraise evidence; send individual decisions to a clinician.

## Quick reference

- PICO frames the question; rank on the evidence hierarchy (meta-analysis/RCT > cohort > case-control > series > opinion) but weight by quality.
- Appraise: randomization/concealment, blinding, attrition/ITT, power, confounding, COI, publication bias (RoB 2 / Newcastle-Ottawa).
- Effects: relative (RR/OR/HR) vs **absolute (ARR)**; **NNT = 1/ARR**; CI > p; statistical ≠ clinical significance.
- GRADE certainty (High→Very low); separate evidence certainty from recommendation strength.
- Search → medical-literature-review; stats → statistical-analysis; method critique → scientific-peer-review. Not medical advice.
