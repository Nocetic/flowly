---
name: credit-analysis
description: "Assess a company's creditworthiness — leverage and coverage ratios, debt capacity, the maturity wall, covenant headroom, liquidity, and free-cash-flow durability — and write a rating-style credit memo with a view on default risk. Includes a Python helper that computes the ratio scorecard from inputs. Use when the user asks 'can they pay their debt', about bond/credit risk, covenants, refinancing risk, or a credit opinion."
metadata: {"flowly":{"emoji":"🧾","tags":["finance","credit","fixed-income","leverage","coverage","covenants","debt","default-risk","ratings"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["sec-filings","lbo-model","earnings-analysis","comps-analysis","finance"]}}
---

# Credit Analysis — Will They Pay the Debt Back?

Equity analysis asks "how much upside?"; credit analysis asks "**what can go wrong, and do they still pay?**" It's an asymmetric, downside-first discipline: you don't get paid more for being right about a good company, only for avoiding the one that defaults. Center every judgment on **cash flow vs obligations** and **what happens in a bad year**.

## What this skill produces

**Chat-first.** Default: a credit scorecard (the key leverage/coverage/liquidity ratios with trend), the maturity wall, and a one-line credit view (e.g. "solid IG-equivalent; 2.1x levered, well-covered, no near-term wall"). Offer a full rating-style memo (`.md`/`.pdf`) when the user wants the long form.

Use `scripts/credit_ratios.py` to compute the scorecard cleanly and print it as markdown.

## When to use

- "Can \<company\> service its debt?" / "Is their balance sheet safe?"
- "How risky is this bond?" / "Default risk on \<issuer\>?"
- "Walk me through their leverage / coverage / covenants."
- "When does their debt mature? Refinancing risk?"
- "Give me a credit opinion / rating-style view."
- "Is the dividend / buyback safe given the debt load?"

## The five C's, modernized for corporates

1. **Capacity** — can cash flow cover debt service? (the heart of it)
2. **Leverage** — how much debt relative to earnings/assets?
3. **Liquidity** — can they fund the next 12–24 months *without* the capital markets?
4. **Collateral / structure** — secured vs unsecured, where you sit in the waterfall.
5. **Conditions / business risk** — cyclicality, customer concentration, moat, industry.

## The ratio scorecard (what to compute)

### Leverage — how much debt
- **Total Debt / EBITDA** (gross leverage) — the headline. <2x conservative, 2–4x moderate, 4–6x aggressive, >6x highly levered.
- **Net Debt / EBITDA** — net of cash (use when cash is real and accessible).
- **Debt / (Debt + Equity)** and **Debt / Assets** — balance-sheet leverage.
- **EBITDA adjustments matter** — capitalize operating leases where material; treat "adjusted" EBITDA skeptically (add-backs inflate capacity).

### Coverage — can they pay the interest (and more)?
- **EBITDA / Interest** (interest coverage) — >4x comfortable, 2–4x adequate, <2x stressed, <1x can't pay.
- **(EBITDA − Capex) / Interest** — coverage after the capex they can't skip.
- **FFO / Debt** and **FCF / Debt** — cash-flow-to-debt (the agencies lean on FFO/Debt heavily).
- **DSCR** = (EBITDA − capex − taxes) / (interest + mandatory amortization) — total debt-service coverage; <1x means cash doesn't cover obligations.

### Liquidity — survive without the market
- **Cash + undrawn revolver** vs near-term needs.
- **Current ratio / quick ratio** for working-capital businesses.
- **Sources vs uses over 12–24 months:** cash + FCF + revolver vs maturities + capex + dividends.

### Profitability & cash durability
- Margin stability, FCF positive *through a cycle*, earnings volatility. A 3x-levered stable utility is safer than a 2x-levered cyclical.

## The maturity wall — timing is everything

A solvent company can still fail if it can't **refinance** at the wrong moment.
- Lay out the **debt maturity schedule** by year. A cluster of maturities in the next 12–24 months ("the wall") against thin liquidity is the classic distress setup.
- Ask: can they refi at *current* market rates, and what does that do to coverage? (Rolling 4% debt into 9% debt can break an otherwise-fine credit.)
- Floating-rate exposure: how much interest goes up if rates stay high?
- Pull the debt note and maturity table from filings via the `sec-filings` skill (`edgar.py`).

## Covenants — the early-warning system

- **Maintenance covenants** (tested each period): max leverage, min coverage, min liquidity. Compute **headroom** — how far can EBITDA fall before a breach?
- **Incurrence covenants** (tested on action): limits on new debt, dividends, asset sales.
- A breach → technical default → renegotiation, fees, or acceleration. Shrinking headroom is a leading indicator of trouble, often before the bonds move.

## Structure & recovery — if it does default, what do you get?

- **Seniority waterfall:** secured → senior unsecured → subordinated → equity.
- **Structural subordination:** opco debt sits ahead of holdco debt on opco assets.
- **Recovery:** secured/senior recovers more; estimate via asset coverage or EV-in-distress vs debt layers.
- For a bond question, place the specific instrument in the stack — a senior secured note and a sub note on the same issuer are very different risks.

## Sourcing

- Financials and the debt schedule from filings (`sec-filings` / `edgar.py`); the debt footnote is the most important page.
- Market signals (bond yields/spreads, CDS, agency ratings) from the user or a cited source — **never invent spreads or ratings**.
- Date everything; credit deteriorates fast.

## Chat output format

```
**Credit — ACME** (FY2025, filed 2026-02)

Scorecard (trend vs FY24):
| Metric | Value | Band |
|--------|-------|------|
| Net debt/EBITDA | 2.1x ↓ | moderate |
| EBITDA/interest | 6.4x ↑ | comfortable |
| (EBITDA−capex)/int | 4.1x | adequate |
| FCF/debt | 18% | healthy |

🏗️ Maturities: nothing material until $400M due 2028 — no near wall.
📐 Covenant headroom: EBITDA can fall ~35% before max-leverage trip.
💧 Liquidity: $1.2B cash + $750M undrawn revolver vs $0 near-term.

📌 View: comfortably investment-grade-equivalent. Low default risk;
   main risk is a debt-funded acquisition that lifts leverage. Watch capex.
```

## Workflow

1. **Frame the question:** whole-company credit, a specific bond, refi risk, or covenant headroom?
2. **Pull financials + the debt note** (`edgar.py`); identify every tranche, rate, and maturity.
3. **Run `credit_ratios.py`** for the scorecard; trend it vs the prior year.
4. **Map the maturity wall** and test refi at current rates.
5. **Check covenant headroom** and liquidity (sources vs uses, 12–24 mo).
6. **Stress it:** what do the ratios look like if EBITDA drops 20–30% (a recession year)?
7. **Place the instrument** in the structure (for bond questions) and estimate recovery.
8. **Deliver** the scorecard + view; hand off to `lbo-model` if the question is debt *capacity* for a deal, `earnings-analysis` if the trend is the story.

## Key pitfalls

- **Equity mindset on a credit question.** Upside is irrelevant; you're underwriting the downside. A great growth story can be a terrible credit.
- **Trusting "adjusted" EBITDA.** Add-backs inflate capacity and coverage. Haircut aggressive adjustments; capitalize material leases.
- **Ignoring the maturity wall.** Solvency ≠ liquidity. Companies fail at refinancing, not on a coverage ratio.
- **Static analysis.** Always stress for a bad year — credit is about the cycle's trough, not the peak.
- **Gross vs net leverage confusion.** Net is only fair if the cash is real, domestic/accessible, and not already spoken for.
- **Forgetting the rate reset.** Low coupons rolling into a high-rate market can break coverage; model the refi.
- **Treating one issuer as one risk.** Senior secured ≠ subordinated; structure determines recovery.

## Quick reference

- Gross leverage = Total debt / EBITDA · Net leverage = (Total debt − cash) / EBITDA
- Interest coverage = EBITDA / Interest · Cash-flow coverage = (EBITDA − capex) / Interest
- DSCR = (EBITDA − capex − taxes) / (Interest + Mandatory amortization)
- FFO/Debt and FCF/Debt — agencies weight these heavily for ratings.
- Rough IG vs HY line: ~3x leverage / ~BBB−. Below that band ≈ high yield, refinancing-sensitive.
- Covenant headroom = how far EBITDA can fall before breaching the maintenance test — compute it.
