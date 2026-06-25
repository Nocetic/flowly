---
name: excel-author
description: Build auditable Excel workbooks headless with openpyxl — blue/black/green cell conventions, formulas over hardcodes, named ranges, balance checks, sensitivity tables. Use for financial models, audit outputs, reconciliations.
version: 1.0.0
license: Apache-2.0
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"📊","tags":["excel","openpyxl","finance","spreadsheet","modeling"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["pptx-author","dcf-model","3-statement-model","finance"]}}
---

# excel-author

Generate an `.xlsx` file programmatically with `openpyxl`, no Excel installation
required. The guidance here encodes the spreadsheet conventions that working
analysts and auditors rely on, so the workbook you ship can be opened, traced,
and stress-tested by someone who never saw it built.

## What you are producing

- Save the workbook to `./out/<name>.xlsx`, creating `./out/` first if it is missing.
- Report the path back in your closing message so any follow-on step can locate the file.
- Keep one self-contained model per workbook. Only extend a pre-existing file when the request explicitly says so.

## Installing the dependency

```bash
pip install "openpyxl>=3.0"
```

## The rules that make a model trustworthy

### A color code for cell intent

The classic spreadsheet discipline is to let font color tell the reader what a
cell *means* at a glance:

| Color | Font code | Meaning |
|-------|-----------|---------|
| Blue  | `Font(color="0000FF")` | A typed-in assumption or raw input — growth rates, discount-rate components, observed prices. |
| Black | default (`Font(color="000000")`) | A calculation. The cell holds a live formula, not a number. |
| Green | `Font(color="006100")` | A pull from another sheet or an outside file. |

With this convention in place a reviewer instantly separates "things a human
decided" from "things the workbook derived."

### Calculations live as formulas, never as baked-in numbers

Any cell that represents a computation has to carry an Excel formula string. Do
not evaluate the math in Python and drop the answer in — that turns a live model
into a snapshot that silently goes stale the moment an input changes.

```python
# Don't: the result freezes and stops responding to the assumption
ws["D20"] = revenue_prior_year * (1 + growth)

# Do: the cell recomputes whenever the driver in B8 is edited
ws["D20"] = "=D19*(1+$B$8)"
```

Literal numbers are only acceptable in three situations:

1. Reported historical facts (booked revenue, actual EBITDA, and so on).
2. Driver assumptions the user is supposed to adjust (growth, discount inputs, terminal growth).
3. Live market observations (a quoted price, an outstanding debt figure) — and these carry a comment naming where the number came from and when.

Whenever you notice yourself doing arithmetic in Python to fill a model cell,
treat that as a signal to write a formula instead.

### Name the figures that cross sheet boundaries

If a value is consumed somewhere other than the sheet it lives on — by another
tab, a slide, a written memo — give it a defined name. Named references survive
row insertions and read far more clearly than raw coordinates.

```python
from openpyxl.workbook.defined_name import DefinedName
wb.defined_names["WACC"] = DefinedName("WACC", attr_text="Inputs!$C$8")
# ...and later, on any sheet:
calc["D30"] = "=D29/WACC"
```

### A dedicated checks tab

Add a `Checks` sheet whose only job is to assert that the model is internally
consistent, each test resolving to TRUE or FALSE:

- The balance sheet actually balances (assets equal liabilities plus equity).
- The cash flow statement's net change reconciles to the swing in the balance-sheet cash line.
- Component subtotals add up to the consolidated figure.
- No stray literals are hiding inside ranges that should be all formulas.

```python
checks = wb.create_sheet("Checks")
checks["A2"] = "BS balances"
checks["B2"] = "=IS!D20-IS!D21-IS!D22"
checks["C2"] = "=ABS(B2)<0.01"   # resolves to TRUE / FALSE
```

### Cite every input at the moment you enter it

Attach the source comment in the same step that writes the value — never as a
cleanup pass afterward.

```python
from openpyxl.comments import Comment
ws["C2"] = 1_250_000_000
ws["C2"].font = Font(color="0000FF")
ws["C2"].comment = Comment("Source: 10-K FY2024, p.47, revenue line", "analyst")
```

A workable citation template: `Source: [system/document], [date], [reference], [URL if any]`.

Resist leaving a placeholder like `TODO: cite later`. The source should land
with the number.

## A starter layout for a typical model

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter
from pathlib import Path

BLUE = Font(color="0000FF")
BLACK = Font(color="000000")
GREEN = Font(color="006100")
BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)

wb = Workbook()

# --- Inputs tab ---
inp = wb.active
inp.title = "Inputs"
inp["A1"] = "MARKET DATA & KEY INPUTS"
inp["A1"].font = HEADER_FONT
inp["A1"].fill = HEADER_FILL
inp.merge_cells("A1:C1")

inp["B3"] = "Revenue FY2024"
inp["C3"] = 1_250_000_000
inp["C3"].font = BLUE
inp["C3"].comment = Comment("Source: 10-K FY2024 p.47", "model")

inp["B4"] = "Growth Rate"
inp["C4"] = 0.12
inp["C4"].font = BLUE

# --- Calc tab ---
calc = wb.create_sheet("DCF")
calc["B2"] = "Projected Revenue"
calc["C2"] = "=Inputs!C3*(1+Inputs!C4)"   # formula, stays black

# --- Checks tab ---
chk = wb.create_sheet("Checks")
chk["A2"] = "BS balances"
chk["B2"] = "=ABS(BS!D20-BS!D21-BS!D22)<0.01"

Path("./out").mkdir(exist_ok=True)
wb.save("./out/model.xlsx")
```

## Banner rows via merged cells

openpyxl has a gotcha here: after merging a range, the value belongs on the
top-left anchor cell, but fill and font have to be applied across every cell in
the span yourself.

```python
ws["A7"] = "CASH FLOW PROJECTION"
ws["A7"].font = HEADER_FONT
ws.merge_cells("A7:H7")
for col in range(1, 9):           # columns A through H
    ws.cell(row=7, column=col).fill = HEADER_FILL
```

## Two-way sensitivity grids

Drive these with loops rather than hand-typing a formula into each cell. A few
rules keep them honest:

- **Use an odd count of rows and columns** (5×5, 7×7) so there is a genuine middle cell.
- **The middle cell is the base case.** Set the center row and column headers to the model's actual live assumptions, so the center output reproduces the base-case result. If it doesn't match, the grid is wired wrong.
- **Flag the center cell** with a medium-blue fill (`"BDD7EE"`) and bold text.
- **Each interior cell holds a complete recalculation**, not a shortcut estimate.

```python
# 5x5 grid: discount rate down the rows, terminal growth across the columns
wacc_axis = [0.08, 0.085, 0.09, 0.095, 0.10]    # middle row = base 9.0%
term_axis = [0.02, 0.025, 0.03, 0.035, 0.04]    # middle col = base 3.0%

start_row = 40
ws.cell(row=start_row, column=1).value = "Implied Share Price ($)"
ws.cell(row=start_row, column=1).font = BOLD

# column headers (terminal growth)
for j, g in enumerate(term_axis):
    ws.cell(row=start_row + 1, column=2 + j).value = g
    ws.cell(row=start_row + 1, column=2 + j).font = BLUE

# row headers (discount rate) + body
for i, w in enumerate(wacc_axis):
    r = start_row + 2 + i
    ws.cell(row=r, column=1).value = w
    ws.cell(row=r, column=1).font = BLUE
    for j, g in enumerate(term_axis):
        c = 2 + j
        # A real model would reference the full projection block here.
        ws.cell(row=r, column=c).value = (
            f"=SUMPRODUCT(FCF_range,1/(1+{w})^year_offset) + "
            f"FCF_terminal*(1+{g})/({w}-{g})/(1+{w})^terminal_year"
        )

# emphasize the base-case center cell
center = ws.cell(
    row=start_row + 2 + len(wacc_axis) // 2,
    column=2 + len(term_axis) // 2,
)
center.fill = PatternFill("solid", fgColor="BDD7EE")
center.font = BOLD
```

## Computing the formulas before you hand it off

openpyxl stores formula text but never evaluates it. Excel will recalculate the
moment a person opens the file, but an automated consumer — a verification
script, a CI job — reading with `data_only=True` sees `None` for every formula
until something has actually run the math.

Force a calculation pass before delivery. The blunt way is LibreOffice in
headless mode:

```bash
libreoffice --headless --calc --convert-to xlsx ./out/model.xlsx --outdir ./out/
```

For a wrapped version that returns a status and resaves in place, use the helper
shipped alongside this skill at `scripts/recalc.py`.

## Lock the layout before writing formulas

References break when you move rows, so settle the geometry first:

1. Decide the row position of every section.
2. Write all headers and labels.
3. Insert all divider and spacer rows.
4. Only now start filling in formulas, against row positions that will not move.

Skipping straight to formulas and then squeezing in a header row later is how
you get the cascade where one insertion invalidates every downstream reference.

## Pause and confirm on big builds

For anything substantial — a DCF, a three-statement model, an LBO — stop at
natural milestones and let the user inspect the intermediate state. Spotting a
mistaken margin assumption before the sensitivity grids depend on it is far
cheaper than unwinding it afterward.

Suggested checkpoints:

- After the inputs block — confirm the raw assumptions.
- After revenue projections — confirm the top line and growth path.
- After the free-cash-flow build — confirm the full schedule.
- After the discount-rate inputs — confirm those figures.
- After the valuation — confirm the equity bridge.
- Then, and only then, build the sensitivity grids.

## When something else is the better tool

- The user is already in a live Excel session with an automation bridge available — operate on their open workbook instead of producing a file.
- The deliverable is a flat data dump with no formulas — `csv` or `pandas.to_excel` is the lighter path.
- The ask is an interactive dashboard or rich charts — reach for a real BI tool.
