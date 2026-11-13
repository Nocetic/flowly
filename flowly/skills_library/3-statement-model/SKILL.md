---
name: 3-statement-model
description: Build fully-integrated 3-statement models (IS, BS, CF) in Excel with working capital schedules, D&A roll-forwards, debt schedule, and the plugs that make cash and retained earnings tie. Pairs with excel-author.
version: 1.0.0
license: Apache-2.0
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"🏦","tags":["finance","three-statement","income-statement","balance-sheet","cash-flow","excel","openpyxl","modeling"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["excel-author","pptx-author","dcf-model","finance"]}}
---

## Environment

You are building an `.xlsx` workbook on disk with **headless openpyxl** — there is no live Excel session.
Adopt the cell-coloring, formula-writing, named-range, and sensitivity-table conventions defined by the `excel-author` skill so the output stays consistent with the rest of the toolchain.
When the model is finished, force a recalculation pass before you hand it off: `python /path/to/excel-author/scripts/recalc.py ./out/model.xlsx`.

# Integrated Three-Statement Model Builder

This skill walks you through filling in a 3-statement model template — wiring the Income Statement, Balance Sheet, and Cash Flow Statement together so every figure flows through live formulas and the model self-balances when assumptions change.

## The One Rule That Cannot Be Broken: Live Formulas Only

A 3-statement model earns its keep by *recomputing itself* when you flip a scenario or nudge a driver. The instant you bake a computed number into a cell, that chain snaps — silently. So:

- Every projection, roll-forward, link, and subtotal is an **Excel formula string**, never a number you worked out beforehand.
- In openpyxl this means you assign the *expression*: `ws["D15"] = "=D14*(1+Assumptions!$B$5)"`. You never assign the *answer*: `ws["D15"] = 12500`.
- Only two kinds of cells are allowed to hold a literal number: (a) **historical actuals**, and (b) **driver assumptions on the Assumptions tab**. Everything else is derived.
- Caught yourself calculating a figure in Python so you can drop it into a cell? Stop and write the formula that produces it instead.

If you ignore this, the integrity checks downstream will report "balanced" against stale hardcodes and you'll ship a broken model.

## Build It in Stages — Confirm With the User at Each Gate

Do **not** populate the whole workbook and then unveil it. Errors compound across statements, so surface your work in stages and let the user sanity-check each one before you continue:

1. **Template mapped** — list the tabs and sections you found; confirm your reading before editing a single cell.
2. **Historicals entered** — show the historical block; confirm the numbers and periods match the source.
3. **Income Statement projected** — run the subtotal checks, show the projected IS; confirm before starting the BS.
4. **Balance Sheet built** — show the A = L + E balance check for *every* period; confirm before starting the CF.
5. **Cash Flow built** — show the cash tie-out (CF ending cash equals BS cash); confirm before you finalize.

## Color Convention — Restrained Blue & Grey

Unless the template or user dictates otherwise, use a deliberately spare palette: blues and a single grey, plus white. No greens-as-fills, yellows, or oranges — visual restraint reads as professional.

| Where it appears | Fill | Font |
|---|---|---|
| Statement title bars (IS / BS / CF) | Dark blue `#1F4E79` | White, bold |
| Period headers (FY2024A, FY2025E…) | Light blue `#D9E1F2` | Black, bold |
| Inputs (actuals, driver assumptions) | Light grey `#F2F2F2` or white | Blue `#0000FF` |
| Calculated cells | White | Black |
| Links pulling from another tab | White | Green `#008000` |
| Check rows and headline totals | Medium blue `#BDD7EE` | Black, bold |

The roster: three blues, one grey, white. **Font color tells you what a cell *is*** — input, formula, or cross-tab link. **Fill color tells you where you *are*** — header band, data body, or check row. If the template arrives with its own scheme, defer to the template.

## Reading the Template Before You Touch It

Templates differ in how they name tabs and lay out rows. Survey the whole workbook first.

### Likely tabs and what lives on them

| Tab name(s) you might see | What it holds |
|---|---|
| IS, P&L, Income Statement | Income Statement |
| BS, Balance Sheet | Balance Sheet |
| CF, CFS, Cash Flow | Cash Flow Statement |
| WC, Working Capital | Working Capital schedule |
| DA, D&A, Depreciation, PP&E | Depreciation & Amortization schedule |
| Debt, Debt Schedule | Debt schedule |
| NOL, Tax, DTA | Net Operating Loss / deferred-tax schedule |
| Assumptions, Inputs, Drivers | Forecast drivers and inputs |
| Checks, Audit, Validation | Integrity dashboard |

Not every template ships every schedule. Note which tabs actually exist, flag any tab not on this list, and trace how the supporting schedules feed the three primary statements.

### Reading a single tab

- **Rows:** find the title, the section dividers, the units row ($mm / % / x), and the period labels. Note where actuals (A) hand off to estimates (E).
- **Columns:** confirm line-item labels sit in the leftmost column, that historical years precede projected years, and that the column order is identical across every tab. Look for the border that separates history from forecast.
- **Cell types:** distinguish inputs from formulas, usually by font color (blue = input, black = formula, green = link).

### Named ranges

Many templates route key inputs and outputs through named ranges. Before entering anything, open the Name Manager and review them — typical names cover revenue growth, cost percentages, marquee outputs (Net Income, EBITDA, Total Debt, Cash), and the scenario-selector cell. Make sure your inputs land in the cells those ranges actually point to.

### Forecast horizon

Most templates extend five years past the final historical year. Confirm the A/E split is unmistakable and that headers use fiscal-year notation (FY2024A, FY2025E).

## Profitability Margins (only on request)

> Add margins **only if** the user asks or the template clearly calls for them. No prompt → skip this entirely.

Place margin percentages on the IS tab to track operating efficiency and support peer comparison.

| Margin | Definition | Reads on |
|---|---|---|
| Gross Margin | Gross Profit ÷ Revenue | Pricing power, production efficiency |
| EBITDA Margin | EBITDA ÷ Revenue | Core operating profitability |
| EBIT Margin | EBIT ÷ Revenue | Operating profitability net of D&A |
| Net Margin | Net Income ÷ Revenue | Bottom-line profitability |

Render each margin as a percentage row directly beneath the profit line it describes — Gross Margin under Gross Profit, EBITDA Margin under EBITDA, EBIT Margin under EBIT, Net Margin under Net Income.

## Credit & Leverage Metrics (only on request)

> Add these **only if** the user asks or the template requires them. No prompt → skip.

Put credit metrics on the BS tab to gauge solvency, debt headroom, and covenant standing.

| Metric | Definition | Reads on |
|---|---|---|
| Total Debt / EBITDA | Total Debt ÷ LTM EBITDA | Gross leverage |
| Net Debt / EBITDA | (Total Debt − Cash) ÷ LTM EBITDA | Leverage net of cash |
| Interest Coverage | EBITDA ÷ Interest Expense | Debt-service capacity |
| Debt / Total Cap | Total Debt ÷ (Total Debt + Equity) | Capital structure |
| Debt / Equity | Total Debt ÷ Total Equity | Financial leverage |
| Current Ratio | Current Assets ÷ Current Liabilities | Near-term liquidity |
| Quick Ratio | (Current Assets − Inventory) ÷ Current Liabilities | Immediate liquidity |

**Directional sanity by scenario:** the Upside case should always look the healthiest —
- Leverage ratios: Upside < Base < Downside (lower is better)
- Coverage ratios: Upside > Base > Downside (higher is better)
- Liquidity ratios: Upside > Base > Downside (higher is better)

If covenant thresholds are known, add explicit pass/fail cells comparing each metric to its threshold.

## Scenario Toggle (Base / Upside / Downside)

Drive scenarios from a single dropdown on the Assumptions tab, switching values with `CHOOSE` or `INDEX/MATCH`.

| Scenario | What it represents |
|---|---|
| Base | Management guidance or consensus |
| Upside | Faster growth, expanding margins |
| Downside | Slower growth, compressing margins |

**Sensitize these drivers:** revenue growth, gross margin, SG&A %, DSO / DIO / DPO, CapEx %, interest rate, tax rate.

**Verify after wiring the toggle:** flipping the selector updates all three statements; the BS balances in *every* scenario; cash ties out in every scenario; and the hierarchy holds (Upside beats Base beats Downside on NI, EBITDA, FCF, and margins).

## Pulling Numbers From SEC Filings

When the template requires public-company data sourced from 10-Ks or 10-Qs, follow [references/sec-filings.md](references/sec-filings.md). You only need that reference for regulatory-filing data — skip it when the data is supplied directly.

## Workflow: Completing Any Template

A general procedure that fills in a 3-statement template without trampling existing formulas.

### Step 1 — Understand the architecture

- **Tell inputs from formulas.** Use font color and shading cues (blue = input, black = formula, green = cross-sheet link). Trace Precedents / Dependents reveals how cells connect; the Name Manager reveals controlling named ranges.
- **Chart the data flow.** Establish the feed order (typically Assumptions → IS → BS → CF), note every supporting schedule and where it plugs in, and write down the template's specific line items before you populate anything.

### Step 2 — Enter data without breaking formulas

| Discipline | Why |
|---|---|
| Edit only input cells | Don't overwrite a formula unless you mean to replace it |
| Paste Values (Ctrl+Shift+V) | Keeps source formatting from clobbering destination formulas |
| Match the template's units | Confirm thousands vs. millions vs. units before typing |
| Honor the sign convention | Follow how the template already treats expenses (positive or negative) |
| Mind circular references | If interest creates circularity, enable iterative calculation |

**Order of operations:** (1) find the designated input cells, (2) enter historicals and confirm the formulas calculate over those periods, (3) enter the forecast drivers, (4) review the calculated outputs, (5) if you ever must change a formula cell, record the original formula first.

**Pre-built formulas:** expect transient `#REF!` / `#DIV/0!` errors while inputs are incomplete — they resolve as you fill in. When a result looks wrong, trace precedents to find the missing input. Never delete rows or columns without checking cross-tab dependencies.

### Step 3 — Validate the formulas

| Technique | How |
|---|---|
| Trace precedents | Confirm a formula reads the right inputs |
| Trace dependents | Confirm key inputs reach the right outputs |
| Evaluate Formula | Step through complex calculations one operation at a time |
| Hunt hardcodes | Projection cells must reference assumptions, not literals |
| Test values | Feed simple known inputs and check the output |
| Column consistency | The same logic should repeat across every projection period (Ctrl+\ surfaces differences) |

Watch for: mixed absolute/relative references that misbehave when copied; broken links (`#REF!`); early-period division-by-zero before revenue ramps; intentional-vs-accidental circular warnings; and inconsistent formulas across columns. For cross-tab links, confirm shared values are *linked* (not retyped) and that schedule totals tie to the statement line items, with period labels aligned everywhere.

### Step 4 — Per-statement quality checks

**Income Statement**
- Historical revenue matches source.
- Expense lines sum to reported totals.
- Subtotals (Gross Profit, EBIT, EBT, Net Income) compute correctly.
- Tax logic handles losses sensibly.
- Forecast lines reference the Assumptions tab — no hardcodes.
- Period-over-period moves are directionally plausible.

**Balance Sheet**
- Assets = Liabilities + Equity for every period (the headline test).
- Cash equals the CF ending cash.
- Working-capital accounts tie to their schedule (if present).
- Retained Earnings rolls forward: Prior RE + Net Income − Dividends ± adjustments = Ending RE.
- Debt ties to the debt schedule (if present).
- Signs are correct (assets positive, most liabilities positive).

**Cash Flow Statement**
- CFO opens with Net Income equal to the IS Net Income.
- Non-cash add-backs (D&A, SBC) tie to their sources.
- Working-capital deltas carry the right sign (asset increase = cash use = negative).
- CapEx ties to the PP&E roll-forward.
- Financing lines tie to BS debt and equity movements.
- Ending Cash equals BS Cash; Beginning Cash equals the prior period's Ending Cash.

**Supporting schedules**
- Opening balance = prior period's closing balance.
- Roll-forward is complete: Beginning + Additions − Deductions = Ending.
- Schedule totals tie to the statement line items.
- Calculation assumptions match the Assumptions tab.

### Step 5 — Cross-statement integration tests

| Test | Expression | Target |
|---|---|---|
| BS balances | Assets − Liabilities − Equity | 0 |
| Cash ties out | CF Ending Cash − BS Cash | 0 |
| NI links | IS Net Income − CF opening Net Income | 0 |
| RE reconciles | Prior RE + NI − Dividends − BS Ending RE | 0 (adjust for SBC/other as needed) |

### Step 6 — Final pass

- Cycle through every scenario and confirm the checks pass in each.
- Resolve or document any `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`.
- Confirm every input cell is filled — search for leftover placeholders.
- Confirm units are consistent across tabs.
- Save a clean copy before any further edits.

## Validation & Audit Reference

All validation logic in one place. Formula specifics live in [references/formulas.md](references/formulas.md).

### Linkages that must always hold

| Test | Expression | Target |
|---|---|---|
| BS balances | Assets − Liabilities − Equity | 0 |
| Cash ties out | CF Ending Cash − BS Cash | 0 |
| Monthly vs. annual cash | Closing Cash (monthly) − Closing Cash (annual) | 0 |
| NI links | IS Net Income − CF opening Net Income | 0 |
| RE reconciles | Prior RE + NI + SBC − Dividends − BS Ending RE | 0 |
| Equity financing | ΔCommon Stock/APIC (BS) − Equity Issuance (CFF) | 0 |
| Year-0 equity | Equity Raised (Year 0) − Beginning Equity (Year 1) | 0 |

### Sign conventions

| Statement | Item | Sign |
|---|---|---|
| CFO | D&A, SBC | + (add-back) |
| CFO | ΔAR increase | − (cash use) |
| CFO | ΔAP increase | + (cash source) |
| CFI | CapEx | − |
| CFF | Debt issuance | + |
| CFF | Debt repayment | − |
| CFF | Dividends | − |

### Circularity from interest

Interest expense loops: Interest → Net Income → Cash → Debt Balance → Interest. To resolve it, enable iterative calculation (File → Options → Formulas), set max iterations to 100 and max change to 0.001, and wire a circuit-breaker toggle on the Assumptions tab so you can cut the loop if it fails to converge.

### Audit dashboard — section by section

**1 · Currency consistency** — currency named and documented in Assumptions; every tab uses the same symbol and scale; the units row matches.

**2 · Balance sheet integrity** — Assets − Liabilities − Equity = 0 for each period.

**3 · Cash flow integrity** — CF ending cash = BS cash; monthly closing cash = annual closing cash; CF Net Income = IS Net Income; D&A ties to its schedule; SBC ties to the IS; ΔAR / ΔInventory / ΔAP tie to the WC schedule; CapEx ties to the D&A schedule.

**4 · Retained earnings** — Prior RE + NI + SBC − Dividends = Ending RE; show the component breakdown for debugging.

**5 · Working capital** — AR, Inventory, AP tie to the BS; DSO / DIO / DPO sit within reasonable bands (flag outliers).

**6 · Debt schedule** — Total Debt (current + long-term) ties to the BS; interest ties to the IS.

**6b · Equity financing** — issuance proceeds tie to the BS Common Stock/APIC increase; the cash inflow equals the equity-account increase; ΔCommon Stock/APIC (BS) = Equity Issuance (CFF) = 0; Equity Raised (Year 0) = Beginning Equity (Year 1).

**6c · NOL schedule** — Beginning NOL at formation = 0; NOL grows only when EBT < 0; the schedule's DTA ties to the BS deferred-tax asset; NOL utilization ≤ 80% of EBT (post-2017 federal cap); the NOL balance never goes negative; tax expense = 0 when taxable income ≤ 0.

**7 · Scenario hierarchy** — absolute metrics (NI, EBITDA, FCF) rank Upside > Base > Downside; margins (GM%, EBITDA%, NI%) likewise; leverage ratios invert (Upside < Base < Downside).

**8 · Formula integrity** — COGS, S&M, G&A, R&D, SBC all driven as % of revenue (no hardcodes); formulas consistent across projection years; no `#REF!` / `#DIV/0!` / `#VALUE!`.

**9 · Credit thresholds** — color each metric green/yellow/red against its covenant band and summarize the red flags.

### Master status cell

Roll every section's status into one headline cell: all pass → "✓ ALL CHECKS PASS"; any failure → "✗ ERRORS DETECTED — REVIEW BELOW".

### Debugging when the master cell is red

1. Scroll to the red-highlighted sections.
2. Identify which check category failed.
3. Jump to the source tab.
4. Fix the root cause.
5. Return to the Checks tab and confirm it clears.

## Where the Data Comes From — MCP First, Web Fallback

If any structured financial-data MCP is configured (Flowly supports MCP — see the `native-mcp` skill), reach for it first for point-in-time comps, precedent transactions, and filings.

Otherwise fall back, in order, to:
- `web_search` / `web_fetch` against SEC EDGAR (`https://www.sec.gov/cgi-bin/browse-edgar`) for US filings;
- company investor-relations pages for press releases and earnings decks;
- `browser_tab(action="navigate")` for interactive data portals;
- data the user supplies directly — ask explicitly when the context lacks it.

**Never invent figures.** If a multiple, precedent, or filing value cannot be sourced, mark the cell `[UNSOURCED]` and raise it with the user.
