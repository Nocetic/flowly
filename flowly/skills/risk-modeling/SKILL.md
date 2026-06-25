---
name: risk-modeling
description: "Quantify and stress-test downside risk — Value-at-Risk (VaR) and CVaR/Expected Shortfall (historical + parametric), volatility, max drawdown, beta, scenario and sensitivity analysis, and liquidity risk. Includes a Python helper that turns a returns CSV into a full risk report. Use when the user asks 'how much could I lose', about VaR, downside/tail risk, stress testing, volatility, or worst-case scenarios."
metadata: {"flowly":{"emoji":"🎯","tags":["finance","risk","var","cvar","stress-test","volatility","drawdown","tail-risk","scenario-analysis"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["portfolio-review","macro-research","credit-analysis","statistical-analysis","finance"]}}
---

# Risk Modeling — How Much Could This Lose, and When?

Risk modeling is the discipline of putting a number and a story on the downside. The number (VaR, vol, drawdown) is the easy part; the discipline is **respecting its limits** — every model is wrong in the tail, exactly where it matters. Lead with the loss the user actually cares about and always say what the model can't see.

## What this skill produces

**Chat-first.** Default: a risk snapshot — the headline downside numbers (VaR/CVaR at a stated confidence + horizon, volatility, max drawdown) plus a couple of named stress scenarios and the one-line takeaway. Offer a full file for a detailed risk report or an ongoing monitor.

The deliverable is a *decision-useful* read of risk, not a statistics dump. "95% 1-day VaR of 2.1% (~\$21k on \$1M)" beats "σ = 0.013."

## When to use

- "How much could I lose?" / "What's my VaR / downside?"
- "Stress-test this portfolio / position." / "What happens in a 2008 / 2020 / 2022 scenario?"
- "How volatile is this?" / "What's the max drawdown?"
- "What's my tail risk?" / "Worst-case?"
- "Build me a sensitivity table for \<variable\>."

## The core measures (and what each really says)

| Measure | What it answers | The catch |
|---|---|---|
| **Volatility (σ)** | Typical dispersion of returns | Symmetric; treats upside = downside; assumes ~normal |
| **VaR (95%/99%)** | "Loss not exceeded X% of the time" | Says *nothing* about how bad the tail beyond it is; not sub-additive |
| **CVaR / Expected Shortfall** | "Average loss *when* you breach VaR" | The better tail metric; still data-dependent |
| **Max drawdown** | Worst peak-to-trough decline | Path-dependent; backward-looking |
| **Beta** | Sensitivity to a benchmark/market | Linear; unstable in crises (correlations → 1) |
| **Downside deviation / Sortino** | Vol of *only* bad returns | Needs a target/MAR |

**VaR is a floor on bad news, not a ceiling.** "95% 1-day VaR = 2%" means *on the worst 5% of days you lose more than 2% — possibly far more.* That's why you pair it with CVaR.

## Three ways to compute VaR (use more than one)

1. **Historical simulation** — rank actual past returns, read off the percentile. No distribution assumption; captures real fat tails and skew. Limited by the lookback window (did it include a crash?).
2. **Parametric (variance-covariance)** — assume normal, VaR = z × σ × √horizon. Fast, but **understates tail risk** because real returns are fat-tailed (kurtosis) and skewed.
3. **Monte Carlo** — simulate many paths from an assumed process. Flexible for nonlinear payoffs (options); only as good as the assumed model.

Report at least historical **and** parametric; the gap between them *is* the fat-tail signal. State confidence (95% vs 99%) and horizon (1-day vs 10-day) explicitly — and remember the **√t scaling** of vol assumes i.i.d. returns (it breaks under autocorrelation/vol-clustering).

## Stress & scenario analysis (this matters more than VaR)

Statistical VaR fails precisely in regime shifts. Complement it with **named, deterministic scenarios**:
- **Historical replays:** 2008 GFC (−50%+ equities, credit freeze), Mar-2020 COVID (−34% in weeks), 2022 rate shock (bonds *and* stocks down together), 1998 LTCM, 2018-Q4.
- **Factor shocks:** equities −20%, rates +100bps, credit spreads +200bps, oil ±30%, USD +10%, correlations → 1.
- **Reverse stress test:** start from "what would cause a 30% loss?" and work backward to the scenario — often more revealing than forward VaR.

For each scenario, estimate the portfolio P&L and name the dominant driver. **In a crisis, diversification fails** — correlations converge toward 1, so model the correlated case, not the calm-market one.

## Sensitivity analysis

For models/positions with key drivers, show how the outcome moves as one or two inputs flex (a 1-D table or 2-D grid). Center on the base case; use odd dimensions so the middle is the base. (For valuation grids specifically, see `dcf-model`/`lbo-model`; this skill covers risk/P&L sensitivities.)

## Liquidity risk (the risk that bites in the stress)

- **Position vs market depth:** how many days of average volume is the position? Can it be exited without moving the price?
- **Funding liquidity:** margin/leverage calls forcing sales at the worst time.
- **Liquidity-adjusted horizon:** an illiquid book's effective VaR horizon is longer than 1 day.

## The helper

`scripts/risk.py` turns a CSV of returns (or prices) into the full snapshot — historical & parametric VaR/CVaR at chosen confidence, annualized vol, max drawdown, Sharpe/Sortino, skew/kurtosis, plus deterministic factor-shock scenarios.

```bash
python3 scripts/risk.py returns.csv --col daily_return --confidence 0.95 --horizon 1 --value 1000000
python3 scripts/risk.py prices.csv --prices --col close --confidence 0.99
```
Stdlib only (no numpy needed).

## Data sourcing

- Returns/prices from the user or a cited source — **never invent a return series**.
- State the **lookback window and its content** (a 2-year window that excludes any crash will lie about tail risk).
- Stamp dates; risk estimates decay as regimes change.

## Chat output format

```
**Risk snapshot** ($1.0M, daily returns, 3-yr lookback incl. 2022)

σ 14% ann · 95% 1-day VaR 1.8% (~$18k) · CVaR 2.7% (~$27k)
99% 1-day VaR 3.2% (~$32k) · Max drawdown −31% (2022)
Skew −0.4, excess kurtosis 4.1 → fat left tail (parametric VaR understates)

Stress P&L:
| Scenario | Est. loss |
|----------|-----------|
| Equities −20% | −18% |
| Rates +100bps | −6% |
| 2008 replay | −41% |

Takeaway: typical bad day ~$18–27k; a GFC-style event ~−40%. Tail is
fatter than normal — lean on CVaR/stress, not parametric VaR.
```

Always state confidence + horizon; never quote a VaR number bare.

## Workflow

1. **Frame the risk question** (whole-portfolio downside, a position, a scenario, a sensitivity).
2. **Get the data** (return/price series; positions; benchmark) and **state the lookback**.
3. **Run `risk.py`** for VaR/CVaR/vol/drawdown; report historical vs parametric.
4. **Add named stress scenarios** — historical replays + factor shocks + a reverse stress test.
5. **Check liquidity** (depth, funding, adjusted horizon).
6. **Deliver** the snapshot + takeaway with explicit confidence/horizon and model caveats; hand off to `portfolio-review` for exposure context, `macro-research` for the regime.

## Key pitfalls

- **Treating VaR as the worst case.** It's the *threshold*, not the limit. Pair with CVaR and stress.
- **Parametric-only.** Normal-distribution VaR understates fat tails — always show historical too.
- **Calm-market correlations.** In a crisis, diversification evaporates; model correlations → 1.
- **Lookback that excludes a crash.** Your tail estimate is only as scary as your worst historical day.
- **Bare numbers.** "VaR is 2%" with no confidence/horizon/base value is meaningless.
- **Ignoring liquidity.** A position you can't exit has a longer effective risk horizon than the math assumes.
- **False precision in the tail.** Tail estimates are uncertain by nature — present ranges, caveat the model.

## Quick reference

- Parametric VaR = z × σ × √horizon × value (z = 1.645 @95%, 2.326 @99%); subtract mean if material.
- CVaR (ES) = mean of losses beyond the VaR threshold — the better tail metric.
- Vol scaling: σ_T = σ_1 × √T (assumes i.i.d.; breaks under vol-clustering).
- Max drawdown = max peak-to-trough decline over the path.
- Sortino = (return − MAR) / downside deviation; penalizes only bad vol.
- Reverse stress test: solve for the scenario that produces an unacceptable loss.
