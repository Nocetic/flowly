---
name: economic-indicators
description: "Interpret specific economic data series and releases — read a print against consensus (surprise), trend, and z-score; track the release calendar; classify leading vs lagging indicators; and tag the growth/inflation regime. Includes a Python helper for surprise %, z-scores, and trend from a release CSV. Use when the user wants a single indicator explained, a release reaction, a surprise-vs-consensus read, an economic data calendar, or which indicators to watch. For the broader narrative note, pair with macro-research."
metadata: {"flowly":{"emoji":"📊","tags":["finance","economics","macro","indicators","cpi","jobs","pmi","surprise","release-calendar","data"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["macro-research","credit-analysis","portfolio-review","statistical-analysis","finance"]}}
---

# Economic Indicators — Read the Data Series, Not Just the Headline

This skill is the **data-mechanics complement** to `macro-research`. Where macro-research writes the narrative note and the regime call, this one is the disciplined toolkit for a *single series or release*: what it measures, how to judge today's print (surprise, trend, z-score), whether it leads or lags, and where it sits on the calendar. Use this when the question is about *an indicator*; use macro-research when the question is about *the economy*.

## What this skill produces

**Chat-first.** Default: a tight read on a specific release — actual vs consensus vs prior, the surprise and its size (z-score), the trend direction, what it leads/lags, and the one-line market implication. Offer a file for a tracked indicator dashboard or a release-calendar table.

## When to use

- "Explain \<indicator\> — what does it measure?" (PMI, NFP, CPI, JOLTS, PCE, ISM, claims, retail sales, housing starts, etc.)
- "Read this morning's \<release\> — was it good?"
- "How big was the surprise vs consensus?" / "Is this a meaningful beat?"
- "What's on the economic calendar this week?"
- "Which indicators lead the cycle / inflation / the labor market?"
- "Is this a leading or lagging indicator?"

(For "what does it all mean for the economy / my portfolio / the Fed" → `macro-research`.)

## Reading a single print (the discipline)

1. **Actual vs consensus vs prior.** Markets trade the **surprise** (actual − consensus), not the level. State all three.
2. **Size the surprise.** A "beat" of 0.1 on a noisy series is nothing. Use a **z-score** (surprise ÷ historical surprise std, or change ÷ series std) to judge whether it's signal. A surprise inside ±0.5σ is largely noise.
3. **Trend & second derivative.** One print is noise; the 3–6 month trend is signal. Is it decelerating even if still elevated? Direction > level.
4. **Revisions.** Many series (NFP, GDP, retail sales) are heavily revised — a strong headline with a big downward prior revision can be a net negative. Always check the revision.
5. **Headline vs core/control.** Strip the volatile bits: CPI/PCE → **core**; retail sales → **control group**; durable goods → **ex-transportation/ex-defense**. The core is the trend.
6. **Diffusion vs level.** For ISM/PMI, **50 is the line** (expansion/contraction), and the sub-indices (new orders, prices paid, employment) often lead the headline.

## Leading vs coincident vs lagging (know what you're holding)

| Type | Examples | Use for |
|---|---|---|
| **Leading** | PMI new orders, building permits, jobless claims, yield curve, consumer expectations, avg weekly hours, money supply | Anticipating turns |
| **Coincident** | Nonfarm payrolls, industrial production, real income, retail sales | Confirming the current state |
| **Lagging** | Unemployment rate, CPI, unit labor costs, prime rate | Confirming a trend already underway |

A common error: reacting to a **lagging** indicator (unemployment rate) as if it's predictive. By the time the U-rate turns up decisively, the slowdown is usually well underway — the **leading** set (claims, permits, PMI orders) moved months earlier.

## The release calendar (timing & sequencing)

- Know the cadence: weekly (jobless claims), monthly (jobs ~1st Friday, CPI ~mid-month, PCE ~end, ISM ~1st business day, retail sales ~mid), quarterly (GDP, ECI).
- **Sequencing matters:** ISM prices-paid and PPI foreshadow CPI; ADP and claims set up NFP; the components of one release preview the next.
- Flag the **high-impact** releases (CPI, NFP, PCE, FOMC, GDP) vs second-tier ones — not every print moves markets equally.

## The helper

`scripts/surprise.py` turns a CSV of releases (actual, consensus, prior) into surprise %, z-scores, and a trend read — useful for judging whether a print is signal or noise and for building a tracked table.

```bash
python3 scripts/surprise.py releases.csv
# CSV columns: date, indicator, actual, consensus, prior   (consensus/prior optional)
# single-series mode adds a z-score of the actual vs the column's own history
```
Stdlib only.

## Data sourcing

- **Primary agencies, cited & dated:** BLS (CPI, NFP, JOLTS, ECI), BEA (GDP, PCE, income), ISM / S&P Global (PMIs), Census (retail sales, housing, durable goods), Fed (industrial production, consumer credit), DOL (claims).
- **FRED** for the historical series (`fred.stlouisfed.org/series/<ID>`).
- Consensus from the user or a cited source — **never invent the consensus or the print.** Stamp the release date; this data is revised.

## Chat output format

```
**CPI — May 2026** (released 2026-06-11)

Core CPI +0.2% MoM (cons +0.3%, prior +0.4%) → **−0.1pp surprise, ~0.8σ cooler** ✅
Headline +0.1% MoM · YoY core 3.2% (↓ from 3.4%) — 3rd straight monthly decel
Type: lagging (confirms the disinflation trend already in PPI/ISM-prices)
Leads watched into this: ISM prices-paid had softened → consistent.

Implication: a genuine downside surprise of meaningful size; supports the
disinflation narrative. (Full regime read → macro-research.)
```

## Workflow

1. **Identify the indicator** and what it actually measures (don't assume).
2. **Get actual / consensus / prior** (+ the relevant core/control cut).
3. **Run `surprise.py`** to size the surprise (z-score) and trend; check revisions.
4. **Classify** leading/coincident/lagging and note the sequencing context.
5. **Give the one-line implication**; hand off to `macro-research` for the regime/market narrative, `statistical-analysis` for deeper series work.

## Key pitfalls

- **Level instead of surprise.** Markets move on actual vs expected — always include consensus.
- **Unsized surprises.** A "beat" without a z-score can be pure noise on a volatile series.
- **Ignoring revisions.** The prior-period revision can outweigh the headline.
- **Headline over core/control.** The volatile components mislead on the trend.
- **Reacting to lagging indicators as if predictive.** The U-rate and CPI confirm; they don't lead.
- **Treating all releases as equal.** Tier them — CPI/NFP/PCE/FOMC move markets; many don't.
- **Stale data unmarked.** Date every print; economic data is the most-revised data there is.

## Quick reference

- Surprise = actual − consensus · Surprise z = surprise ÷ historical surprise std (|z|<0.5 ≈ noise)
- Core CPI/PCE (ex food & energy); retail-sales control group; durable goods ex-transport — the trend cuts.
- ISM/PMI: 50 = expansion/contraction line; new-orders & prices-paid sub-indices lead.
- Leading: claims, permits, PMI orders, yield curve, weekly hours. Lagging: U-rate, CPI, ULC.
- Key FRED IDs: CPIAUCSL, CPILFESL (core CPI), PCEPILFE (core PCE), PAYEMS, UNRATE, ICSA (claims), NAPM/MANEMP, RSAFS (retail), HOUST, GDPC1.
- Sequencing: PPI/ISM-prices → CPI; ADP/claims → NFP; component → next release.
