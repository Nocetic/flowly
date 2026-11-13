---
name: business-case
description: "Build a decision-ready business case — frame the problem, lay out options (including do-nothing), quantify costs/benefits and ROI/payback/NPV, surface risks and assumptions, and end with a clear recommendation memo. Includes a stdlib ROI/payback/NPV calculator. Use when the user needs to justify an investment, decision, or project, asks 'is this worth it', wants an ROI/business case, or a recommendation memo for a spend or initiative."
metadata: {"flowly":{"emoji":"📊","tags":["business","business-case","roi","decision","npv","payback","memo","strategy"],"requires":{"bins":["python3"]},"category":"business","related_skills":["market-sizing","pricing-strategy","finance","dcf-model"]}}
---

# Business Case — Make the Decision Defensible

A business case exists to help someone say yes or no with confidence. It is not a sales pitch — it's an honest weighing of **options against the do-nothing baseline**, with the numbers, the risks, and a clear recommendation. The discipline: quantify what you can, state every assumption, include the option to do nothing, and don't bury the downside.

## What this skill produces

**Chat-first.** Default: a structured case — problem, options, the cost/benefit + ROI/payback, key risks, and a one-line recommendation — readable inline. Offer a full memo (`.md`/`.docx`) or a model (`.xlsx`) for a board-level decision.

## When to use

- "Is \<X\> worth it?" / "Justify this investment / hire / tool / project."
- "Build a business case / ROI for \<initiative\>."
- "Should we build vs buy?" / "Which option should we pick?"
- "Write a recommendation memo for \<spend\>."

## The structure

1. **Problem / opportunity.** What's the pain or the upside, sized? Why now? (Cost of inaction is a real cost — quantify the status quo.)
2. **Options (always ≥3, including do-nothing).** A case with one option is a foregone conclusion, not a decision. Typical: do-nothing (baseline), the proposal, and a cheaper/alternative path (e.g. build vs buy vs partner).
3. **Costs & benefits per option.** One-time + recurring costs; quantified benefits (revenue, cost savings, risk reduction, time saved → money). Separate **hard** (defensible $) from **soft** (morale, brand) benefits — don't pad the ROI with soft ones.
4. **The numbers.** ROI, **payback period** (when cumulative benefit covers cost — the number execs ask for first), and **NPV** (for multi-year, discounting future cash flows; positive NPV = value-creating). For a quick screen, ROI + payback suffice; for big multi-year bets, NPV/IRR (→ `dcf-model`).
5. **Risks & assumptions.** The 3–5 things that, if wrong, change the answer — with likelihood/impact and a mitigation. **State every key assumption** (the case is only as good as them). Add a sensitivity ("if adoption is half, payback slips to 18 months").
6. **Recommendation.** A clear pick with the *why* in one or two sentences, plus what would change your mind.

## The numbers, defined

- **ROI** = (total benefit − total cost) / total cost. Simple, unitless; specify the time window.
- **Payback period** = time until cumulative net benefit ≥ initial cost. Shorter = less risk. The most-asked metric.
- **NPV** = Σ cash_flowₜ / (1+r)ᵗ − initial cost; r = discount rate (hurdle/cost of capital). Positive NPV → creates value. Honest for multi-year because it accounts for the time value of money.
- **IRR** = the discount rate making NPV = 0; compare to the hurdle rate.
Use `scripts/roi.py` to compute these from inputs and avoid arithmetic slips.

## The helper

`scripts/roi.py` (stdlib):
```bash
python3 scripts/roi.py simple --cost 50000 --benefit 80000           # ROI
python3 scripts/roi.py payback --cost 50000 --annual 20000           # payback period
python3 scripts/roi.py npv --rate 0.1 --initial 50000 --flows 20000 20000 20000 20000  # NPV + IRR
```

## Chat output format

```
**Business case — adopt Tool X** (vs do-nothing, vs in-house)

Problem: support team loses ~15 h/wk to manual triage (~$78k/yr loaded).
Options:
  A) Do nothing — $78k/yr ongoing drag.
  B) Tool X — $24k/yr + 1-wk setup. Saves ~12 h/wk (~$62k/yr).
  C) Build in-house — ~$90k upfront + maintenance. Slower.
Numbers (Option B): net benefit ~$38k/yr · ROI 158% yr-1 · payback ~5 months.
Risks: adoption (mitigate w/ training); vendor lock-in; savings est. ±30%.
Sensitivity: even at half the savings, payback < 12 months.

→ Recommend B. Revisit if Tool X price rises >2× or adoption < 50% by month 3.
```

## Workflow

1. **Frame the problem** and quantify the status-quo cost (cost of inaction).
2. **Lay out ≥3 options** including do-nothing.
3. **Cost/benefit each** (hard vs soft separated); compute ROI/payback/NPV with `roi.py`.
4. **List risks + assumptions** with a sensitivity on the key driver.
5. **Recommend** clearly with the trigger that would change the call.
6. **Deliver** inline or as a memo/model; route market size to `market-sizing`, pricing to `pricing-strategy`, deep multi-year modeling to `dcf-model`/`finance`.

## Key pitfalls

- **One option = no decision.** Always include do-nothing and at least one alternative.
- **Ignoring the cost of inaction.** The status quo isn't free — quantify it as the baseline.
- **Padding ROI with soft benefits.** Keep hard $ separate; don't manufacture a return from "morale."
- **Hidden assumptions.** The case lives or dies on them — state each and sensitivity-test the key one.
- **Payback ignored.** Execs ask "when do we get our money back" — always include it.
- **No downside.** A case with no risks reads as a pitch and loses trust. Name the real risks and mitigations.
- **NPV without a sensible discount rate**, or using ROI where time value matters (multi-year) — pick the right metric.

## Quick reference

- Structure: problem (sized) → options (incl. do-nothing) → cost/benefit → ROI/payback/NPV → risks+assumptions → recommendation.
- ROI = (benefit − cost)/cost · Payback = time to recover cost · NPV = Σ CFₜ/(1+r)ᵗ − initial (>0 = good).
- Separate hard vs soft benefits; quantify cost of inaction; sensitivity-test the key assumption.
- Quick screen → ROI + payback; big multi-year bet → NPV/IRR (dcf-model).
