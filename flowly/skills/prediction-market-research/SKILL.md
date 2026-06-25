---
name: prediction-market-research
description: "Research a real-world event through prediction markets — read the market-implied probability, then independently assess the evidence to find where the market may be mispriced (your edge). Covers calibration, base rates, favorite-longshot bias, fee/spread drag, resolution-criteria risk, and combining multiple markets. Use when the user asks 'what are the odds of X', wants an event forecast, an edge vs a Polymarket/Kalshi price, or a probability-grounded take on an election/economic/sports/geopolitical event. Pulls live odds via the polymarket skill."
metadata: {"flowly":{"emoji":"🎲","tags":["prediction-markets","forecasting","probability","polymarket","kalshi","odds","event-research","calibration","edge"],"requires":{"bins":["python3","curl"]},"category":"finance","related_skills":["polymarket","macro-research","deep-research","crypto-token-analysis","finance"]}}
---

# Prediction-Market Research — Market-Implied Probability vs the Evidence

A prediction market gives you a number: the crowd's probability of an event, priced in real money. That number is a strong prior — but it's not gospel. The work here is two-sided: **(1)** read what the market thinks (and how much to trust it), and **(2)** form your *own* evidence-based estimate, so you can say where (if anywhere) the market is wrong. "The market says 65%" is reporting. "The market says 65%, but the base rate and recent evidence put it nearer 50% — the market is overpricing this" is research.

## What this skill produces

**Chat-first.** Default: the market-implied probability (with the as-of time and a note on spread/liquidity), your independent estimate with its reasoning and key drivers, the gap (your edge, if any), and the main risks to the read — including resolution-criteria gotchas. Offer a file for a multi-market dashboard or a tracked forecast.

> Educational analysis, not betting advice. Prediction markets are speculative; many jurisdictions restrict them.

## When to use

- "What are the odds of \<event\>?" / "How likely is X?"
- "Is the market on \<election / rate decision / outcome\> mispriced?"
- "What's the market-implied probability, and do you agree?"
- "Research this event for me." / "Give me a probability, not a vibe."
- "Compare what Polymarket and Kalshi are pricing."

## Step 1 — Read the market (price = probability)

- **Price ≈ probability.** A "Yes" trading at 0.65 means the market implies ~65%. (Pull live prices via the `polymarket` skill — `scripts/polymarket.py search/market/price` — or a cited source for Kalshi/others.)
- **Adjust for the spread & fees.** The mid isn't the tradeable price; bid/ask and fees create a no-edge band. A "65%" with a wide spread might be 60/70 — don't over-read precision on thin books.
- **Check liquidity & volume.** A deep, high-volume market is far more informative than a \$200 book two people made. Thin markets are noise dressed as a probability.
- **Read the resolution criteria — exactly.** This is the most underrated risk. *How* and *when* does it resolve, by what source, and what edge cases flip it? Many "obvious" markets resolve on a technicality. The map (the market question) is not the territory (the event).

## Step 2 — Form your own estimate (the actual research)

Don't anchor entirely on the market. Build an independent probability:

1. **Base rate first.** What's the historical frequency of this *class* of event? (Incumbent win rates, how often a leading candidate at this stage wins, how often the Fed does what's priced, etc.) Start outside-view, then adjust.
2. **Gather the evidence.** Polls (with quality/recency/house-effect caveats), fundamentals, expert models, recent news. For multi-source synthesis, hand off to `deep-research`; for macro/economic events, `macro-research`/`economic-indicators`.
3. **Update deliberately.** Move from the base rate toward the evidence in proportion to how strong and independent it is. Avoid double-counting correlated sources (ten outlets citing one poll = one poll).
4. **State your probability as a range** and name the 2–3 drivers that would move it most.

## Step 3 — Find the edge (and be honest about it)

- **Edge = your probability − market probability**, after fees/spread. A 3-point gap inside a 5-point spread is *not* an edge.
- **Where edges actually exist:** thin/illiquid markets, slow-to-update markets (the market lagging fresh news), niche events with few sharp participants, and resolution-criteria nuances the crowd misread. Edges are rare in deep, liquid, heavily-traded markets (elections near the close) — respect the efficient case and say "the market is probably right" when it is.
- **Know the biases you're exploiting or falling for:**
  - **Favorite-longshot bias:** longshots tend to be *overpriced* (people overpay for lottery-like payoffs), favorites slightly underpriced.
  - **Sentiment/partisan skew:** politically charged markets can carry a wishful-thinking premium.
  - **Stale pricing** after a news break, before the book catches up.

## Step 4 — Combine & sanity-check

- **Multi-outcome markets must sum to ~100%** (after fees). If the components sum to 108%, there's a fee/vig wedge — normalize before comparing.
- **Cross-market consistency:** related markets should cohere (e.g. "wins nomination" ≥ "wins presidency"). Inconsistencies are either an edge or a resolution-difference you've missed.
- **Compare venues** (Polymarket vs Kalshi vs bookmaker odds) — convergence raises confidence; divergence is a flag to investigate.
- **Convert odds formats** when needed: decimal d → prob 1/d; American +X → 100/(X+100), −Y → Y/(Y+100). Strip the vig for the "fair" probability.

## Data sourcing

- Live odds via `polymarket` (`scripts/polymarket.py`) or a cited source for other venues — **never invent a market price or volume**; timestamp it.
- Evidence (polls, news, models) cited and dated; lean on `deep-research` for the heavy lifting.
- **Never fabricate a probability to sound confident.** Uncertainty is the honest output; give a range and your reasoning.

## Chat output format

```
**Event: <X> by <date>**

Market-implied: ~62% Yes (Polymarket, as of 2026-06-06 14:00; spread 60/64, $1.2M vol)
My estimate: ~52% (range 48–57%)
  Base rate ~50% (this class of event) · recent evidence leans slightly yes
  but the market looks to be extrapolating one strong data point.
Edge: market ~10pts rich on Yes — but inside a liquid book, so modest conviction.
⚠️ Resolution risk: resolves on <source>; an <edge case> would flip it.
Watch: <the 1–2 things that would move this most>
```

## Workflow

1. **Pin the event + the exact market** (and its resolution criteria + as-of time).
2. **Read the market:** implied prob, spread, liquidity (via `polymarket`).
3. **Form your own estimate:** base rate → evidence (→ `deep-research`/`macro-research`) → range.
4. **Compute the edge** net of fees/spread; identify the bias in play.
5. **Cross-check** multi-outcome sums and other venues.
6. **Deliver** market prob + your prob + the gap + resolution risk + what to watch; default to "market's probably right" when it's deep and liquid.

## Key pitfalls

- **Reporting the market as your answer.** The market is a prior; the research is your independent estimate and the gap.
- **Ignoring the spread/fees.** A small "edge" inside the bid/ask is no edge.
- **Trusting thin markets.** A \$200 book is not a forecast.
- **Skipping the resolution criteria.** The technicality of *how* it resolves is where confident reads go wrong.
- **No base rate.** Inside-view-only forecasts are overconfident; anchor on frequency first.
- **Double-counting evidence.** Correlated sources (same poll, same model) aren't independent updates.
- **Longshot overconfidence.** Cheap "Yes" on a longshot is usually overpriced, not a bargain.
- **False precision / false confidence.** Give a range; "I don't have an edge here" is a valid, valuable answer.

## Quick reference

- Price ≈ implied probability (binary market, Yes share 0–1).
- Edge = your prob − market prob, **after** spread + fees.
- Decimal odds d → prob = 1/d · American +X → 100/(X+100) · −Y → Y/(Y+100).
- Multi-outcome implied probs sum to >100% by the vig — normalize to get fair odds.
- Favorite-longshot bias: longshots overpriced, favorites underpriced.
- Edges live in thin/slow/niche/misread-resolution markets; deep liquid markets are usually right.
- Live odds: `polymarket` skill; multi-source evidence: `deep-research`; macro events: `macro-research`.
