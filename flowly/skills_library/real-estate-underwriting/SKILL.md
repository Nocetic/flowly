---
name: real-estate-underwriting
description: "Underwrite a commercial/rental real-estate deal — rent roll, effective gross income, operating expenses, NOI, cap rate, debt service & DSCR, cash-on-cash, levered IRR and equity multiple, plus sensitivity to cap rate, rent, and vacancy. Includes a Python helper that runs the full underwrite. Use when the user asks to evaluate a property, rental, apartment/multifamily/commercial deal, cap rate, NOI, or whether a real-estate investment 'pencils'."
metadata: {"flowly":{"emoji":"🏠","tags":["finance","real-estate","underwriting","noi","cap-rate","dscr","irr","cash-on-cash","rental","cre"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["dcf-model","credit-analysis","risk-modeling","excel-author","finance"]}}
---

# Real-Estate Underwriting — Does the Deal Pencil?

Underwriting a property is the disciplined version of "is this a good buy." It flows in one direction: **rents → NOI → value (via cap rate) → returns (after debt).** Get NOI right and everything downstream follows; get it wrong (optimistic rents, forgotten expenses, no reserves) and the whole deal is fiction.

## What this skill produces

**Chat-first.** Default: the underwriting summary — NOI, going-in cap rate, DSCR, cash-on-cash, levered IRR / equity multiple, and a sensitivity grid (cap rate × rent), with a one-line "pencils / doesn't" verdict. Offer a full `.xlsx` (via `excel-author`) for a lender-ready model with annual cash flows.

## When to use

- "Should I buy this rental / apartment building / commercial property?"
- "Does this deal pencil at \$X?" / "What's a fair price?"
- "What's the cap rate / NOI / cash-on-cash / IRR on this?"
- "Underwrite this property." / "Run the numbers on this listing."
- "How much can I pay and still hit a Y% return?"

## The income statement of a building (build it in order)

1. **Gross Potential Rent (GPR)** — all units at market/contract rent, fully leased.
2. **− Vacancy & credit loss** — never underwrite to 0% vacancy; use a realistic/market rate (e.g. 5–10%), even if currently full.
3. **+ Other income** — parking, laundry, storage, fees.
4. **= Effective Gross Income (EGI).**
5. **− Operating expenses** — taxes (re-assessed at *your* purchase price, not the seller's!), insurance, utilities, management (even if self-managed, charge a market fee ~3–8%), repairs & maintenance, R&M, HOA, payroll. **Express as an expense ratio** (typically 35–50% of EGI) and sanity-check it.
6. **− Replacement reserves** — capital reserve per unit/SF for roofs, HVAC, etc. Real money; don't skip it.
7. **= Net Operating Income (NOI).** *Before* debt service and income tax. This is the number the whole valuation hangs on.

## Value & the cap rate

- **Cap rate = NOI ÷ Value.** It's the unlevered yield. Rearranged: **Value = NOI ÷ Cap rate.**
- A *lower* cap rate = *higher* price (and usually lower risk / better location / more growth). A 5% cap is "expensive"; a 9% cap is "cheap" (or riskier).
- **Going-in cap** (year-1 NOI ÷ purchase price) vs **exit/terminal cap** (assumed at sale — usually modeled ~25–50bps higher than going-in for conservatism).
- Get the market cap rate from **comparable sales**, not a wish. The cap rate is the single most important and most-debated assumption.

## Financing & leverage

- **Loan sizing** is constrained by the *lower* of LTV and DSCR:
  - **LTV** (loan-to-value): loan ÷ value (e.g. 65–75%).
  - **DSCR** = NOI ÷ annual debt service; lenders require a minimum (commonly **1.20–1.30x**). A 1.0x DSCR means NOI exactly covers the mortgage — no cushion.
- **Debt service** = mortgage payment (amortizing) on the loan at the quoted rate/term/amortization.
- **Debt yield** = NOI ÷ loan amount — a leverage-neutral lender check (often ≥8–10%).

## The return metrics (after debt)

- **Cash-on-cash** = (NOI − annual debt service) / total cash invested. Year-1 cash yield on equity.
- **Levered IRR** — the annualized return over the hold, including the **sale** (exit NOI ÷ exit cap, minus selling costs and loan payoff). IRR captures appreciation + cash flow + amortization + timing.
- **Equity multiple** = total cash returned ÷ cash invested (ignores time).
- Returns come from four sources — **cash flow, amortization (tenant pays down your loan), appreciation, and tax benefits** — note which is doing the work. A deal relying entirely on appreciation/cap-rate compression is a bet, not an investment.

## The helper

`scripts/re_underwrite.py` runs the full stack — EGI → NOI → cap rate → loan sizing (LTV & DSCR) → cash-on-cash → levered IRR → equity multiple — and prints a cap-rate × rent-growth sensitivity grid.

```bash
python3 scripts/re_underwrite.py \
  --gpr 240000 --vacancy 0.07 --other-income 12000 --opex-ratio 0.42 \
  --reserves-per-unit 300 --units 20 \
  --price 3000000 --ltv 0.70 --rate 0.065 --amort-years 30 \
  --hold-years 5 --rent-growth 0.03 --expense-growth 0.025 \
  --exit-cap 0.062 --selling-costs 0.05
```
Stdlib only.

## Sanity checks (run before delivering)

- **Expense ratio** in a believable band (35–50% of EGI for most multifamily; verify vs comps).
- **Taxes re-assessed** at the purchase price (a classic rookie miss — the seller's tax bill is irrelevant).
- **Vacancy > 0** even on a full building.
- **Reserves included** — a deal that only works without reserves doesn't work.
- **DSCR ≥ lender minimum** and **going-in cap vs market cap** reconcile.
- **Exit cap ≥ going-in cap** (don't underwrite cap-rate compression as your return).

## Chat output format

```
**Underwrite — 20-unit MF @ $3.0M**

NOI (yr-1) $148k · Going-in cap 4.9% · Price/unit $150k
Loan $2.1M (70% LTV, 6.5%, 30-yr) · DSCR 1.28x · Debt yield 7.0%
Cash invested $960k · Cash-on-cash 4.1% (yr-1)
5-yr levered IRR 12.4% · Equity multiple 1.7x (exit 6.2% cap)

Cap × rent-growth IRR:
| Exit cap \ rent g | 2% | 3% | 4% |
|-------------------|----|----|----|
| 6.0% | 12% | 14% | 16% |
| 6.2% | 11% | 12% | 14% |
| 6.5% |  9% | 11% | 13% |

Verdict: thin going-in cap, returns lean on rent growth + amortization,
not cap compression (good). DSCR has cushion. Pencils if rent g ≥ ~3%.
```

## Workflow

1. **Gather inputs:** rent roll/GPR, units, vacancy, other income, opex (or ratio), price, financing terms, hold, growth, exit cap.
2. **Build NOI** in order; re-assess taxes at purchase price; include reserves.
3. **Value & cap:** going-in cap vs market comps.
4. **Size the loan** (lower of LTV/DSCR); compute debt service & debt yield.
5. **Run `re_underwrite.py`** for returns + the sensitivity grid.
6. **Sanity-check** the list above; stress the cap-rate and rent assumptions (hand off to `risk-modeling` for deeper downside).
7. **Deliver** the summary + verdict; offer the `.xlsx`; use `credit-analysis` framing if it's the *debt* that's in question.

## Key pitfalls

- **Seller's expenses / taxes.** Re-underwrite from scratch at *your* basis; property tax resets on sale in most US jurisdictions.
- **Zero (or unrealistic) vacancy.** Even a full building turns over.
- **No reserves / no management fee.** Both are real costs; "I'll manage it myself" still has an opportunity cost.
- **Cap-rate compression as the thesis.** Exit cap ≥ going-in cap unless you can defend otherwise.
- **DSCR ignored.** The loan is sized by the *lower* of LTV and DSCR — a high-LTV quote can be DSCR-capped.
- **IRR without the sale.** Most of the return is at exit; model the disposition (exit cap, selling costs, loan payoff).
- **Pro-forma fantasy.** "Stabilized" / "after renovation" rents are projections — separate in-place from pro-forma and underwrite the path, not just the destination.

## Quick reference

- NOI = EGI − operating expenses − reserves (before debt & income tax)
- EGI = GPR − vacancy/credit loss + other income
- Cap rate = NOI ÷ Value · Value = NOI ÷ Cap rate
- DSCR = NOI ÷ Annual debt service (lender min ~1.20–1.30x)
- Debt yield = NOI ÷ Loan amount
- Cash-on-cash = (NOI − debt service) ÷ Cash invested
- Equity multiple = Total distributions ÷ Equity invested
- Loan = min(LTV-constrained, DSCR-constrained)
- 1% expense-ratio or vacancy error flows straight to NOI and gets multiplied by 1/cap into value.
