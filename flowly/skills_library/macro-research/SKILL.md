---
name: macro-research
description: "Research and interpret the macro picture — CPI/inflation, jobs & unemployment, GDP, central-bank policy (Fed/ECB/etc.), the yield curve, rates, PMIs, and the dollar — then write a tight macro note. Reads releases vs consensus, frames the regime, and connects it to markets. Use when the user asks about inflation, the Fed, rates, recession odds, the economy, or wants a macro briefing."
metadata: {"flowly":{"emoji":"🌐","tags":["finance","macro","economics","inflation","cpi","fed","interest-rates","yield-curve","gdp","central-banks"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["economic-indicators","credit-analysis","portfolio-review","deep-research","finance"]}}
---

# Macro Research — Read the Economy, Write the Note

Macro is about **regime and direction**, not point forecasts. The job is to take the latest data, place it against consensus and the trend, infer what it means for policy and markets, and say it clearly. A good macro note is opinionated, dated, and falsifiable — not a list of numbers.

## What this skill produces

**Chat-first.** Default: a concise macro briefing — the regime call up top, then the few data points that support it, then the market implication and what to watch. Offer a longer written note (`.md`/`.pdf`) when the user wants a full macro report or a recurring weekly.

You are interpreting, not just reporting. "CPI came in at 3.1%" is data; "core inflation is sticky at 3.1%, above the Fed's comfort zone, which pushes the first cut back toward Q3 and keeps the front end bid" is research.

## When to use

- "What's going on with inflation / the economy / rates?"
- "What did the Fed do / signal?" / "When's the next cut?"
- "Read me this morning's CPI / jobs / GDP print."
- "What's the yield curve saying?" / "Recession odds?"
- "Write me a macro note / weekly macro briefing."
- "How does this macro backdrop affect \<asset/portfolio\>?"

## The core data, and how to read each

| Indicator | Cadence | The read |
|---|---|---|
| **CPI / PCE** | Monthly | **Core** (ex food/energy) and the MoM trend matter more than headline YoY. PCE is the Fed's preferred gauge. Watch services-ex-housing ("supercore"). |
| **Jobs (NFP)** | Monthly | Payrolls + **unemployment rate** + **wage growth** (avg hourly earnings) + participation. Wages = inflation feedback. Watch revisions. |
| **GDP** | Quarterly | Real growth, and the composition (consumer vs investment vs inventories/net exports). Final sales > headline for signal. |
| **Fed / central banks** | ~8/yr | The decision, the **statement language diff**, the **dot plot**, and the press conference tone. The path matters more than the single move. |
| **Yield curve** | Daily | 2s10s and 3m10y slope; **inversion** = classic recession signal; **bull/bear steepening/flattening** tells you the driver. |
| **PMIs / ISM** | Monthly | >50 expansion, <50 contraction; leading-ish. New orders & prices-paid sub-indices lead. |
| **Retail sales / consumer** | Monthly | The consumer is ~70% of US GDP; control group is the cleaner signal. |
| **Jobless claims** | Weekly | Highest-frequency labor read; trend in continuing claims flags turning points. |
| **The dollar (DXY) / financial conditions** | Daily | Tightening/easing transmission; risk-on/off barometer. |

## The interpretive framework

### 1. Surprise vs consensus (not just the level)
Markets trade the **delta from expectations**. Always frame a print as actual vs consensus vs prior. A "high" number that came in below expectations can rally bonds. State the consensus and its source.

### 2. Trend and second derivative
One print is noise. Is inflation *decelerating* even if still high? Is job growth *slowing* from a strong level? The direction and rate-of-change drive the regime call more than any single month.

### 3. The regime
Place the economy on the growth × inflation grid, because it dictates what works:
- **Goldilocks** (growth up, inflation down) — risk-on, equities + credit.
- **Reflation** (growth up, inflation up) — cyclicals, commodities, steeper curve.
- **Stagflation** (growth down, inflation up) — hardest; real assets, defensives.
- **Deflation/slowdown** (growth down, inflation down) — duration/bonds, quality.

### 4. The policy reaction function
Translate data → central-bank path. Is the data pushing the next move (cut/hike/hold) earlier or later? Reference market-implied pricing (fed funds futures / OIS) and whether the data confirms or fights it. The **gap between the Fed's dots and market pricing** is itself a trade.

### 5. Market transmission
Close the loop: what does this mean for the **front end vs long end (curve)**, **the dollar**, **equities (multiple vs earnings)**, **credit spreads**, **commodities**? A note that stops at "inflation is sticky" without "so the curve flattens / front end stays anchored" is half a note.

## Data sourcing

- **Use primary releases** and cite them with date: BLS (CPI, jobs), BEA (GDP, PCE), the Fed (FOMC statement, SEP/dots, minutes), Treasury (yields), ISM/S&P Global (PMIs), the relevant central bank for non-US.
- **FRED** (St. Louis Fed) is the canonical free time-series source — link the series. (`fred.stlouisfed.org/series/<ID>`, e.g. CPIAUCSL, UNRATE, GDPC1, DGS10, DGS2, T10Y2Y.)
- For multi-source synthesis or a current read of *expectations/positioning*, hand off to the `deep-research` skill (web fan-out + verification).
- **Never invent a print or a consensus.** If a number isn't confirmed, say so. Stamp every figure with its release date — macro data is revised.
- Pair recurring quantitative series work with the `economic-indicators` skill (surprise vs consensus tables, release calendar, regime tagging).

## Chat output format

```
**Macro briefing — 2026-06-05**

🧭 Regime: late-cycle, disinflating slowly. Soft-landing base case intact.

📉 Inflation: core PCE 2.8% YoY (MoM +0.2%, cooling); supercore still firm.
💼 Labor: NFP +145k (below 175k est) — softening but not breaking; U-rate 4.2%.
🏛️ Fed: on hold; dots imply 2 cuts '26. Market prices first cut Sept.
📈 Curve: 2s10s +12bps (re-steepening as front end prices cuts).

➡️ Implication: front end anchored, belly rich; USD soft; supports duration
   and quality equities. Risk: a hot services print pushes cuts to Q4.
🔭 Watch: next CPI (Jun 11), jobless-claims trend, FOMC Jun 17.
```

Lead with the **regime call**, keep it to one screen, every number dated.

## Workflow

1. **Scope the question:** a single print, the policy path, the regime, or a full note?
2. **Pull the relevant releases** (cite + date); get consensus for any print being judged.
3. **Frame each:** actual vs consensus vs trend; second derivative.
4. **Synthesize the regime** on the growth × inflation grid.
5. **Map the policy reaction** and compare to market-implied pricing.
6. **Transmit to markets** (curve / FX / equities / credit / commodities).
7. **Deliver** the briefing with a dated "watch" list; offer the long-form note.

## Key pitfalls

- **Reporting levels, not surprises.** Markets trade vs expectations — always include consensus.
- **Over-reading one print.** A single month is noise; lead with the trend and the second derivative.
- **Ignoring revisions.** Jobs and GDP get revised hard; flag prior-period revisions.
- **Headline over core.** Energy-driven headline moves mislead on the underlying trend.
- **Stopping at the data.** No regime call, no market implication = not research.
- **Fighting the market without saying so.** If your read differs from fed-funds futures, name the gap; don't pretend it isn't there.
- **Stale numbers.** Every figure dated; macro data is the most-revised data there is.

## Quick reference

- Fed targets **2% PCE**; core PCE is the gauge that matters most.
- Yield-curve inversion (2s10s / 3m10y < 0) has preceded most US recessions — necessary-ish, not sufficient; the *un*-inversion often coincides with the downturn.
- "Supercore" = core services ex-housing — the stickiest, most labor-sensitive inflation.
- FRED series: CPIAUCSL (CPI), PCEPILFE (core PCE), UNRATE (U-rate), PAYEMS (payrolls), GDPC1 (real GDP), DGS10/DGS2 (10y/2y), T10Y2Y (2s10s), FEDFUNDS, DTWEXBGS (USD).
- Growth × inflation grid maps the regime to what works — anchor the note to it.
