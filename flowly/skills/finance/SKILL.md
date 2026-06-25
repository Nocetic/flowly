---
name: finance
description: "Financial modelling toolkit — DCF, 3-statement, LBO, M&A, comps. Plus Excel/PPTX authoring for investment-banking-style deliverables. Use when the user asks for a valuation, projection, transaction model, or pitch deck driven by financial data."
metadata: {"flowly":{"emoji":"💼","tags":["finance","valuation","dcf","lbo","m&a","comps","excel","pptx","financial-modeling"],"requires":{"bins":["python3"]}}}
---

# Finance — Financial Modelling Toolkit

Adapted from Anthropic's `optional-skills/finance` bundle. Six modelling
disciplines plus two output authoring tracks. Pick the model that
matches the user's question, then drive the relevant output (Excel
workbook, PowerPoint deck, or summary write-up).

## When to Use This Skill

The user is asking for:

- A **valuation** of a public or private company → DCF or comps
- **Five-year projections** with linked income statement, balance
  sheet, and cash-flow statement → 3-statement model
- A **leveraged-buyout** scenario (sponsor returns, debt schedule,
  exit multiples) → LBO model
- A **merger or acquisition** with combined financials, synergies,
  accretion/dilution → merger model
- A **trading / transaction comparable** spread for benchmarking
  against peers → comps analysis
- A **finished Excel workbook** of any of the above (formatted,
  formulas live, ready for review) → excel-author
- A **pitch deck** that wraps the model output in IB-style slides →
  pptx-author

## The Six Models

### 1. 3-Statement Model
Five-year integrated projection: income statement → balance sheet →
cash-flow statement. Working capital schedules, capex/depreciation,
debt rollforward. The base layer almost every other model builds on.

### 2. DCF (Discounted Cash Flow)
Unlevered free cash flow, terminal value (Gordon growth or exit
multiple), WACC, sensitivity tables on g and WACC. Outputs equity
value and implied share price.

### 3. LBO (Leveraged Buyout)
Sources & uses, debt schedule (multiple tranches with cash sweeps),
sponsor IRR / MOIC, exit at terminal multiple. Returns table across
exit-year × exit-multiple.

### 4. Merger Model
Acquirer + target combined P&L, financing mix (cash / stock / debt),
synergies (revenue + cost), accretion-dilution analysis on EPS.
Deal-economics summary.

### 5. Comparable Company Analysis (Trading Comps)
Peer set selection, multiples (EV/EBITDA, EV/Revenue, P/E, P/B),
size and growth normalisation, implied valuation range for the target.

### 6. M&A Comparable Transactions
Precedent deals, control premia, paid multiples, deal-rationale
notes. Pair with trading comps for a public + private valuation
range.

## Output Authoring

### Excel Workbook
Formatted multi-tab workbook: Assumptions, Income Statement, Balance
Sheet, Cash Flow, Schedules (debt/working capital/capex), Valuation,
Sensitivities. Use ``openpyxl`` for the heavy lifting; preserve
number formats (1,000s, percentages, parentheses for negatives).

### PowerPoint Deck
IB-style pitch deck: Cover, Situation Overview, Market Position,
Financial Performance (historical + projected), Valuation Summary,
Sensitivities, Recommendations / Risks. Use ``python-pptx``.

## Workflow

1. **Clarify the ask.** What company / scenario? Public or private?
   Time horizon? What's the deliverable — a number, a workbook, a
   deck, or all three?
2. **Gather inputs.** Public: SEC filings, earnings calls, market
   data. Private: ask the user for the assumptions (revenue, margins,
   capex, working capital, growth rates).
3. **Pick the model**(s). Most engagements need 3-statement as the
   foundation, then DCF + comps for valuation, then optionally LBO
   or merger if it's a transaction.
4. **Build in Python** with ``openpyxl`` (Excel) or ``python-pptx``
   (deck). Keep formulas live, don't pre-compute and dump values —
   the user expects to be able to flex assumptions.
5. **Sanity-check.** Margins reasonable vs peers? Terminal value
   not the entire enterprise value? IRR within plausible LBO range
   (usually 15-30%)?
6. **Deliver** with a one-paragraph summary of the conclusion plus
   the workbook / deck.

## Key Pitfalls

- **Forgetting to link the three statements.** Net income flows to
  retained earnings; D&A flows back as cash; working capital changes
  flow to operating cash flow. If any link is broken the model
  doesn't tie.
- **Hardcoding what should be a formula.** Reviewers grow suspicious
  when the model "magically" produces the same answer as your
  calculator. Live formulas only.
- **Terminal-value dominance.** If &gt; 75% of DCF enterprise value is
  in TV, your projection horizon is too short or your TV assumptions
  are too aggressive.
- **Missing sensitivities.** Every valuation needs a 3×3 (at minimum)
  sensitivity grid. WACC × g for DCF; entry multiple × exit multiple
  for LBO.

## Reference

The full Anthropic finance bundle (with worked examples, Python
templates per model type, troubleshooting docs, and Excel/PPTX
author scripts) lives upstream at:

`flowly/optional-skills/finance/{3-statement-model, dcf-model,
lbo-model, merger-model, comps-analysis, excel-author, pptx-author}/SKILL.md`

Each has its own ``scripts/`` directory with reusable building
blocks. Pull individual sub-skills into Flowly as the workload
demands rather than carrying all 3,000+ lines as a single monolith.
