---
name: portfolio-review
description: "Review an investment portfolio — position weights, concentration, sector/geography/asset-class exposure, factor tilts, correlation and diversification, drawdown and volatility, and concrete rebalancing notes. Includes a Python helper that turns a holdings CSV into exposure, concentration, and risk tables. Use when the user shares holdings and asks 'how's my portfolio', about diversification, risk, concentration, or rebalancing."
metadata: {"flowly":{"emoji":"📐","tags":["finance","portfolio","asset-allocation","risk","diversification","concentration","rebalancing","factor-exposure"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["risk-modeling","macro-research","comps-analysis","finance"]}}
---

# Portfolio Review — Exposure, Risk, and What to Do About It

A portfolio review answers "**what am I actually exposed to, where is the risk concentrated, and what should I change?**" People think they're diversified because they own 25 names; then it turns out 70% is mega-cap tech that all move together. The value here is making hidden exposure and concentration visible, then turning that into a short, actionable rebalancing list.

> **Not financial advice.** Frame outputs as analysis and options, not personalized investment recommendations. Note that conclusions depend on the user's goals, horizon, and risk tolerance — ask for those if they aren't given.

## What this skill produces

**Chat-first.** Default: a few compact tables (top positions + weights, sector/geo/asset-class exposure, concentration metrics, a risk snapshot) and a short, prioritized list of rebalancing observations. Offer a full file (`.xlsx`/`.md`) for a detailed breakdown or an ongoing tracker.

Use `scripts/portfolio.py` to convert a holdings CSV into all the standard tables — it does the arithmetic so you can focus on the judgment.

## When to use

- "Here are my holdings — how's my portfolio?" / "Review my portfolio."
- "Am I diversified?" / "Where's my concentration / hidden risk?"
- "What's my sector / geography / asset-class exposure?"
- "What should I rebalance?" / "Is this too risky for me?"
- "How correlated are my positions?" / "What's my biggest drawdown risk?"

## What to ask for (inputs)

Ideal holdings data, one row per position: **ticker, name, quantity OR market value, asset class, sector, geography** (and cost basis if they want gains/tax framing). A CSV or a pasted list both work. Also helpful, if available: the user's **goals, time horizon, and risk tolerance** (these change the verdict), and a benchmark to compare against.

If you only get tickers + values, you can still do weights and concentration; sector/geo/factor needs classification (ask, or classify the well-known names and flag the rest as "unclassified").

## The review, dimension by dimension

### 1. Position weights & concentration
- Rank positions by weight; eyeball the top 5 / top 10 share.
- **Concentration metrics:** top-1, top-5, top-10 weight; the **Herfindahl-Hirschman Index (HHI)** = Σ(weightᵢ²) and its inverse, the **effective number of holdings** (1/HHI) — "you own 25 names but your effective N is 6."
- Flag any single position >~10% (idiosyncratic blow-up risk) and any cluster that behaves as one bet.

### 2. Exposure breakdowns
Aggregate weights by:
- **Asset class** (equity / fixed income / cash / alternatives / crypto) — the single biggest driver of return and risk.
- **Sector / industry** — is "diversified" actually 60% tech?
- **Geography** — home-country bias is the most common blind spot.
- **Market cap / style** (large/mid/small, growth/value) if classifiable.

### 3. Factor & correlation (the hidden-bet check)
- Many "different" stocks share factors (growth, rates-sensitivity, momentum, the same mega-cap beta). Two names with 0.9 correlation are one position.
- Note obvious factor tilts (e.g. heavy long-duration growth = one rate bet; lots of banks + energy = a value/cyclical bet).
- If returns history is available, a correlation matrix / average pairwise correlation quantifies real diversification; otherwise reason qualitatively from sector/factor overlap.

### 4. Risk snapshot
- **Volatility** (annualized) and **max drawdown** if a return/price history is provided.
- **Estimated portfolio beta** vs a benchmark (weighted-average of position betas, if supplied).
- A rough **downside scenario** ("in a 2022-style drawdown this book likely falls X%") — hand off to `risk-modeling` for VaR / proper stress testing.

### 5. Cash & income (if relevant)
- Cash drag vs dry powder; dividend/yield profile; concentration of income in a few payers.

## From analysis to action — rebalancing notes

Don't stop at description. End with **3–6 concrete, prioritized observations**, each tied to a finding:
- "Top 3 positions are 48% of the book and all mega-cap tech — trimming to ~30% would cut single-factor risk materially."
- "0% international and 0% fixed income — adding ex-US and bonds would diversify the dominant US-equity beta."
- "Position X is 14% — above a typical single-name cap; consider trimming to ≤10%."
Frame as options with the trade-off stated, anchored to the user's stated goals/horizon when known. Avoid prescriptive "buy/sell this now" calls.

## Chat output format

```
**Portfolio review** (15 holdings, $X total)

Concentration: top-5 = 58% · HHI 0.14 → effective ~7 holdings
| Position | Weight |
|----------|--------|
| NVDA | 18% |
| MSFT | 14% |
| AAPL | 11% |
| ... | |

Sector: Tech 61% · Health 12% · Financials 9% · Other 18%
Geography: US 94% · Intl 6%   Asset class: Equity 96% · Cash 4%

⚠️ Findings:
1. 61% tech + top-3 all mega-cap → effectively one factor bet.
2. 94% US, ~0% bonds → no diversifier if equities sell off.
3. NVDA 18% — well above a 10% single-name guard.

🔧 Options (not advice): trim top-3 toward ~30%; add ex-US + a bond sleeve;
   given your stated 10-yr horizon, the equity tilt may be acceptable — but
   the *concentration*, not the equity %, is the main risk here.
```

Keep tables ≤4 columns and to the top ~8 positions in chat; send the full holdings table as a file if long.

## Workflow

1. **Get the holdings** (CSV/list) + ideally goals/horizon/risk tolerance + benchmark.
2. **Run `portfolio.py`** to compute weights, concentration (HHI/effective N), and exposure breakdowns; add risk metrics if a return history is supplied.
3. **Find the hidden bets** — factor/correlation overlap behind nominal diversification.
4. **Assess against the user's profile** (or note the assumptions you're making).
5. **Write the prioritized rebalancing notes** — options with trade-offs, not directives.
6. **Deliver** the tables + findings; offer the file; hand off to `risk-modeling` for VaR/stress, `macro-research` for the regime lens.

## Key pitfalls

- **Counting names instead of measuring concentration.** 25 holdings can be a 3-factor bet. Always show effective N / top-5 share.
- **Sector diversification ≠ factor diversification.** "Different sectors" that all share mega-cap/growth/rates beta move together.
- **Ignoring home bias.** A heavily home-country book is one currency + one economy bet.
- **No personalization context.** "Too risky" is meaningless without horizon and tolerance — ask, or state your assumption.
- **Description without action.** The user wants to know what to *do*; end with prioritized options.
- **Over-precision on estimated risk.** Beta/vol from sparse data are estimates — present ranges and caveat them.
- **Crossing into advice.** Offer analysis and options; flag that personalized recommendations depend on their full situation.

## Quick reference

- Weight = position value ÷ total portfolio value
- HHI = Σ(weightᵢ²) · Effective number of holdings = 1 / HHI
- Single-name guard: many frameworks cap individual positions at ~5–10%.
- Real diversification needs **low-correlation** exposures, not just many positions.
- The asset-allocation decision (equity/bonds/cash/alts mix) usually dominates security selection for total risk.
- For VaR, stress scenarios, and formal downside modeling → `risk-modeling`.
