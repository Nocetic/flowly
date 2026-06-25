# Formatting Reference

How cells should look so a reader can tell at a glance what each one is.

## Core conventions

| Element | How to format it |
|---|---|
| Hard-coded inputs | Blue font |
| Formulas | Black font |
| Cross-sheet links | Green font |
| Check cells | Green when balanced, red when in error |
| Negative numbers | Wrap in parentheses; don't use a minus sign |
| Currency | No decimals on large figures; 2 decimals on per-share values |
| Percentages | One decimal place |
| Header text | Bold, with a bottom border |
| Units row | Sit a units row under the headers ($ millions, %, x, …) |

## Borders for visual grouping

- A thin vertical rule between the last historical column and the first projection column.
- A single bottom border under subtotals.
- A thick bottom border under section totals (e.g. Total Assets).
- A double bottom border under grand totals.

## Bold the aggregates

Any cell that aggregates other cells — a total, subtotal, or summary — carries **bold** numerals so it stands apart from the line items feeding it. The tables below are representative, not exhaustive: bold *any* row that rolls up others.

**Income Statement**

| Row | Bold? |
|---|---|
| Gross Revenue | yes |
| Total Cost of Revenue | yes |
| Gross Profit | yes |
| Total SG&A | yes |
| EBITDA | yes |
| EBIT | yes |
| EBT | yes |
| Net Profit After Tax | yes |

**Balance Sheet**

| Row | Bold? |
|---|---|
| Total Current Assets | yes |
| Total Non-Current Assets | yes |
| Total Other Assets | yes |
| Total Assets | yes |
| Total Current Liabilities | yes |
| Total Non-Current Liabilities | yes |
| Total Equity | yes |
| Total Liabilities and Equity | yes |

**Cash Flow Statement**

| Row | Bold? |
|---|---|
| Cash Generated from Operations Before Working Capital Changes | yes |
| Total Working Capital Changes | yes |
| Net Cash Generated from Operations | yes |
| Net Cash Flow from Investing Activities | yes |
| Net Cash Flow from Financing Activities | yes |
| Closing Cash Balance | yes |

## The balance-sheet check row

The check row beneath Total Liabilities and Equity should jump out the moment it goes off zero. Drive its color off the value:

| Check value | Font |
|---|---|
| 0 (balanced) | Black / standard |
| ≠ 0 (out of balance) | Red |

Implement with the custom number format `[Red][<>0]0.00;[Red][<>0](0.00);0.00`, or a conditional-format rule "Cell Value ≠ 0 → red font."

## Margin rows

| Element | Format |
|---|---|
| Margin % rows | Indented, italic, one decimal |
| Improving trend | No special treatment (optionally a faint green) |
| Deteriorating trend | Mark for review (faint yellow) |
| Below peer average | Worth highlighting for discussion |

## Credit-metric rows

| Element | Format |
|---|---|
| Leverage multiples | One decimal with an "x" suffix (e.g. 2.5x) |
| Percentages | One decimal with a "%" suffix |
| Negative net debt | Parentheses — signals a net cash position |
| Section header | Bold, "CREDIT METRICS" |
| Separator | Thin border above the section |

## Credit-metric threshold colors

| Metric | Green | Yellow | Red |
|---|---|---|---|
| Total Debt / EBITDA | < 2.5x | 2.5x–4.0x | > 4.0x |
| Net Debt / EBITDA | < 2.0x | 2.0x–3.5x | > 3.5x |
| Interest Coverage | > 4.0x | 2.5x–4.0x | < 2.5x |
| Debt / Total Cap | < 40% | 40%–60% | > 60% |
| Current Ratio | > 1.5x | 1.0x–1.5x | < 1.0x |
| Quick Ratio | > 1.0x | 0.75x–1.0x | < 0.75x |

## Checks-tab conditional formatting

- Pass indicator → green fill.
- Fail indicator → red fill.
- Warning → yellow fill.
- Difference cell = 0 → light-green fill.
- Difference cell ≠ 0 → light-red fill.

## Margin reasonability flags

- Gross Margin < 0% → ERROR: review COGS.
- Gross Margin > 80% → WARNING: verify revenue/COGS.
- EBITDA Margin < 0% → FLAG: operating losses.
- EBITDA Margin > 50% → WARNING: unusually high.
- Net Margin < 0% → FLAG: net losses (can be acceptable in a growth phase).
- Net Margin > Gross Margin → ERROR: formula problem.
