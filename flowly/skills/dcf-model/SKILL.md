---
name: dcf-model
description: Build institutional-quality DCF valuation models in Excel — revenue projections, FCF build, WACC, terminal value, Bear/Base/Bull scenarios, 5x5 sensitivity tables. Pairs with excel-author. Use for intrinsic-value equity analysis.
version: 1.0.0
license: Apache-2.0
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"💵","tags":["finance","valuation","dcf","excel","openpyxl","modeling","investment-banking"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["excel-author","pptx-author","3-statement-model","finance"]}}
---

## Environment

You are working **headlessly with openpyxl** — the end product is an `.xlsx` workbook saved to disk, not a live spreadsheet session.

Lean on the `excel-author` skill for the mechanics of cell styling, formula syntax, named ranges, number formats, and the layout of two-axis grids. This skill layers DCF-specific structure and finance logic on top of those conventions.

Before you hand off any model, recalculate it:

```bash
python /path/to/excel-author/scripts/recalc.py ./out/model.xlsx
```

# DCF Model Builder

## What this skill produces

A discounted-cash-flow model is an estimate of what a company is worth today based on the cash it is expected to generate in the future. This skill walks you through assembling one at the standard you would expect from an equity research or banking desk: a fully-formula-driven Excel workbook that projects revenue, builds free cash flow, discounts it at a cost of capital, adds a terminal value, bridges enterprise value down to a per-share number, and stress-tests the answer across a grid of assumptions.

The deliverable is a single workbook. The main valuation lives on one sheet with three sensitivity grids parked beneath it; a second sheet isolates the cost-of-capital math.

## Data inputs

Use everything the user hands you, plus whatever live data sources are wired up (MCP servers, web access). See the "Sourcing data" section near the end for the priority order and the rule on never inventing numbers.

## Read this before you build anything

The points below are not style preferences — they are the rules that separate a model that survives an audit from one that quietly breaks. Internalize them before the first cell goes in.

### Rule 1 — Everything that can be a formula must be a formula

The model has to *recompute itself* when someone edits an assumption. That only happens if the arithmetic lives in Excel, not in your Python session.

- Projections, margins, discount factors, present values, the equity bridge, and every sensitivity cell are **live formulas**.
- Right with openpyxl: `ws["D20"] = "=D19*(1+$B$8)"`.
- Wrong with openpyxl: `ws["D20"] = 1234.5` (a number you calculated in Python).
- The *only* legitimate hardcoded entries are:
  1. Raw historical actuals (reported revenue, reported margins).
  2. Assumption drivers the user dials (growth rates, WACC components, terminal growth).
  3. Point-in-time market facts (share price, debt balance, share count).
- If you find yourself doing math in Python and dropping the result into a cell, stop. That number will go stale the moment an assumption changes.

### Rule 2 — Confirm with the user at each checkpoint, never build the whole thing in one shot

DCF errors compound downstream. A bad margin assumption found *after* the sensitivity grids are built means tearing out and rebuilding everything below it. Pause and get a thumbs-up at each of these gates:

1. **Inputs assembled** → show the raw block (revenue, margins, share count, net debt). Confirm before projecting.
2. **Revenue projected** → show the top line and implied growth percentages. Confirm before modeling costs.
3. **FCF built** → show the complete cash-flow schedule. Confirm the logic before touching WACC.
4. **WACC computed** → show the calculation and every input. Confirm before discounting.
5. **Terminal value + PVs done** → show the full bridge (EV → equity → per share). Confirm before building sensitivities.

### Rule 3 — Sensitivity grids are odd-by-odd and centered on the base case

- Always use an **odd count of rows and columns** — 5×5 is standard, 7×7 occasionally. Odd dimensions guarantee a genuine middle cell.
- The **center cell is the base case.** Construct each axis so the middle header equals the model's actual assumption. If the base WACC is 9.0%, the middle row header reads 9.0%; if terminal growth is 3.0%, the middle column header reads 3.0%. As a consequence, the center cell's computed output *must* equal the model's headline implied share price. That equality is your proof the grid is wired correctly.
- **Visually anchor the center cell** with a medium-blue fill (`#BDD7EE`) and bold font.
- Fill **every** cell with a complete DCF recomputation — typically 3 grids × 25 cells = 75 formulas. Generate them with an openpyxl loop.
- No placeholder text, no straight-line approximations, no "the user finishes this manually." Each cell recomputes the full valuation for its own assumption pair.

### Rule 4 — Comment every hardcoded value as you place it

- The instant you write a blue input, attach a comment recording where it came from.
- Comment format: `Source: [System/Document], [Date], [Reference], [URL if applicable]`.
- Do not batch this for the end and do not leave `TODO: source`. An undocumented input is an unauditable input.

### Rule 5 — Lock the layout before writing formulas

Formulas reference rows by number. If you write formulas first and then insert header rows, every reference shifts and the model fills with `#REF!`. So:

1. Decide the row position of every section up front.
2. Write all labels and headers.
3. Write all dividers and spacer rows.
4. *Only then* write formulas against the now-fixed row map.
5. Test each formula the moment it exists.

The analogy: pour the foundation before raising walls, never the reverse.

### Rule 6 — Recalculate to zero errors before delivery

- Run `python recalc.py model.xlsx 30`.
- Drive the error count to zero — no `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, `#NULL!`, `#NUM!`, or `#N/A` survives.
- The status must read `success`.

### Rule 7 — One assumption block per scenario, driven by a selector

- Lay out Bear, Base, and Bull as three separate blocks, each showing assumptions running *horizontally* across the projection years.
- A single selector cell (1 = Bear, 2 = Base, 3 = Bull) picks the active case.
- Prefer a **consolidation column** that pulls the live values with `INDEX` (detailed below) over scattering nested `IF` statements through every projection row.

## The valuation workflow, step by step

### Step 1 — Gather and sanity-check the data

Pull from MCP servers, the user, and the web (see the sourcing section). Then run the inputs through these checks:

- Is the position net debt or net cash? This flips a sign in the equity bridge, so it matters.
- Are you using *diluted* shares, and do they reflect recent buybacks or issuance?
- Do the historical margins make sense for the business model?
- Are the projected growth rates plausible against industry norms?
- Is the tax rate sane (roughly 21–28% for most US corporates)?

### Step 2 — Study the history (3–5 years)

Pull apart the recent track record so the projections are anchored in reality:

- **Top-line trajectory** — compute the revenue CAGR and name the drivers.
- **Margin path** — gross margin, EBIT margin, and FCF margin over time.
- **Capital intensity** — D&A and CapEx as a share of revenue.
- **Working-capital behavior** — how net working capital moves relative to revenue changes.
- **Returns** — the trend in ROIC and ROE.

Summarize like this:

```
Trailing-twelve-month snapshot:
  Revenue:            $X million
  Revenue CAGR:       X%
  Gross margin:       X%
  EBIT margin:        X%
  D&A / revenue:      X%
  CapEx / revenue:    X%
  FCF margin:         X%
```

### Step 3 — Project revenue

The whole model hangs off the revenue line, so build it deliberately.

- Anchor on the most recent actual revenue (LTM or last fiscal year).
- Grow it forward one year at a time with the scenario's growth rates.
- Display both the dollar level *and* the implied growth percentage in each year.

A typical growth shape:

- **Years 1–2** — higher, reflecting near-term visibility.
- **Years 3–4** — fading toward the industry average.
- **Year 5+** — converging on the terminal rate.

Core formulas:

- `Revenue(year N) = Revenue(year N-1) × (1 + growth rate)`
- `Growth%(year N) = Revenue(year N) / Revenue(year N-1) − 1`

Across the three scenarios you might use:

```
Bear:  conservative growth (e.g. 8–12%)
Base:  central case          (e.g. 12–16%)
Bull:  optimistic growth     (e.g. 16–20%)
```

### Step 4 — Model the operating cost structure

Cost lines should show realistic operating leverage as the company scales.

- **Sales & marketing** — commonly 15–40% of revenue depending on the model.
- **R&D** — commonly 10–30% for tech-heavy businesses.
- **G&A** — commonly 8–15%, with the percentage easing down as the company grows.

Non-negotiables:

- Every percentage is taken **against revenue**, never against gross profit.
- Build in leverage — the percentages should drift lower as revenue rises.
- Keep S&M, R&D, and G&A on separate lines.
- `EBIT = Gross Profit − Total Operating Expenses`.

Frame any margin expansion explicitly:

```
                Today  →  Year 5
  Gross margin:   X%   →    Y%   (justify: scale, mix, efficiency)
  EBIT margin:    X%   →    Y%   (follows from growth + opex leverage)
```

### Step 5 — Build unlevered free cash flow

Construct FCF in this order:

```
  EBIT
  − Taxes (EBIT × tax rate)
  = NOPAT
  + D&A          (non-cash; modeled as % of revenue)
  − CapEx        (% of revenue, often 4–8%)
  − Δ NWC        (change in net working capital)
  = Unlevered Free Cash Flow
```

Working capital:

- Model the change as a percentage of the *change* in revenue (ΔRevenue).
- Typical band: −2% to +2% of ΔRevenue.
- A negative figure releases cash (a source); a positive figure consumes cash (a use).

CapEx:

- **Maintenance CapEx** keeps the lights on (~2–3% of revenue).
- **Growth CapEx** funds expansion (an extra 2–5%).
- The total should square with the growth story you're telling.

### Step 6 — Compute the cost of capital (WACC)

**Cost of equity via CAPM:**

```
  Cost of equity = Risk-free rate + Beta × Equity risk premium

    Risk-free rate     = current 10-year Treasury yield
    Beta               = 5-year monthly beta vs. the market
    Equity risk premium = ~5.0–6.0% (market convention)
```

**After-tax cost of debt:**

```
  After-tax cost of debt = Pre-tax cost of debt × (1 − tax rate)

  Source the pre-tax rate from:
    - credit rating, or
    - yield on the company's bonds, or
    - interest expense / total debt from the filings
```

**Capital-structure weights:**

```
  Equity (market cap) = share price × shares outstanding
  Net debt            = total debt − cash & equivalents
  Enterprise value    = market cap + net debt

  Equity weight = market cap / enterprise value
  Debt weight   = net debt   / enterprise value

  WACC = (cost of equity × equity weight)
       + (after-tax cost of debt × debt weight)
```

**Edge cases:**

- **Net cash** (cash > debt) → net debt is negative, the debt weight can go negative, and WACC adjusts accordingly.
- **No debt** → WACC collapses to the cost of equity.

**Where WACC usually lands:**

- Large, stable cap: 7–9%
- Growth names: 9–12%
- High-growth / high-risk: 12–15%

### Step 7 — Discount the projected cash flows

**Mid-year convention** — cash arrives, on average, in the middle of each year, so the discount periods are 0.5, 1.5, 2.5, 3.5, 4.5, …

```
  Discount factor = 1 / (1 + WACC) ^ period
  PV of FCF       = unlevered FCF × discount factor
```

Worked example for year 1:

```
  FCF             = $1,000
  WACC            = 10%
  Period          = 0.5
  Discount factor = 1 / (1.10)^0.5 = 0.9535
  PV              = 1,000 × 0.9535 = $954
```

**Choosing the horizon:**

- **5 years** — the default for most names.
- **7–10 years** — high-growth companies with a long runway.
- **3 years** — mature, stable businesses.

### Step 8 — Terminal value

The terminal value captures everything beyond the explicit forecast. Two methods.

**Perpetuity growth (the default):**

```
  Terminal FCF   = final-year FCF × (1 + terminal growth)
  Terminal value = terminal FCF / (WACC − terminal growth)

  Hard constraint: terminal growth < WACC, or the denominator
  goes to zero/negative and the value blows up.
```

Picking the terminal growth rate:

- Conservative: 2.0–2.5% (roughly GDP).
- Moderate: 2.5–3.5%.
- Aggressive: 3.5–5.0% (reserve this for genuine market leaders).
- Never exceed the risk-free rate or long-run GDP growth.

**Exit multiple (the alternative):**

```
  Terminal value = final-year EBITDA × exit multiple

  Exit multiple drawn from comparable trading multiples or
  precedent transactions; commonly 8–15× EBITDA.
```

**Discount the terminal value back to today:**

```
  PV of terminal value = terminal value / (1 + WACC) ^ final period

  Under the mid-year convention, a 5-year model uses period = 4.5.
```

**Sanity check** — terminal value should sit around 50–70% of enterprise value. Above ~75% the model leans too hard on assumptions about the distant future; below ~40% the terminal inputs may be too timid.

### Step 9 — Bridge enterprise value to a per-share price

```
    Sum of PV of explicit FCFs      = $X million
  + PV of terminal value            = $Y million
  = Enterprise value                = $Z million

  − Net debt  (or + net cash if negative)  = $A million
  = Equity value                            = $B million

  ÷ Diluted shares outstanding              = C million
  = Implied price per share                 = $XX.XX

    Current price                           = $YY.YY
    Implied return = implied / current − 1  = XX%
```

Watch these:

- **Net debt = total debt − cash.** Positive net debt is subtracted from EV (lowers equity value); negative net debt (net cash) is added (raises equity value).
- **Use diluted shares** — fold in options, RSUs, and convertibles.
- Layer in **other claims** where relevant: minority interest, pension shortfalls, operating-lease obligations.

A clean output block:

```csv
Valuation Component,Amount ($M)
PV of explicit FCFs,X.X
PV of terminal value,Y.Y
Enterprise value,Z.Z
(-) Net debt,A.A
Equity value,B.B
,
Diluted shares (M),C.C
Implied price per share,$XX.XX
Current price,$YY.YY
Implied upside/(downside),+XX%
```

### Step 10 — Sensitivity grids

Build **three** grids at the foot of the DCF sheet so the reader can see how the answer moves with the inputs:

1. **WACC × terminal growth** — sensitivity to the discount rate and the perpetuity assumption.
2. **Revenue growth × EBIT margin** — sensitivity to the top line and operating leverage.
3. **Beta × risk-free rate** — sensitivity to the two main drivers of the cost of equity.

These are **plain 2-D formula grids**, not Excel's built-in Data Table feature. Every data cell carries a full DCF recomputation for its own pair of assumptions. The detailed mechanics are in the "Patterns to follow" section below; the headline requirement is that all 75 cells are populated programmatically with openpyxl.

<correct_patterns>

This section collects the patterns you *should* follow.

### Scenario blocks driven by a selector

Lay each scenario out as its own block. **Every block needs three structural pieces:**

```csv
BEAR CASE ASSUMPTIONS   (merged header across the columns)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),12%,10%,9%,8%,7%
EBIT Margin (%),45%,44%,43%,42%,41%

BASE CASE ASSUMPTIONS   (merged header across the columns)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),16%,14%,12%,10%,9%
EBIT Margin (%),48%,49%,50%,51%,52%

BULL CASE ASSUMPTIONS   (merged header across the columns)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),20%,18%,15%,13%,11%
EBIT Margin (%),50%,51%,52%,53%,54%
```

The **column-header row showing the projection years** (FY2025E, FY2026E, …) directly under each block title is mandatory. Without it the reader cannot tell which value belongs to which year.

**Wiring the scenarios in:**

1. A selector cell (say `B6`) holds 1, 2, or 3.
2. A **consolidation column** uses `INDEX` (or `OFFSET`) to lift the active value out of the right block.
3. Projection formulas point at the consolidation column — clean, single references.
4. Each scenario block carries the full assumption set across all projection years.

**Consolidation pattern (INDEX):**

```
=INDEX(B10:D10, 1, $B$6)
```

**What to avoid — nested IFs sprinkled everywhere:**

```
=IF($B$6=1,[Bear cell],IF($B$6=2,[Base cell],[Bull cell]))
```

The consolidation column keeps the scenario logic in one place and makes the model far easier to audit.

### Revenue projection pattern

Build the consolidation column first, then reference it.

- **Consolidation cell for FY1 growth:** `=INDEX([Bear FY1]:[Bull FY1], 1, $B$6)`
- **Revenue projection:** `Revenue FY1: =D29*(1+$E$10)`
  - `D29` = prior-year revenue
  - `$E$10` = consolidation cell holding the active FY1 growth (an INDEX formula)
  - `$B$6` = the selector

### FCF formula pattern

Drive the cash-flow lines off consolidation cells too:

```csv
Item,Formula,Note
D&A,=E29*$E$21,$E$21 = consolidation cell for D&A %
CapEx,=E29*$E$22,$E$22 = consolidation cell for CapEx %
Δ NWC,=(E29-D29)*$E$23,$E$23 = consolidation cell for NWC %
Unlevered FCF,=E57+E58-E60-E62,E57=NOPAT E58=D&A E60=CapEx E62=ΔNWC
```

Fix the scenario-block rows and set up the consolidation cells *before* writing any of this.

### Cell-comment format

`Source: [System/Document], [Date], [Reference], [URL if applicable]`

```csv
Item,Comment
Share price,Source: market-data feed 2025-10-12 closing price
Shares outstanding,Source: 10-K FY2024 p.45 Note 12
Historical revenue,Source: 10-K FY2024 p.32 consolidated statements
Beta,Source: market-data feed 2025-10-12 5-year monthly beta
Consensus estimates,Source: management guidance Q3 2024 call
```

### Assumption-table structure

Each scenario block needs all three pieces:

1. A merged **section header** (e.g. "BEAR CASE ASSUMPTIONS").
2. A **column-header row of years** — required, do not skip.
3. The **data rows**.

```csv
BEAR CASE ASSUMPTIONS  (merge across A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,

BASE CASE ASSUMPTIONS  (merge across A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,

BULL CASE ASSUMPTIONS  (merge across A:G)
Assumption,FY1,FY2,FY3,FY4,FY5
Revenue Growth (%),X%,X%,X%,X%,X%
EBIT Margin (%),X%,X%,X%,X%,X%
Terminal Growth,X%,,,,
WACC,X%,,,,
```

Then build the consolidation column (usually the next column over) with INDEX formulas keyed to the selector. Projections reference that column.

### Layout-first build sequence

1. **Write all labels and headers first:**

```csv
Row,Content
1,[Company] DCF Model
2,Ticker | Date | Fiscal year-end
4,Case selector
7,KEY ASSUMPTIONS
26,Assumption headers
27-31,Growth assumptions
...,...
```

2. **Add all dividers and spacer rows.**
3. **Then write formulas** against the locked rows.
4. **Test each formula immediately.**

Good order: headers, then formulas (formulas stay valid). Bad order: formulas, then headers (references shift and break).

### Sensitivity-grid implementation

These are **not** Excel's Data Table feature. They are ordinary formula grids you write with openpyxl — yes, ~75 formulas (3 grids × 25), which is routine inside a loop.

**Grid shape — 5×5, odd dimensions, base centered:**

If base WACC = 9.0% and base terminal growth = 3.0%, build both axes symmetrically around those values:

```csv
WACC \ Terminal g, 2.0%, 2.5%, 3.0%, 3.5%, 4.0%
8.0%, [f], [f], [f], [f], [f]
8.5%, [f], [f], [f], [f], [f]
9.0%, [f], [f], [★], [f], [f]     ← middle row = base WACC
9.5%, [f], [f], [f], [f], [f]
10.0%, [f], [f], [f], [f], [f]
                  ↑ middle column = base terminal g
```

**★ is the center cell.** Its output must match the headline implied price from the valuation summary. Fill it `#BDD7EE` and bold it so the base case is obvious.

**Axis rule:** `axis = [base − 2·step, base − step, base, base + step, base + 2·step]` — symmetric, odd count, guaranteed center.

**Per-cell formula** — say cell `B88` sits at WACC 8.0% (row header `$A88`) and terminal growth 2.0% (column header `B$87`). The cell recomputes the implied price substituting those two values:

```
=([sum of PV of FCFs discounted at $A88]
  + [terminal value using B$87 as growth and $A88 as WACC, discounted]
  − [net debt]) / [diluted shares]
```

**Write a formula in every cell.** Generate them in a loop:

```python
for r, wacc in enumerate(wacc_axis):
    for c, term_g in enumerate(term_g_axis):
        formula = f"=<full DCF recompute using {wacc} and {term_g}>"
        ws.cell(row=start_row + r, column=start_col + c).value = formula
```

The grids must be live the moment the file opens — no manual steps.

</correct_patterns>

<common_mistakes>

This section collects the patterns to avoid.

### Approximations or placeholders in the sensitivity grids

Do not fake the recomputation:

```
WRONG — straight-line guess:
  B97: =B88*(1+(0.096-0.116))

WRONG — division shortcut that never re-runs the DCF:
  B105: =B88/(1+(E48-0.07))
```

Do not leave notes or blanks:

```
WRONG — punting to the user:
  "Use Data → What-If Analysis → Data Table to fill these in."

WRONG — empty cells "because it's complex".
```

Do not muddle the terminology:

- Wrong: "the grids need Excel's Data Table feature" (that's a specific tool we can't automate).
- Right: "the grids are plain formula grids, one formula per cell."

Why the shortcuts fail: linear adjustments never re-run the DCF and the true relationships are non-linear, so the numbers are simply wrong; placeholders force manual work; blanks ship an incomplete model. None of it is client-ready. The honest objection — "75 formulas feels like a lot" — dissolves once you write the loop: every cell is the same pattern with two substituted values.

### Missing cell comments

Wrong: dropping in hardcoded inputs without comments, planning to "add them later," or leaving `TODO: source`. The result is unverifiable, fails the spreadsheet conventions, and isn't audit-ready. Right: comment each input *as you create it*.

### Formula references pointing at the wrong rows

Symptom — the FCF block references the wrong assumption rows:

```
D&A:   =E29*$E$34   (should be $E$21)
CapEx: =E29*$E$41   (should be $E$22 — the row shifted)
```

Cause — formulas were written first, headers inserted after, every reference slid, and the result is `#REF!`. Fix — lock the row layout before writing formulas.

### Stacking each assumption vertically across scenarios

Wrong:

```csv
Assumption,Bear,Base,Bull
Revenue Growth FY1,10%,13%,16%
Revenue Growth FY2,9%,12%,15%
```

This buries the year-over-year progression within each scenario and makes the scenarios hard to compare. Right — one horizontal block per scenario, years running across, so each case reads as a coherent set.

### No borders

A borderless model has no section structure, blends together, and looks amateur. Add borders around the major sections.

### Wrong (or absent) font-color coding

Wrong: everything black, or relying only on fills, or mixing up which cells are blue. The reader then can't tell inputs from formulas and auditing becomes impossible. Right: blue text for every hardcoded input, black for every formula, green for cross-sheet links.

### Operating expenses taken off gross profit

```
WRONG:  S&M: =E33*0.15   (E33 = gross profit)
RIGHT:  S&M: =E29*0.15   (E29 = revenue)
```

Opex scales with revenue, not gross profit; tying it to gross profit produces a nonsensical margin path.

### The five most frequent failures

1. **References off** → fix all row positions before any formula.
2. **Missing comments** → comment as you go.
3. **Faked sensitivity grids** → full DCF recompute in every cell.
4. **Scenario references crossed** → confirm each pull hits the right Bear/Base/Bull block.
5. **No borders** → add section borders for a client-ready look.

Also stay alert to:

**WACC mistakes** — mixing book and market values in the structure; misusing levered vs. unlevered beta; applying the wrong tax rate to debt; using a stale risk-free rate (use the *current* 10Y); forgetting the net-cash adjustment.

**Growth-assumption mistakes** — terminal growth ≥ WACC (infinite value); projections divorced from history; ignoring industry ceilings; revenue growth detached from unit economics; margin expansion with no operational story behind it.

**Terminal-value mistakes** — picking the wrong method for the situation; terminal value above ~80% of EV; terminal margins inconsistent with steady state; the wrong discount period.

**Cash-flow mistakes** — opex off gross profit; D&A/CapEx ratios that don't fit the business; mishandled working-capital changes; tax rates that jump year to year; NOPAT arithmetic errors.

Re-read this list before starting a build.

</common_mistakes>

## Building the workbook

This skill relies on the **`xlsx`/`excel-author` conventions** for the spreadsheet mechanics: formula construction, number formats, error checking, and the `recalc.py` recalculation step. Every workbook must satisfy those conventions, including zero formula errors after recalculation.

## What "good" looks like

A finished model should score well on:

1. Revenue and margin assumptions grounded in the actual history.
2. A cost of capital built correctly from CAPM.
3. Sensitivity coverage that shows a real range of outcomes.
4. A terminal value with a stated rationale.
5. A structure that genuinely supports scenario switching.
6. Transparent documentation of every meaningful input.

## What the user has to provide

**Required:**

1. **Company** — ticker or name.
2. **Growth view** — revenue growth for the forecast horizon, or "use consensus."

**Optional:**

- Forecast length (default 5 years).
- Bear/Base/Bull growth and margin assumptions.
- Terminal growth (default 2.5–3.0%).
- Explicit WACC inputs if you'd rather not derive them from CAPM.

## Workbook structure

### Sheets

Two sheets:

1. **DCF** — the main model, with the three sensitivity grids at the bottom.
2. **WACC** — the cost-of-capital build.

The sensitivity grids belong at the **bottom of the DCF sheet**, not on their own tab, so all valuation output stays in one place.

### Recalculation (mandatory)

After any creation or edit, recalculate with `recalc.py` from the `excel-author` skill:

```bash
python recalc.py [path_to_excel_file] [timeout_seconds]
```

Example:

```bash
python recalc.py AAPL_DCF_Model_2025-10-12.xlsx 30
```

The script recomputes every formula on every sheet via LibreOffice, scans all cells for Excel errors (`#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, `#NULL!`, `#NUM!`, `#N/A`), and returns JSON:

```json
{
  "status": "success",
  "total_errors": 0,
  "total_formulas": 42,
  "error_summary": {}
}
```

When errors exist:

```json
{
  "status": "errors_found",
  "total_errors": 2,
  "total_formulas": 42,
  "error_summary": {
    "#REF!": { "count": 2, "locations": ["DCF!B25", "DCF!C25"] }
  }
}
```

Fix everything and re-run until the status is `success` before delivering.

### Formatting

Follow the `xlsx` skill for formula and number-format conventions. On top of that, this skill specifies the visual presentation.

**Two color layers.**

**Layer 1 — font color (mandatory, from the xlsx skill):**

- **Blue (RGB 0,0,255)** — every hardcoded input (price, shares, history, assumptions).
- **Black (RGB 0,0,0)** — every formula.
- **Green (RGB 0,128,0)** — links to other sheets (e.g. references to the WACC tab).

**Layer 2 — fill color (a restrained blue/grey palette, default unless the user says otherwise):**

- Keep it to **blues and greys only**. No greens, yellows, or oranges — a rainbow workbook reads as amateur.
- **Section headers** — dark blue (`#1F4E79`) with white bold text.
- **Sub-headers / column headers** — light blue (`#D9E1F2`) with black bold text.
- **Input cells** — light grey (`#F2F2F2`) with blue font (or plain white with blue font for maximum minimalism).
- **Calculated cells** — white with black font.
- **Output rows** (per-share, EV, etc.) — medium blue (`#BDD7EE`) with black bold font.
- That's the whole palette: three blues, one grey, white.
- A user-supplied template or explicit preference always wins.

The two layers answer different questions: **font color says *what* a cell is** (input / formula / link); **fill color says *where* you are** (header / data / output).

### Borders (required)

- **Thick (1.5pt)** around major sections: key inputs, projection assumptions, the cash-flow projection, terminal value, the valuation summary, and each sensitivity grid.
- **Medium (1pt)** between sub-sections (e.g. company details vs. historical performance; growth vs. margin vs. FCF parameters).
- **Thin (0.5pt)** around data tables (scenario blocks, the historical-vs-projected matrix).
- **None** on individual interior cells, to keep tables scannable.

A model without borders is not client-ready.

### Number formats (from the xlsx skill)

- **Years** — plain text (`2024`, not `2,024`).
- **Percentages** — `0.0%`.
- **Currency** — `$#,##0` for millions, `$#,##0.00` per share; always state the unit in the header ("Revenue ($mm)").
- **Zeros** — render as a dash via formatting (`$#,##0;($#,##0);-`).
- **Large numbers** — thousands separators.
- **Negatives** — parentheses, not a leading minus.

### Cell comments (mandatory)

Every hardcoded value carries a source comment in the format `Source: [System/Document], [Date], [Reference], [URL if applicable]`. Add them as you create cells — never at the end.

### DCF sheet — detailed layout

**Section 1 — header**

```csv
Row,Content
1,[Company] DCF Model
2,Ticker: [XXX] | Date: [Date] | Fiscal year-end: [FYE]
3,(blank)
4,Case selector (1=Bear 2=Base 3=Bull)
5,Case name display (=IF(selector=1,"Bear",IF(selector=2,"Base","Bull")))
```

**Section 2 — market data (scenario-independent)**

```csv
Item,Value
Current price,$XX.XX
Shares outstanding (M),XX.X
Market cap ($M),[formula]
Net debt ($M),XXX  (or net cash if negative)
```

**Section 3 — scenario assumptions**

Three blocks (Bear, Base, Bull), each carrying the DCF assumptions — revenue growth %, EBIT margin %, tax rate %, D&A % of revenue, CapEx % of revenue, ΔNWC % of ΔRevenue, terminal growth, WACC — laid out horizontally across the projection years. Each block has a section header, a year column-header row, and data rows. See the "Assumption-table structure" pattern above.

**Section 4 — historical and projected financials**

Reference a consolidation column (the "Selected case") rather than embedding IFs in every row:

```csv
Income Statement ($M),2020A,2021A,2022A,2023A,2024E,2025E,2026E
Revenue,XXX,XXX,XXX,XXX,[=E29*(1+$E$10)],[=F29*(1+$E$11)],[=G29*(1+$E$12)]
  % growth,XX%,XX%,XX%,XX%,[=E29/D29-1],[=F29/E29-1],[=G29/F29-1]
,,,,,,
Gross Profit,XXX,XXX,XXX,XXX,[=E29*E33],[=F29*F33],[=G29*G33]
  % margin,XX%,XX%,XX%,XX%,[=E33/E29],[=F33/F29],[=G33/G29]
,,,,,,
Operating Expenses:,,,,,,,
  S&M,XXX,XXX,XXX,XXX,[=E29*0.15],[=F29*0.14],[=G29*0.13]
  R&D,XXX,XXX,XXX,XXX,[=E29*0.12],[=F29*0.11],[=G29*0.10]
  G&A,XXX,XXX,XXX,XXX,[=E29*0.08],[=F29*0.07],[=G29*0.07]
  Total OpEx,XXX,XXX,XXX,XXX,[=E36+E37+E38],[=F36+F37+F38],[=G36+G37+G38]
,,,,,,
EBIT,XXX,XXX,XXX,XXX,[=E33-E39],[=F33-F39],[=G33-G39]
  % margin,XX%,XX%,XX%,XX%,[=E41/E29],[=F41/F29],[=G41/G29]
,,,,,,
Taxes,(XX),(XX),(XX),(XX),[=E41*$E$24],[=F41*$E$24],[=G41*$E$24]
  Tax rate,XX%,XX%,XX%,XX%,[=E43/E41],[=F43/F41],[=G43/G41]
,,,,,,
NOPAT,XXX,XXX,XXX,XXX,[=E41-E43],[=F41-F43],[=G41-G43]
```

Key pattern:

- Revenue growth: `=E29*(1+$E$10)` where `$E$10` is the consolidation cell for year-1 growth.
- Not: `=E29*(1+IF($B$6=1,$B$10,IF($B$6=2,$C$10,$D$10)))`.

**Section 5 — free cash flow build**

Confirm every reference points at the *correct* assumption row, and test each formula on creation:

```csv
Cash Flow ($M),2020A,2021A,2022A,2023A,2024E,2025E,2026E
NOPAT,XXX,XXX,XXX,XXX,[=E45],[=F45],[=G45]
(+) D&A,XXX,XXX,XXX,XXX,[=E29*$E$21],[=F29*$E$21],[=G29*$E$21]
    % of Rev,XX%,XX%,XX%,XX%,[=E58/E29],[=F58/F29],[=G58/G29]
(-) CapEx,(XX),(XX),(XX),(XX),[=E29*$E$22],[=F29*$E$22],[=G29*$E$22]
    % of Rev,XX%,XX%,XX%,XX%,[=E60/E29],[=F60/F29],[=G60/G29]
(-) Δ NWC,(XX),(XX),(XX),(XX),[=(E29-D29)*$E$23],[=(F29-E29)*$E$23],[=(G29-F29)*$E$23]
    % of Δ Rev,XX%,XX%,XX%,XX%,[=E62/(E29-D29)],[=F62/(F29-E29)],[=G62/(G29-F29)]
,,,,,,
Unlevered FCF,XXX,XXX,XXX,XXX,[=E57+E58-E60-E62],[=F57+F58-F60-F62],[=G57+G58-G60-G62]
```

Reference map (from the layout plan):

- `$E$21` = D&A % (consolidation column, row 21)
- `$E$22` = CapEx % (row 22)
- `$E$23` = NWC % (row 23)
- `E29` = revenue (row 29)
- `E45` = NOPAT (row 45)

Verify these against the actual layout, build one column, then copy across.

**Section 6 — discounting and valuation**

```csv
DCF Valuation,2024E,2025E,2026E,2027E,2028E,Terminal
Unlevered FCF ($M),XXX,XXX,XXX,XXX,XXX,
Period,0.5,1.5,2.5,3.5,4.5,
Discount factor,0.XX,0.XX,0.XX,0.XX,0.XX,
PV of FCF ($M),XXX,XXX,XXX,XXX,XXX,
,,,,,,
Terminal FCF ($M),,,,,,XXX
Terminal value ($M),,,,,,XXX
PV terminal value ($M),,,,,,XXX
,,,,,,
Valuation summary ($M),,,,,,
Sum of PV FCFs,XXX,,,,,
PV terminal value,XXX,,,,,
Enterprise value,XXX,,,,,
(-) Net debt,(XX),,,,,
Equity value,XXX,,,,,
,,,,,,
Diluted shares (M),XX.X,,,,,
IMPLIED PRICE PER SHARE,$XX.XX,,,,,
Current price,$XX.XX,,,,,
Implied upside/(downside),XX%,,,,,
```

### WACC sheet — layout

```csv
COST OF EQUITY,,
Risk-free rate (10Y Treasury),X.XX%,[input]
Beta (5Y monthly),X.XX,[input]
Equity risk premium,X.XX%,[input]
Cost of equity,X.XX%,[calculated]
,,
COST OF DEBT,,
Credit rating,AA-,[input]
Pre-tax cost of debt,X.XX%,[input]
Tax rate,XX.X%,[link to DCF]
After-tax cost of debt,X.XX%,[calculated]
,,
CAPITAL STRUCTURE,,
Current price,$XX.XX,[link to DCF]
Shares outstanding (M),XX.X,[link to DCF]
Market cap ($M),"X,XXX",[calculated]
,,
Total debt ($M),XXX,[input]
Cash & equivalents ($M),XXX,[input]
Net debt ($M),XXX,[calculated]
,,
Enterprise value ($M),"X,XXX",[calculated]
,,
WACC,Weight,Cost,Contribution
Equity,XX.X%,X.X%,X.XX%
Debt,XX.X%,X.X%,X.XX%
,,
WEIGHTED AVERAGE COST OF CAPITAL,X.XX%,[output]
```

Key WACC formulas:

```
Market cap       = price × shares
Net debt         = total debt − cash
Enterprise value = market cap + net debt
Equity weight    = market cap / EV
Debt weight      = net debt   / EV
WACC             = cost of equity × equity weight
                 + after-tax cost of debt × debt weight
```

### Sensitivity grids (bottom of the DCF sheet)

Reminder: a "sensitivity grid" is a plain 2-D grid — row headers, column headers, a formula in each data cell. It is **not** Excel's Data Table feature. You write ordinary Excel formulas into each cell with openpyxl.

**Location:** rows 87+ on the DCF sheet, not a separate tab.

**Three grids, stacked:**

1. **WACC × terminal growth** (rows ~87–100) — 25 formula cells.
2. **Revenue growth × EBIT margin** (rows ~102–115) — 25 formula cells.
3. **Beta × risk-free rate** (rows ~117–130) — 25 formula cells.

**Total: 75 formulas — required, not optional.** Populate every cell programmatically. No linear shortcuts, no placeholder notes, no empty cells.

**Setup:**

1. Build each grid's row/column headers (the assumption values being tested).
2. Fill every data cell with a formula that takes the row-header value and the column-header value, recomputes the full DCF on those assumptions, and returns the implied share price.
3. All cells live on delivery.
4. Apply a color scale (higher values greener, lower values redder).
5. Bold the base-case (center) cell.
6. Leave one or two blank rows between grids.

The grids must work the instant the file opens, with nothing left for the user to do.

## Implementing the case selector

**Three cases:**

**Bear** — conservative growth (low end of history), flat or compressing margins, a higher WACC (extra risk premium), a lower terminal rate, heavier CapEx.

**Base** — consensus or guidance growth, moderate margin expansion from operating leverage, the current market-implied WACC, GDP-aligned terminal growth (2.5–3.0%), standard CapEx.

**Bull** — optimistic growth (high end), meaningful margin expansion, a lower WACC (less risk premium), higher terminal growth (3.5–5.0%), lighter CapEx.

**Implementation** — do not scatter nested IFs. Use a consolidation column with `INDEX` (or `OFFSET`):

```
=INDEX(B10:D10, 1, $B$6)
```

where `B10:D10` are the Bear/Base/Bull values and `$B$6` is the selector (1/2/3). Then reference the consolidation column everywhere:

```
Revenue FY1: =D29*(1+$E$10)
```

with `$E$10` the consolidation cell for year-1 growth. This keeps the scenario logic in one auditable place.

## Deliverable

**File name:** `[Ticker]_DCF_Model_[Date].xlsx`

**Two sheets:**

1. **DCF** — full model with Bear/Base/Bull cases and the three sensitivity grids at the bottom (WACC × terminal growth, revenue growth × EBIT margin, beta × risk-free rate).
2. **WACC** — cost-of-capital build.

**Must include:** the case selector (1/2/3), a consolidation column with INDEX/OFFSET, color-coded cells, source comments on every input, and professional borders.

## Practices worth keeping

**Construction** — build section by section and finish each before the next; drop in sample numbers to test formulas as you go; reuse the same pattern for similar calculations; comment any unusual formula; build in sum and balance checks.

**Documentation** — explain the reasoning behind key inputs; cite the source of every data point; describe any non-standard method; flag the assumptions you have least visibility into.

**Quality control** — cross-check the math more than one way; use the sensitivity grids to confirm the model is robust; have someone else review the formulas; save versions as you progress.

## Situational variations

**High-growth tech** — longer horizon (7–10 years), high initial growth (20–30%), meaningful margin expansion, higher WACC (12–15%), model the unit economics (users, ARPU).

**Mature / stable** — shorter horizon (3–5 years), modest growth (GDP +1–3%), steady margins, lower WACC (7–9%), focus on cash generation and capital allocation.

**Cyclical** — model across the cycle, normalize margins at mid-cycle, consider trough and peak cases, adjust beta for cyclicality.

**Multi-segment** — separate DCFs per business unit, segment-specific growth and margins, sum-of-the-parts, account for synergies.

## Troubleshooting

If recalc throws errors, results look off, or the case selector misbehaves, see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

## Workflow integration

### Kicking off

1. **Market data** — check for MCP servers, use web search/fetch for price/beta/market metrics, ask the user for anything specific.
2. **Historical financials** — check MCP servers (e.g. Daloopa), ask the user, or pull from 10-Ks as a last resort.
3. **Build** using the methodology above.

### While building

1. Use openpyxl with **formulas, not hardcoded values**.
2. Follow the `xlsx`/`excel-author` conventions for formulas and formatting.
3. Apply fills per the palette above (or per the user's template).

### Before delivery (mandatory)

1. **Verify structure** — scenario blocks across the projection years; a working selector that references the right blocks; sensitivity grids at the bottom of the DCF sheet; correct font colors (blue inputs, black formulas, green links); comments on every input; section borders.
2. **Recalculate** — `python recalc.py model.xlsx 30`.
3. **Check status** — `success` → continue; `errors_found` → inspect `error_summary` and see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).
4. **Fix and re-run** until `success`.
5. **Spot-check formulas** — does an FCF formula hit the right assumption rows? does flipping the selector update the consolidation column? do revenue formulas reference the consolidation column (not nested IFs)?
6. **Deliver.**

### Data sources at a glance

- **MCP servers** — if configured (e.g. Daloopa for historicals).
- **Web search/fetch** — for current prices, beta, market data.
- **User-provided** — historicals, consensus estimates.
- **Manual** — SEC EDGAR filings as fallback.

## Final checklist

**Required:**

- `python recalc.py model.xlsx 30` returns `success` (zero formula errors).
- Two sheets: DCF (sensitivities at the bottom) and WACC.
- Font colors: blue inputs, black formulas, green links.
- Source comments on every input.
- All sensitivity cells populated with formulas.
- Section borders.

**Validation:**

- Opex computed off revenue, not gross profit.
- Terminal value 50–70% of EV.
- Terminal growth < WACC.
- Tax rate 21–28%.
- File named `[Ticker]_DCF_Model_[Date].xlsx`.

## Sourcing data — MCP first, web fallback

Where this guide references commercial financial-data MCPs, treat them as optional:

- **If any structured financial-data MCP is configured** (Flowly supports MCP — see the `native-mcp` skill), prefer it for point-in-time comps, precedent transactions, and filings.
- **Otherwise**, fall back to:
  - `web_search` / `web_fetch` against SEC EDGAR (`https://www.sec.gov/cgi-bin/browse-edgar`) for US filings.
  - Company IR pages for press releases and earnings decks.
  - `browser_tab(action="navigate")` for interactive data portals.
  - The user — ask explicitly when the data isn't in context.
- **Never fabricate.** If a multiple, precedent, or filing number can't be sourced, mark the cell `[UNSOURCED]` and raise it with the user.
