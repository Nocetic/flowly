---
name: market-sizing
description: "Size a market — TAM / SAM / SOM — using both top-down (from industry figures) and bottom-up (from units × price) approaches, reconcile them, state assumptions, and sanity-check. Handles market-entry estimates, opportunity sizing, and Fermi-style estimation. Includes a stdlib sizing calculator. Use when the user asks how big a market is, for TAM/SAM/SOM, to size an opportunity or a market-entry, or a revenue-potential estimate."
metadata: {"flowly":{"emoji":"🌐","tags":["business","market-sizing","tam-sam-som","estimation","strategy","gtm","opportunity"],"requires":{"bins":["python3"]},"category":"business","related_skills":["business-case","competitor-analysis","pricing-strategy","finance"]}}
---

# Market Sizing — TAM/SAM/SOM, Two Ways, Reconciled

Market sizing is structured estimation under uncertainty. The credibility comes not from a precise number but from **transparent assumptions** and **two independent methods that roughly agree**. A single top-down number pulled from a report is a guess; a bottom-up build that lands near it is a defensible estimate.

## What this skill produces

**Chat-first.** Default: TAM/SAM/SOM with the build shown (the assumptions and arithmetic), top-down vs bottom-up reconciled, and a sanity check. The `sizing.py` helper does both builds. Keep numbers honest — present a range, not false precision.

## When to use

- "How big is the market for \<X\>?" / "What's the TAM/SAM/SOM?"
- "Size this opportunity / market-entry."
- "What's the revenue potential of \<product\>?"
- "Estimate the number of \<X\> in \<region\>." (Fermi estimation)

## The three layers

- **TAM (Total Addressable Market):** total demand if you had 100% share of everyone who could possibly use the product. The whole pie.
- **SAM (Serviceable Available Market):** the slice your business model/geography/segment can actually serve (e.g. only English-speaking SMBs, only your region).
- **SOM (Serviceable Obtainable Market):** the realistic share you can win in a defined period given competition, GTM, and capacity. The number that should drive planning — TAM is for context, SOM is for the plan.

A common error is selling the **TAM as the opportunity** ("it's a $50B market!"). Investors and operators care about SOM and the path to it.

## Two methods — do both

**Top-down:** start from a published total and narrow by segment percentages.
> Industry size → % relevant geography → % relevant segment → % addressable = SAM.
Fast, but only as good as the source (cite it + date it) and the slicing assumptions. Prone to over-optimism.

**Bottom-up (preferred, harder to fool yourself):** build from units.
> (# potential customers) × (adoption/penetration) × (price or spend per customer) = market value.
Grounded in real, checkable inputs (customer counts, ACV). This is the more credible build — top-down is the cross-check.

**Reconcile:** if the two are within ~2× of each other, you have a defensible range. If they differ wildly, an assumption is wrong — investigate (usually the top-down % slices or the bottom-up penetration).

## Sanity checks

- **Per-customer reasonableness:** does the implied spend per customer match reality?
- **Penetration realism:** a new entrant winning 30% of SOM in year 1 is fantasy; ground it in comparable adoption curves.
- **Don't double-count** segments; don't multiply already-overlapping percentages.
- **State the time frame** (annual market value? SOM over 3 years?).
- **Cite sources** for top-down inputs with dates; flag where a number is a guess.

## The helper

`scripts/sizing.py` (stdlib):
```bash
# bottom-up: customers × penetration × price
python3 scripts/sizing.py bottomup --customers 2000000 --penetration 0.05 --price 1200
# top-down: total, then fractional slices
python3 scripts/sizing.py topdown --total 50e9 --slices 0.4 0.5 0.3
# reconcile two estimates
python3 scripts/sizing.py reconcile --a 1.2e9 --b 0.9e9
```

## Chat output format

```
**Market size — SMB scheduling SaaS (US)**

Bottom-up (SAM): 6M US SMBs × 8% relevant × $600/yr = **$288M/yr**
Top-down (SAM): $12B US SMB software × ~2.5% scheduling = ~$300M/yr ✅ (agrees ±5%)
SOM (3-yr, ~3% of SAM given competition): ~$9M/yr realistic target.
TAM (global, all SMBs): ~$4B (context only).

Assumptions: 8% have the pain (survey-based, ±3pp); $600 ACV (≈ comp pricing).
Range: SAM $280–320M. Drives plan off SOM, not TAM.
```

## Workflow

1. **Define the product, customer, and geography** precisely (vague scope = meaningless number).
2. **Bottom-up build** (customers × penetration × price) — the primary, checkable estimate.
3. **Top-down cross-check** from a cited industry figure narrowed by segment.
4. **Reconcile** (`reconcile`) — within ~2×? If not, fix the bad assumption.
5. **Derive SOM** realistically (share over a time frame, grounded in comparables).
6. **State assumptions + range + sources**; deliver. Feed SOM into `business-case`; pricing inputs ↔ `pricing-strategy`; competition ↔ `competitor-analysis`.

## Key pitfalls

- **Selling TAM as the opportunity.** Plan off SOM; TAM is context. "It's a huge market" isn't a strategy.
- **One method only.** A lone top-down number is a guess — always cross-check bottom-up.
- **Unsourced / stale top-down figures.** Cite and date; report numbers drift and inflate.
- **Fantasy penetration/share.** Ground adoption and SOM share in comparable curves, not hope.
- **Multiplying overlapping percentages / double-counting** segments — inflates the result.
- **False precision.** "$287.4M" implies accuracy you don't have — give a range.
- **No time frame.** Annual vs cumulative, year-1 vs 3-year SOM — state it.

## Quick reference

- TAM (everyone) ⊃ SAM (who you can serve) ⊃ SOM (who you'll realistically win). Plan off SOM.
- Bottom-up = customers × penetration × price (preferred). Top-down = industry total × segment %.
- Reconcile the two within ~2×; if not, an assumption is wrong.
- State assumptions, time frame, sources; give a range, not a point.
- SOM → business-case; price → pricing-strategy; rivals → competitor-analysis.
