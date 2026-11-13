---
name: lbo-model
description: "Build a leveraged-buyout (LBO) model — sources & uses, multi-tranche debt schedule with cash sweep, the returns waterfall (sponsor IRR / MOIC), and an exit-year × exit-multiple sensitivity grid. Includes a Python helper to compute returns and print the grid for chat. Use when the user asks about a buyout, sponsor returns, 'can PE afford this', debt paydown, or LBO feasibility."
metadata: {"flowly":{"emoji":"🏦","tags":["finance","lbo","private-equity","leveraged-buyout","irr","moic","debt-schedule","returns","modeling"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["dcf-model","credit-analysis","comps-analysis","excel-author","finance"]}}
---

# LBO Model — Can the Sponsor Make Money on This Buyout?

An LBO model answers one question: if a financial sponsor buys this company mostly with borrowed money, holds it ~5 years, pays down debt with the company's own cash, and sells, **what return do they earn?** Returns come from three levers — **debt paydown**, **EBITDA growth**, and **multiple expansion** — and a good model shows you which one is doing the work.

## What this skill produces

**Chat-first.** Default: a returns summary (entry/exit assumptions, the IRR/MOIC headline, and the exit-multiple × exit-year sensitivity grid) printed straight into chat via `scripts/lbo_returns.py`. Offer a full formula-driven `.xlsx` (sources & uses, debt schedule, three-statement-lite, returns) via `excel-author` when the user wants the real workbook.

Use the helper to get a fast, correct returns read; build the Excel only when the user needs an auditable, flex-able model.

## When to use

- "Could a PE firm buy \<company\> and make money?" / "Run an LBO."
- "What return does the sponsor get at a 6.0x exit?" / "What's the IRR/MOIC?"
- "How much debt can this deal support?"
- "How sensitive are returns to the exit multiple / hold period / leverage?"
- "What entry multiple can a sponsor justify to hit a 25% IRR?"

## The mechanics, in order

### 1. Entry — Sources & Uses
The deal has to balance: **Sources = Uses**.

**Uses** (where the money goes):
- Purchase enterprise value = Entry multiple × LTM EBITDA
- + Refinance existing debt (if not assumed) + transaction fees (~2–3% of EV) + financing fees

**Sources** (where the money comes from):
- New debt (term loan + high yield/mezz, sized by leverage capacity)
- **Sponsor equity = the plug** (whatever Uses aren't covered by debt)
- + Management rollover, excess cash on the balance sheet

Leverage is sized by the credit market: total debt typically **4–6x EBITDA** (sector- and cycle-dependent). The equity check is the residual.

### 2. Debt schedule — the engine of returns
Model each tranche separately. For each year:
- **Beginning balance → interest** (rate × balance; floating = base rate + spread) **→ mandatory amortization → cash sweep → ending balance.**
- **Cash sweep:** excess free cash flow (after mandatory amort) pays down debt early, senior first. This is the core deleveraging that drives equity value.
- Track the **cash flow available for debt service**: EBITDA − cash taxes − capex − Δ net working capital − cash interest.
- Tranche order: senior secured (term loan) amortizes/sweeps first, then subordinated.

### 3. Operating projection (lite)
You don't need a full three-statement model, but you need the cash:
- Revenue → EBITDA via growth + margin assumptions (the operational thesis).
- EBITDA → unlevered FCF: less cash taxes, capex, ΔNWC.
- That FCF feeds the debt sweep.

### 4. Exit & the returns waterfall
- **Exit EV** = Exit multiple × exit-year EBITDA. (Conservative base case: **exit multiple = entry multiple** — don't bank on multiple expansion.)
- **Exit equity** = Exit EV − net debt at exit (this is why paydown matters).
- **MOIC** = Exit equity ÷ initial sponsor equity.
- **IRR** = the annualized return over the hold (with management of any interim dividends/recaps).

### 5. Returns attribution — *show which lever paid*
A credible LBO decomposes the equity value creation into:
- **EBITDA growth** (operational improvement)
- **Multiple expansion** (buy low, sell high — the least reliable, don't lean on it)
- **Debt paydown / deleveraging** (the structural engine)
If the entire return depends on multiple expansion, the deal is a bet on the market, not the business — flag it.

## The returns helper

`scripts/lbo_returns.py` computes the equity check, runs a simple sweep, and prints the IRR/MOIC plus the exit sensitivity grid as markdown — ideal for a chat answer.

```bash
python3 scripts/lbo_returns.py \
  --ebitda 100 --entry-mult 8.0 --exit-mult 8.0 \
  --net-debt 0 --leverage 5.0 --rate 0.09 \
  --years 5 --ebitda-growth 0.06 --fcf-conv 0.55 --fees 0.025
```
Outputs: sources & uses, entry/exit equity, **MOIC + IRR**, the lever attribution, and a 5×5 **exit-multiple × hold-year** IRR grid. Stdlib only.

## Sanity checks (run before delivering)

- **IRR in the plausible band:** sponsors target ~**20–25%+**; a base case under ~15% or a fantasy 40%+ both warrant a second look at the inputs.
- **Leverage is financeable:** total debt 4–6x EBITDA for most deals; >7x needs a reason (and a hot credit market).
- **Coverage holds:** EBITDA/interest comfortably >1x in every year — if the company can't service the debt, there's no deal (cross-check with `credit-analysis`).
- **Conservative exit:** base case exit multiple ≤ entry multiple. Put multiple expansion in the bull case only.
- **The cash actually sweeps:** debt should fall meaningfully over the hold; if it doesn't, returns rely entirely on growth + multiple.

## Chat output format

```
**LBO — ACME** (entry 8.0x $100M EBITDA, 5.0x leverage, 5-yr hold)

Sources & Uses: EV $800M = Debt $500M + Equity $325M (+ $25M fees)
Base case (exit 8.0x): Exit equity $X → MOIC 2.4x · IRR 19%
Lever mix: deleveraging 55% / EBITDA growth 35% / multiple 10%

Exit-multiple × hold-year IRR:
| Exit \ Yr | 4 | 5 | 6 |
|-----------|---|---|---|
| 7.0x | 16% | 15% | 14% |
| 8.0x | 21% | 19% | 17% |
| 9.0x | 25% | 22% | 20% |

Read: clears a ~20% bar at base case; return is structurally driven by
debt paydown, not multiple expansion (good). Sensitive to exit multiple.
```

## Workflow

1. **Gather entry assumptions:** LTM EBITDA, entry multiple, existing net debt, leverage target, debt rate, hold period.
2. **Gather the operating thesis:** EBITDA growth, margin path, capex, FCF conversion.
3. **Run `lbo_returns.py`** for the fast read + sensitivity grid.
4. **Sanity-check** IRR/leverage/coverage/exit conservatism.
5. **If the user wants the workbook**, build the full formula-driven `.xlsx` with `excel-author` (S&U, tranche-level debt schedule, returns) — keep formulas live, recalc to zero errors.
6. **Deliver** the returns headline, the lever attribution, and the grid; hand off to `credit-analysis` if debt capacity is the real question.

## Key pitfalls

- **Banking on multiple expansion.** Base case exit ≤ entry. Returns that need a higher exit multiple are a market bet.
- **Over-levering past what credit allows.** A model can put 9x on anything; the credit market can't fund it.
- **Ignoring the cash sweep.** Without modeling early paydown you understate the equity and miss the main return driver.
- **Forgetting fees.** Transaction + financing fees (4–6% combined of EV) come out of sources and dent the equity.
- **No coverage check.** A deal that can't service interest in a down year isn't a deal.
- **Confusing MOIC and IRR.** MOIC ignores time; a 2.5x over 7 years (~14% IRR) is worse than 2.0x over 4 years (~19%). Always report both.
- **Single-point answer.** Returns are a surface — always show the exit/hold sensitivity grid.

## Quick reference

- Equity check = Entry EV + fees + refinanced debt − new debt − rollover − excess cash
- MOIC = Exit equity ÷ Sponsor equity
- IRR ≈ MOIC^(1/years) − 1 (with no interim cash flows); use a real IRR for dividends/recaps
- Exit equity = (Exit multiple × exit EBITDA) − net debt at exit
- Leverage band: 4–6x EBITDA typical; coverage (EBITDA/interest) > 1.5–2x desired
- Return levers: deleveraging + EBITDA growth + multiple expansion — show the split.
